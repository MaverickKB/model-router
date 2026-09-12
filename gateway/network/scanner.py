"""Nmap execution and XML decoding. No model names, serving ports or GPU assumptions."""

from __future__ import annotations

import asyncio
import ipaddress
import shutil
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator

from .local import is_local_address
from .portable import addresses as expand_addresses
from .portable import scan as connect_scan


def scoped_targets(values: list[str]) -> list[str]:
    result = []
    for value in values:
        value = value.strip()
        if not value or value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("Use individual hostnames, addresses or CIDR networks")
        if "/" in value:
            network = ipaddress.ip_network(value, strict=False)
            value = str(network)
        result.append(value)
    return result


def read_host(element: ET.Element) -> dict:
    address = element.find("address[@addrtype='ipv4']")
    if address is None:
        address = element.find("address[@addrtype='ipv6']")
    status = element.find("status")
    if address is None or status is None:
        return {}
    names = element.findall("hostnames/hostname")
    return {
        "address": address.get("addr"),
        "name": next((n.get("name") for n in names if n.get("name")), ""),
        "status": status.get("state"),
        "evidence": status.get("reason", ""),
        "ports": [
            int(p.get("portid"))
            for p in element.findall("ports/port")
            if p.find("state") is not None and p.find("state").get("state") == "open"
        ],
        "listeners": {
            int(p.get("portid")): dict(p.find("service").attrib)
            for p in element.findall("ports/port")
            if p.find("service") is not None
        },
        "filtered_ports": sum(
            int(p.get("count", "0"))
            for p in element.findall("ports/extraports")
            if p.get("state") in {"filtered", "open|filtered"}
        )
        + sum(
            1
            for p in element.findall("ports/port/state")
            if p.get("state") in {"filtered", "open|filtered"}
        ),
    }


async def scan_nmap(
    targets: list[str], *, ports: str | None, packets_per_second=1000, privileged=False
) -> AsyncIterator[dict]:
    groups: dict[tuple[bool, bool], list[str]] = {}
    for target in scoped_targets(targets):
        ipv6 = ":" in target
        # Local publishing (including Docker) requires the normal socket path.
        # Remote port discovery uses SYN probes; catalog validation always uses TCP.
        raw = privileged and (ports is None or not is_local_address(target))
        groups.setdefault((ipv6, raw), []).append(target)
    for (ipv6, raw), addresses in groups.items():
        async for item in _scan(
            addresses,
            ports=ports,
            packets_per_second=packets_per_second,
            privileged=raw,
            ipv6=ipv6,
        ):
            yield item


async def _scan(
    targets: list[str],
    *,
    ports: str | None,
    packets_per_second=1000,
    privileged=False,
    ipv6=False,
) -> AsyncIterator[dict]:
    binary = shutil.which("nmap")
    if not binary:
        raise RuntimeError(
            "Network scanner is unavailable: install nmap on the router host"
        )
    targets = scoped_targets(targets)
    if not targets:
        return
    args = [
        binary,
        "--privileged" if privileged else "--unprivileged",
        "-n",
        "-v",
        "--max-rate",
        str(packets_per_second),
        "--max-retries",
        "3",
        "--initial-rtt-timeout",
        "250ms",
        "--max-rtt-timeout",
        "1000ms",
        "--stats-every",
        "5s",
        "-oX",
        "-",
    ]
    if privileged:
        # Allow adaptive timing within a tenfold range of the operator's budget.
        # This keeps silent targets from stalling the complete inventory indefinitely.
        args += ["--min-rate", str(max(1, packets_per_second // 10))]
    if ipv6:
        args.append("-6")
    if ports is None:
        args += ["-sn"]
    else:
        # Port reachability must stream independently of protocol inspection.
        # A slow service fingerprint must not hold every host's inventory back.
        args += ["-sS" if privileged else "-sT", "-Pn"]
        args += ["--max-hostgroup", "8"]
        if privileged:
            args += ["--defeat-rst-ratelimit"]
        args += ["--top-ports", "1000"] if ports == "top1000" else ["-p", ports]
    args += targets
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    errors = asyncio.create_task(process.stderr.read())
    parser = ET.XMLPullParser(events=["end"])
    try:
        while chunk := await process.stdout.read(65536):
            parser.feed(chunk)
            for _, element in parser.read_events():
                if element.tag == "host":
                    host = read_host(element)
                    if host:
                        yield {"host": host}
                    element.clear()
                elif element.tag == "taskprogress":
                    yield {
                        "progress": float(element.get("percent", "0")),
                        "task": element.get("task", ""),
                    }
                elif element.tag == "finished" and element.get("exit") != "success":
                    raise RuntimeError("Network scan did not complete successfully")
        parser.close()
        await process.wait()
        error = (await errors).decode(errors="replace").strip()
        if process.returncode:
            raise RuntimeError(error[-500:] or "Network scanner failed")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        await asyncio.gather(errors, return_exceptions=True)


async def scan(
    targets,
    *,
    ports,
    packets_per_second=1000,
    privileged=False,
    backend="connect",
    max_addresses=4096,
):
    if backend == "connect":
        async for item in connect_scan(
            targets,
            ports=ports,
            packets_per_second=packets_per_second,
            max_addresses=max_addresses,
        ):
            yield item
    elif backend == "nmap":
        await expand_addresses(targets, max_addresses)
        async for item in scan_nmap(
            targets,
            ports=ports,
            packets_per_second=packets_per_second,
            privileged=privileged,
        ):
            yield item
    else:
        raise ValueError("Scanner backend is not installed")
