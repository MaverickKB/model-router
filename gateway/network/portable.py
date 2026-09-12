"""Portable TCP connection discovery with bounded concurrency and no payload probes."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time


def port_numbers(value: str) -> list[int]:
    ports = set()
    for part in value.split(","):
        ends = [int(v) for v in part.split("-")]
        ports.update(range(ends[0], ends[-1] + 1))
    return sorted(ports)


async def addresses(targets: list[str], maximum: int) -> list[str]:
    result = set()
    for target in targets:
        try:
            network = ipaddress.ip_network(target, strict=False)
        except ValueError:
            if "/" in target:
                raise
            try:
                rows = await asyncio.to_thread(socket.getaddrinfo, target, None)
            except OSError as exc:
                raise ValueError(f"Cannot resolve configured target {target}") from exc
            result.update(row[4][0] for row in rows)
        else:
            if network.num_addresses > maximum:
                raise ValueError(
                    f"Scope exceeds the configured {maximum} address budget; increase the budget or narrow the scope"
                )
            result.update(str(ip) for ip in network)
        if len(result) > maximum:
            raise ValueError("Scope exceeds the configured address budget")
    return sorted(
        result,
        key=lambda value: (
            ipaddress.ip_address(value).version,
            int(ipaddress.ip_address(value)),
        ),
    )


async def scan(
    targets, *, ports, packets_per_second=1000, privileged=False, max_addresses=4096
):
    hosts = await addresses(targets, max_addresses)
    numbers = port_numbers(ports)
    if not numbers:
        raise ValueError("Choose at least one TCP port")
    jobs: asyncio.Queue = asyncio.Queue(128)
    results: asyncio.Queue = asyncio.Queue(128)
    observations = {
        address: {
            "address": address,
            "name": "",
            "status": "down",
            "evidence": "no TCP response",
            "ports": [],
            "listeners": {},
            "filtered_ports": 0,
        }
        for address in hosts
    }
    remaining = {address: len(numbers) for address in hosts}
    rate = asyncio.Lock()
    next_start = time.monotonic()

    async def produce():
        for port in numbers:
            for address in hosts:
                await jobs.put((address, port))
        for _ in range(64):
            await jobs.put(None)

    async def worker():
        nonlocal next_start
        while item := await jobs.get():
            address, port = item
            async with rate:
                delay = next_start - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                next_start = max(next_start, time.monotonic()) + 1 / packets_per_second
            observation = observations[address]
            opened = False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), 0.5
                )
                writer.close()
                await writer.wait_closed()
                observation["ports"].append(port)
                opened = True
                observation.update(status="up", evidence="TCP connection accepted")
            except ConnectionRefusedError:
                observation.update(status="up", evidence="TCP connection refused")
            except (OSError, TimeoutError):
                observation["filtered_ports"] += 1
            remaining[address] -= 1
            if opened:
                await results.put(
                    {"host": {**observation, "ports": [port]}, "partial": True}
                )
            if not remaining[address]:
                await results.put(
                    {"host": {**observation, "ports": sorted(observation["ports"])}}
                )

    tasks = [asyncio.create_task(produce())] + [
        asyncio.create_task(worker()) for _ in range(64)
    ]
    completed = 0
    try:
        while completed < len(hosts):
            item = await results.get()
            if not item.get("partial"):
                completed += 1
            yield item
            yield {
                "progress": 100 * completed / max(1, len(hosts)),
                "task": "TCP connections",
            }
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
