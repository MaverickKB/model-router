"""A single bounded scan: inventory TCP surfaces, then inspect under saved policy."""

import asyncio
import copy
import socket
import time

import httpx

from .local import local_listeners
from .report import Phase, mutate_report, read_policy, read_report
from .scanner import scan
from .transports import inspect_transports


def catalog_refresh_targets(report: dict) -> list[tuple[dict, int]]:
    """Return each tracked host port once, even when it has many API surfaces."""
    targets: list[tuple[dict, int]] = []
    seen: set[tuple[str, int]] = set()
    for host in report.get("hosts", []):
        address = host.get("address")
        if not isinstance(address, str) or not address:
            continue
        for service in host.get("services", []):
            port = service.get("port")
            if not service.get("catalog_tracked") or type(port) is not int:
                continue
            key = (address, port)
            if key in seen:
                continue
            seen.add(key)
            targets.append((host, port))
    return targets


def _surface_key(service: dict) -> tuple[object, str]:
    """Keep independent API bases separate even when they share a TCP port."""
    return (
        service.get("port"),
        str(service.get("surface_id") or service.get("origin") or ""),
    )


def _checked_at(row: dict) -> float:
    value = row.get("checked_at", row.get("seen_at", 0))
    return float(value) if isinstance(value, int | float) else 0.0


def _merge_mdns_service(scan_service: dict, mdns_service: dict) -> dict:
    """Retain the newest classification while preserving mDNS provenance."""
    newer, older = (
        (mdns_service, scan_service)
        if _checked_at(mdns_service) >= _checked_at(scan_service)
        else (scan_service, mdns_service)
    )
    merged = {**copy.deepcopy(older), **copy.deepcopy(newer)}
    merged["catalog_tracked"] = bool(
        scan_service.get("catalog_tracked") or mdns_service.get("catalog_tracked")
    )
    # A TCP scan may refresh exactly the same surface later, but the mDNS
    # announcement remains real operator-visible provenance for that surface.
    merged["discovery_source"] = "mdns"
    return merged


def _merge_mdns_evidence_into(sweep_report: dict, current_report: dict) -> dict:
    """Overlay mDNS observations onto a sweep snapshot without losing surfaces.

    The sweep owns scan progress and packet-derived host state.  mDNS owns
    announcement-derived service evidence.  When either writes after the
    other, every mDNS surface from the current report is merged into the sweep
    snapshot by ``(port, surface_id)``.  A matching surface is one service,
    not duplicate rows, while different API bases remain independent rows.
    """
    hosts = sweep_report.setdefault("hosts", [])
    by_address = {
        str(host.get("address")): host
        for host in hosts
        if isinstance(host, dict) and isinstance(host.get("address"), str)
    }
    for source_host in current_report.get("hosts", []):
        if not isinstance(source_host, dict):
            continue
        address = source_host.get("address")
        if not isinstance(address, str) or not address:
            continue
        announced = [
            copy.deepcopy(service)
            for service in source_host.get("services", [])
            if isinstance(service, dict) and service.get("discovery_source") == "mdns"
        ]
        if not announced:
            continue
        target = by_address.get(address)
        if target is None:
            target = copy.deepcopy(source_host)
            target["services"] = announced
            target["ports"] = sorted(
                {
                    *(
                        port
                        for port in source_host.get("ports", [])
                        if isinstance(port, int)
                    ),
                    *(
                        port
                        for port in (service.get("port") for service in announced)
                        if isinstance(port, int)
                    ),
                }
            )
            hosts.append(target)
            by_address[address] = target
            continue

        target.setdefault("services", [])
        target.setdefault("ports", [])
        services = {
            _surface_key(service): service
            for service in target["services"]
            if isinstance(service, dict)
        }
        for service in announced:
            key = _surface_key(service)
            existing = services.get(key)
            services[key] = (
                _merge_mdns_service(existing, service) if existing else service
            )
        target["services"] = list(services.values())
        target["ports"] = sorted(
            {
                *(port for port in target["ports"] if isinstance(port, int)),
                *(
                    port
                    for port in source_host.get("ports", [])
                    if isinstance(port, int)
                ),
                *(
                    port
                    for port in (service.get("port") for service in announced)
                    if isinstance(port, int)
                ),
            }
        )
        target["seen_at"] = max(
            _checked_at(target), _checked_at(source_host)
        )
        if _checked_at(source_host) >= _checked_at(target):
            target["status"] = source_host.get("status", target.get("status", "up"))
            target["evidence"] = source_host.get(
                "evidence", target.get("evidence", "mDNS service announcement")
            )
    return sweep_report


def merge_mdns_evidence(sweep_report: dict, current_report: dict) -> dict:
    """Return a sweep snapshot enriched with mDNS evidence from another writer."""
    return _merge_mdns_evidence_into(copy.deepcopy(sweep_report), current_report)


def commit_sweep_report(root, report: dict) -> dict:
    """Publish a sweep snapshot without replacing a newer mDNS observation."""
    snapshot = copy.deepcopy(report)
    return mutate_report(root, lambda current: merge_mdns_evidence(snapshot, current))


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
            snapshot = copy.deepcopy(self.report)
            persisted = await asyncio.to_thread(
                commit_sweep_report, self.root, snapshot
            )
            # Keep the long-lived sweep's in-memory report aware of mDNS
            # services that arrived after it started.  Merge in place so
            # concurrent probe tasks keep their host object references.
            _merge_mdns_evidence_into(self.report, persisted)

    async def probe(self, http, host, port):
        async with self.probes:
            address = host["address"]
            authority = f"[{address}]" if ":" in address else address
            origin = f"http://{authority}:{port}"
            previous_services = [
                service
                for service in host.get("services", [])
                if service.get("port") == port
            ]
            previous_by_surface = {
                str(service.get("surface_id") or service.get("origin")): service
                for service in previous_services
            }
            previous = previous_services[0] if previous_services else {}
            previous_mdns = next(
                (
                    service
                    for service in previous_services
                    if service.get("discovery_source") == "mdns"
                ),
                {},
            )
            if (
                self.policy.get("inspect_all_open_ports")
                or port in self.policy.get("http_ports", [])
                or any(
                    service.get("catalog_tracked", False)
                    for service in previous_services
                )
            ):
                # A prior classified surface is already inside the discovery
                # evidence boundary. Refresh it even when its unusual port is
                # not part of the broad HTTP-port policy.
                results = await inspect_transports(http, authority, port)
            else:
                results = [
                    {
                        "origin": origin,
                        "surface_id": origin,
                        "status": "inspection_required",
                        "protocol": "unverified",
                        "base_url": "",
                        "compatible_base_url": "",
                        "models": [],
                        "capabilities": [],
                        "detail": "Open TCP port. Enable HTTP inspection for this port in Settings or connect its model endpoint explicitly.",
                    }
                ]
            checked_at = time.time()
            for result in results:
                surface_id = str(result.get("surface_id") or result.get("origin"))
                prior = previous_by_surface.get(surface_id, previous)
                result.update(port=port, checked_at=checked_at, surface_id=surface_id)
                if prior.get("discovery_source") or previous_mdns.get(
                    "discovery_source"
                ):
                    result["discovery_source"] = prior.get(
                        "discovery_source"
                    ) or previous_mdns["discovery_source"]
                # The historical field name also retains native model inventory
                # evidence. It controls safe metadata refreshes, never engine
                # registration or route eligibility.
                result["catalog_tracked"] = prior.get(
                    "catalog_tracked", False
                ) or result["status"] in {
                    "model_service",
                    "model_surface",
                    "native_inventory",
                    "gateway",
                }
            if any(
                result["status"] not in {"unidentified", "inspection_required"}
                for result in results
            ):
                host.update(
                    status="up",
                    evidence="Web service response",
                    seen_at=checked_at,
                )
            host["services"] = [
                service
                for service in host.get("services", [])
                if service.get("port") != port
            ] + results
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
                    and s["status"]
                    in {"model_service", "model_surface", "native_inventory", "gateway"}
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
                    host.update(status="up", evidence="Web service response")
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
            ports=policy["port_range"],
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
            await self.pass_scan(http, policy["targets"], policy["port_range"])
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
                observed = catalog_refresh_targets(self.report)
                for offset in range(0, len(observed), 8):
                    await asyncio.gather(
                        *(
                            self.probe(http, host, port)
                            for host, port in observed[offset : offset + 8]
                        )
                    )
