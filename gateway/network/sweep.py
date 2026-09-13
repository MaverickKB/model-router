"""A single bounded scan: inventory TCP surfaces, then inspect under saved policy."""

import asyncio
import copy
import socket
import time

import httpx

from .local import local_listeners
from .protocols import inspect_service
from .report import Phase, publish, read_policy, read_report
from .scanner import scan


def coverage_ports(policy: dict) -> str:
    """TCP coverage is the saved range plus ports approved for HTTP inspection."""
    extra = [str(port) for port in policy.get("http_ports") or []]
    parts = [part for part in [policy.get("port_range") or "", *extra] if part]
    return ",".join(parts)


class Sweep:
    def __init__(self, root, privileged=False, runner=scan):
        self.root, self.privileged, self.runner = root, privileged, runner
        self.report = read_report(root)
        self.policy = {}
        self.probes = asyncio.Semaphore(8)
        self.last_saved = 0.0

    async def save(self, force=False):
        now = time.monotonic()
        if force or now - self.last_saved >= 1:
            self.last_saved = now
            self.report["updated_at"] = time.time()
            await asyncio.to_thread(publish, self.root, copy.deepcopy(self.report))

    async def probe(self, http, host, port):
        async with self.probes:
            address = host["address"]
            authority = f"[{address}]" if ":" in address else address
            origin = f"http://{authority}:{port}"
            previous = next(
                (s for s in host.get("services", []) if s["port"] == port), {}
            )
            if self.policy.get("inspect_all_open_ports") or port in self.policy.get(
                "http_ports", []
            ):
                result = await inspect_service(http, origin)
                if result["status"] == "unidentified":
                    secure = await inspect_service(http, f"https://{authority}:{port}")
                    if secure["status"] != "unidentified":
                        result = secure
            else:
                result = {
                    "origin": origin,
                    "status": "inspection_required",
                    "protocol": "unverified",
                    "base_url": "",
                    "models": [],
                    "capabilities": [],
                    "detail": "Open TCP port. Enable HTTP inspection for this port in Settings or connect its model endpoint explicitly.",
                }
            result.update(port=port, checked_at=time.time())
            result["catalog_tracked"] = previous.get(
                "catalog_tracked", False
            ) or result["status"] in {"model_service", "model_surface", "gateway"}
            if result["status"] not in {"unidentified", "inspection_required"}:
                host.update(
                    status="up",
                    evidence="HTTP service response",
                    seen_at=result["checked_at"],
                )
            host["services"] = [
                s for s in host.get("services", []) if s["port"] != port
            ] + [result]
            await self.save()

    async def pass_scan(self, http, targets, ports, phase=Phase.RUNNING):
        self.report.update(phase=phase, progress=0)
        await self.save(True)
        async for item in self.runner(
            targets,
            ports=ports,
            packets_per_second=self.policy.get("packets_per_second", 1000),
            privileged=self.privileged,
            backend=self.policy.get("scanner", "connect"),
            max_addresses=self.policy.get("max_addresses", 4096),
        ):
            if "progress" in item:
                self.report["progress"] = item["progress"]
            if "host" in item:
                observed = item["host"]
                host = next(
                    (
                        h
                        for h in self.report["hosts"]
                        if h["address"] == observed["address"]
                    ),
                    None,
                )
                if host is None:
                    host = {
                        "address": observed["address"],
                        "ports": [],
                        "services": [],
                        "scope": "network",
                    }
                    self.report["hosts"].append(host)
                # Preserve fresh catalog evidence even when packet responses are filtered.
                responsive = {
                    s["port"]
                    for s in host["services"]
                    if s.get("catalog_tracked")
                    and s.get("checked_at", 0) >= self.report.get("started_at", 0)
                    and s["status"] in {"model_service", "model_surface", "gateway"}
                }
                ports_seen = set(observed["ports"]) | responsive
                if item.get("partial"):
                    ports_seen |= set(host["ports"])
                if "hardware_address" not in observed:
                    # A portable TCP observation cannot confirm the prior
                    # link-layer identity for a reused DHCP address.
                    host.pop("hardware_address", None)
                host.update({k: v for k, v in observed.items() if k != "ports"})
                host.update(
                    ports=sorted(ports_seen),
                    seen_at=time.time(),
                    scan_complete=not item.get("partial", False),
                )
                if responsive:
                    host.update(status="up", evidence="HTTP service response")
                host["services"] = [
                    s for s in host["services"] if s["port"] in ports_seen
                ]
                for offset in range(0, len(observed["ports"]), 8):
                    await asyncio.gather(
                        *(
                            self.probe(http, host, p)
                            for p in observed["ports"][offset : offset + 8]
                        )
                    )
            await self.save()
        await self.save(True)

    async def collect(self, policy):
        self.policy = policy
        hosts = (
            copy.deepcopy(self.report.get("hosts", []))
            if self.report.get("targets") == policy["targets"]
            else []
        )
        for host in hosts:
            host.update(scan_complete=False, status="pending")
        self.report.update(
            phase=Phase.RUNNING,
            started_at=time.time(),
            completed_at=0,
            targets=policy["targets"],
            ports=coverage_ports(policy),
            packets_per_second=policy["packets_per_second"],
            hosts=hosts,
            error="",
            warnings=[],
            progress=0,
        )
        await self.save(True)
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=32),
        ) as http:
            await self.pass_scan(http, policy["targets"], coverage_ports(policy))
            if policy.get("include_loopback"):
                try:
                    local = await local_listeners()
                except RuntimeError as error:
                    self.report["warnings"].append(str(error))
                else:
                    for address, ports in local.items():
                        host = next(
                            (
                                h
                                for h in self.report["hosts"]
                                if h["address"] == address
                            ),
                            None,
                        )
                        if host is None:
                            host = {"address": address, "services": []}
                            self.report["hosts"].append(host)
                        host.update(
                            name=socket.gethostname(),
                            scope="router_local",
                            ports=ports,
                            status="up",
                            evidence="local listening sockets",
                            scan_complete=True,
                        )
                        for offset in range(0, len(ports), 8):
                            await asyncio.gather(
                                *(
                                    self.probe(http, host, p)
                                    for p in ports[offset : offset + 8]
                                )
                            )
        self.report.update(phase=Phase.COMPLETE, progress=100, completed_at=time.time())
        await self.save(True)

    async def watch_catalogs(self):
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as http:
            while True:
                policy = await asyncio.to_thread(read_policy, self.root)
                await asyncio.sleep(policy.get("refresh_seconds", 10))
                if not policy.get("enabled"):
                    continue
                current = await asyncio.to_thread(read_policy, self.root)
                if current != policy:
                    continue
                self.policy = policy
                observed = [
                    (host, service["port"])
                    for host in self.report.get("hosts", [])
                    for service in host.get("services", [])
                    if service.get("catalog_tracked")
                ]
                for offset in range(0, len(observed), 8):
                    await asyncio.gather(
                        *(
                            self.probe(http, host, port)
                            for host, port in observed[offset : offset + 8]
                        )
                    )
