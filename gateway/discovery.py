from __future__ import annotations

import asyncio
import ipaddress
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from .adapters import CATALOG_ADAPTERS
from .contracts import EngineStatus, EngineView, ModelView
from .network.announcements import listen
from .network.protocols import is_gateway_catalog, unavailable_reason
from .network.report import mutate_report, read_json, read_report
from .network.transports import inspect_transports
from .routing import matches
from .schema import Engine, declared_inventory_signature
from .store import Conflict, DeclaredProofToken, Store


@dataclass
class Observation:
    identity_locked: bool = field(default=False, init=False)
    engine_id: str
    models: list[ModelView] = field(default_factory=list)
    status: EngineStatus = "checking"
    checked_at: float = 0
    observed_at: float = 0
    latency_ms: float | None = None
    error: str = ""
    inflight: int = 0
    circuit_until: float = 0
    last_success: float | None = None
    declared_inventory_signature: str | None = None
    declared_inventory_proof_revision: int | None = None
    probe_generation: int = 0


def _hostname_alias(base_url: str, hostname: str | None) -> str | None:
    """Build the reported host name at the endpoint's exact port and path."""
    value = (hostname or "").strip().rstrip(".")
    if not value or any(char.isspace() or char in "/\\" for char in value):
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return None
    try:
        parts = urlsplit(base_url)
        host = f"[{value}]" if ":" in value else value
        netloc = host + (f":{parts.port}" if parts.port is not None else "")
        return Engine.validate_url(
            urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        )
    except ValueError:
        return None


class DiscoveryService:
    def __init__(self, store: Store, http: httpx.AsyncClient):
        self.store = store
        self.http = http
        self.observations: dict[str, Observation] = {}
        self.scan_lock = asyncio.Lock()
        self.probe_limit = asyncio.Semaphore(12)
        self.scanning = False
        self.last_scan = 0.0
        self.mdns_error = ""
        self.scan_error = ""
        self.mdns_candidates: set[str] = set()
        self.pending: dict[str, dict] = {}

    def observation(self, engine_id: str) -> Observation:
        return self.observations.setdefault(engine_id, Observation(engine_id))

    def headers(self, engine: Engine) -> dict[str, str]:
        key = self.store.secret(engine.id)
        return {"Authorization": f"Bearer {key}"} if key else {}

    @staticmethod
    def _model_view(
        engine: Engine, model_id: str, row: dict | None = None
    ) -> ModelView:
        """Build one routeable model record from catalog or declared evidence."""
        override = engine.model_settings.get(model_id)
        capabilities = list(engine.capabilities)
        if row is not None:
            stated = row.get("capabilities")
            if isinstance(stated, dict):
                capabilities = [k for k in capabilities if stated.get(k) is not False]
                capabilities = list(
                    dict.fromkeys(
                        capabilities
                        + [
                            k
                            for k, value in stated.items()
                            if value is True
                            and k in {"text", "tools", "vision", "streaming"}
                        ]
                    )
                )
            if isinstance(stated, list):
                capabilities = [value for value in stated if isinstance(value, str)]
            context = row.get("context_length") or row.get("max_model_len")
        else:
            context = None
        if override:
            capabilities = override.capabilities
            if override.context_length:
                context = override.context_length
        result: ModelView = {
            "id": model_id,
            "capabilities": capabilities,
            "context_length": context
            if type(context) is int and context > 0
            else None,
            "enabled": (override.enabled if override else True)
            and matches(model_id, engine.model_patterns)
            and (row is None or not unavailable_reason(row)),
        }
        if row is not None and isinstance(row.get("loaded"), bool):
            result["loaded"] = row["loaded"]
        return result

    def declared_model_views(self, engine: Engine) -> list[ModelView]:
        return [
            self._model_view(engine, model_id) for model_id in engine.declared_models
        ]

    def _refresh_declared_inventory(self, engine: Engine, obs: Observation) -> None:
        signature = declared_inventory_signature(engine)
        proof_revision, last_success = self.store.declared_inventory_evidence(
            engine.id, signature
        )
        if (
            obs.declared_inventory_signature != signature
            or obs.declared_inventory_proof_revision != proof_revision
        ):
            # A new endpoint or inventory has not inherited the prior
            # endpoint's completion evidence. A state revision also captures
            # credential, failure, deletion, and in-process source changes.
            obs.declared_inventory_signature = signature
            obs.declared_inventory_proof_revision = proof_revision
            obs.last_success = last_success
            obs.checked_at = 0
            obs.observed_at = 0
            obs.latency_ms = None
            obs.error = ""
        obs.models = self.declared_model_views(engine)
        obs.status = "available" if obs.last_success else "configured"

    async def record_success(
        self,
        engine: Engine,
        obs: Observation,
        token: DeclaredProofToken | None = None,
    ) -> bool:
        """Record completed traffic without treating configured IDs as a catalog."""
        succeeded_at = time.time()
        if engine.model_inventory_source == "declared":
            if token is None:
                return False
            try:
                receipt = await asyncio.to_thread(
                    self.store.record_declared_inventory_success,
                    token,
                    succeeded_at,
                )
            except (OSError, sqlite3.Error):
                # The completion response remains valid for this caller, but
                # unavailable durable state must not be presented as health.
                return False
            if receipt is None:
                return False
        obs.last_success = succeeded_at
        obs.status = "available"
        obs.error = ""
        obs.circuit_until = 0
        if engine.model_inventory_source == "declared":
            obs.declared_inventory_signature = declared_inventory_signature(engine)
            # Use the mutation's revision, not a later global value. If another
            # request changed the proof after this callback committed, views()
            # will reload durable truth instead of preserving a stale status.
            obs.declared_inventory_proof_revision = receipt.revision
        return True

    async def record_failure(
        self,
        engine: Engine,
        obs: Observation,
        reason: str,
        token: DeclaredProofToken | None = None,
    ) -> bool:
        """Record a disproven completion contract without hiding route fallback."""
        if engine.model_inventory_source == "declared":
            if token is None:
                return False
            try:
                receipt = await asyncio.to_thread(
                    self.store.clear_declared_inventory_success, token
                )
            except (OSError, sqlite3.Error):
                return False
            if receipt is None:
                return False
            obs.declared_inventory_signature = declared_inventory_signature(engine)
            obs.declared_inventory_proof_revision = receipt.revision
        obs.last_success = None
        obs.status = "unavailable"
        obs.error = reason
        obs.circuit_until = time.time() + engine.failure_cooldown_seconds
        return True

    async def probe(self, engine: Engine) -> list[ModelView]:
        if engine.model_inventory_source == "declared":
            return self.declared_model_views(engine)
        body = await CATALOG_ADAPTERS[engine.catalog_protocol].read(
            self.http, engine.base_url, self.headers(engine)
        )
        if engine.source in {
            "network",
            "mDNS",
            "discovery",
            "registration",
        } and is_gateway_catalog(body):
            raise ValueError(
                "Discovered relay catalog requires an explicit connection policy"
            )
        models = []
        for row in body["data"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not row["id"].strip()
                or len(row["id"]) > 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in row["id"])
            ):
                continue
            models.append(self._model_view(engine, row["id"], row))
        if not models:
            raise ValueError("No enabled models are advertised by this endpoint")
        return models

    async def refresh_engine(self, engine: Engine):
        obs = self.observation(engine.id)
        obs.probe_generation += 1
        generation = obs.probe_generation

        def current_probe():
            return (
                generation == obs.probe_generation
                and next(
                    (e for e in self.store.config().engines if e.id == engine.id), None
                )
                == engine
            )

        if engine.model_inventory_source == "declared":
            if current_probe():
                # Declared inventory is configuration, never a disguised
                # catalog probe. A successful proxied request is the first
                # evidence that this exact endpoint and model inventory works.
                self._refresh_declared_inventory(engine, obs)
            return

        started = time.monotonic()
        try:
            async with self.probe_limit:
                models = await self.probe(engine)
            # A removed or edited endpoint cannot be restored by an older probe.
            if not current_probe():
                return
            obs.models = models
            obs.observed_at = time.time()
            obs.status = "draining" if engine.draining else "available"
            obs.error = ""
            obs.latency_ms = round((time.monotonic() - started) * 1000)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError) as exc:
            if not current_probe():
                return
            obs.status = "offline"
            obs.models = []
            # Only class/status is retained. Upstream bodies and credentials are not logged.
            if isinstance(exc, httpx.InvalidURL):
                obs.error = "Endpoint URL is invalid"
            elif isinstance(exc, httpx.HTTPStatusError):
                obs.error = f"Catalog HTTP {exc.response.status_code}"
            elif isinstance(exc, ValueError):
                obs.error = str(exc)
            else:
                obs.error = "Endpoint did not answer"
        finally:
            if current_probe():
                obs.checked_at = time.time()

    async def refresh(self):
        await self.consume_network()
        config = self.store.config()
        await asyncio.gather(*(self.refresh_engine(e) for e in config.engines))

    def views(self) -> list[EngineView]:
        config = self.store.config()
        now = time.time()
        result = []
        for engine in config.engines:
            obs = self.observation(engine.id)
            status = obs.status
            models = obs.models
            if engine.model_inventory_source == "declared":
                self._refresh_declared_inventory(engine, obs)
                status = obs.status
                models = obs.models
            if not engine.enabled:
                status = "disabled"
            elif not engine.model_patterns:
                status = "unconfigured"
            elif (
                engine.model_inventory_source == "declared"
                and obs.last_success
                and now - obs.last_success > config.discovery.stale_seconds
            ):
                # The exact configured inventory remains routeable, but an
                # old request is not current engine-health evidence.
                status = "configured"
            elif (
                obs.observed_at
                and now - obs.observed_at > config.discovery.stale_seconds
            ):
                status, models = "stale", []
            elif obs.circuit_until > now:
                status = "unavailable"
            elif engine.draining or obs.identity_locked:
                status = "draining"
            result.append(
                {
                    **engine.model_dump(),
                    "has_credential": bool(self.store.secret(engine.id)),
                    "models": models,
                    "status": status,
                    "checked_at": obs.checked_at,
                    "observed_at": obs.observed_at,
                    "latency_ms": obs.latency_ms,
                    "error": obs.error,
                    "inflight": obs.inflight,
                    "last_success": obs.last_success,
                }
            )
        return result

    def pending_views(self) -> list[dict]:
        config = self.store.config()
        registered = {url for e in config.engines for url in e.endpoint_urls} | set(
            config.discovery.ignored_urls
        )
        expiry = max(
            config.discovery.interval_seconds * 2, config.discovery.stale_seconds
        )
        return [
            p
            for url, p in self.pending.items()
            if url not in registered and time.time() - p["seen_at"] <= expiry
        ]

    async def trusted_host(self, hostname: str) -> bool:
        policy = self.store.config().discovery
        targets = policy.targets
        try:
            if policy.include_loopback and ipaddress.ip_address(hostname).is_loopback:
                return True
        except ValueError:
            pass
        if hostname in targets:
            return True
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
            ips = {ipaddress.ip_address(row[4][0]) for row in addresses}
        except (OSError, ValueError):
            return False
        for target in targets:
            try:
                net = ipaddress.ip_network(target, strict=False)
                if ips and all(ip in net for ip in ips):
                    return True
            except ValueError:
                try:
                    configured = await asyncio.to_thread(
                        socket.getaddrinfo, target, None
                    )
                    if ips and ips <= {
                        ipaddress.ip_address(row[4][0]) for row in configured
                    }:
                        return True
                except (OSError, ValueError):
                    pass
        return False

    async def discover_url(
        self,
        url: str,
        source="discovery",
        capabilities=None,
        completion_paths=None,
        name=None,
        catalog_protocol="openai",
        hostname=None,
    ) -> str | None:
        reported_hostname = (hostname or "").strip().rstrip(".") or None
        display_name = (
            reported_hostname or name or urlsplit(url).hostname or "Discovered engine"
        )
        alias = _hostname_alias(url, reported_hostname)
        try:
            candidate = Engine(
                name=display_name,
                base_url=url,
                aliases=[alias] if alias else [],
                source=source,
                name_source="discovered" if source != "manual" else "operator",
                catalog_protocol=catalog_protocol,
                capabilities=capabilities
                if capabilities is not None
                else ["text", "streaming"],
                **(
                    {"completion_paths": completion_paths}
                    if completion_paths is not None
                    else {}
                ),
            )
        except ValueError:
            return None
        config = self.store.config()
        if source != "manual" and not await self.trusted_host(
            urlsplit(candidate.base_url).hostname or ""
        ):
            return None
        if candidate.base_url in config.discovery.ignored_urls:
            self.pending.pop(candidate.base_url, None)
            return None
        existing = next(
            (e for e in config.engines if candidate.base_url in e.endpoint_urls), None
        )
        if existing:
            self.pending.pop(candidate.base_url, None)
            changed = False
            if alias and alias not in existing.endpoint_urls:
                existing.aliases.append(alias)
                changed = True
            if (
                reported_hostname
                and existing.name_source == "discovered"
                and existing.name != reported_hostname
            ):
                existing.name = reported_hostname
                changed = True
            if (
                completion_paths is not None
                and existing.source != "manual"
                and existing.completion_paths != candidate.completion_paths
            ):
                # A network-owned engine follows current same-base protocol
                # evidence. Manual engines retain their compatibility default
                # or an operator-selected contract.
                existing.completion_paths = candidate.completion_paths
                changed = True
            if changed:
                try:
                    await asyncio.to_thread(self.store.save, config)
                except Conflict:
                    return None
            return existing.id
        # Discovery and registration admission must carry exact operation
        # evidence from the classifier. A direct catalog probe cannot safely
        # restore the old assumption that every OpenAI-shaped service accepts
        # both OpenAI completion operations. Explicit operator-created engines
        # remain the only source allowed to use the compatibility default.
        # Check this only after resolving an existing endpoint so a harmless
        # rediscovery cannot erase or duplicate a previously saved engine.
        if source != "manual" and completion_paths is None:
            return None
        try:
            async with self.probe_limit:
                models = await self.probe(candidate)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
            return None
        config = self.store.config()
        if candidate.base_url in config.discovery.ignored_urls:
            return None
        if not config.discovery.auto_register:
            self.pending[candidate.base_url] = {
                "name": candidate.name,
                "base_url": candidate.base_url,
                "models": models,
                "capabilities": candidate.capabilities,
                "completion_paths": candidate.completion_paths,
                "catalog_protocol": candidate.catalog_protocol,
                "seen_at": time.time(),
            }
            return None
        existing = next(
            (e for e in config.engines if candidate.base_url in e.endpoint_urls), None
        )
        if existing:
            changed = False
            if alias and alias not in existing.endpoint_urls:
                existing.aliases.append(alias)
                changed = True
            if (
                reported_hostname
                and existing.name_source == "discovered"
                and existing.name != reported_hostname
            ):
                existing.name = reported_hostname
                changed = True
            if (
                completion_paths is not None
                and existing.source != "manual"
                and existing.completion_paths != candidate.completion_paths
            ):
                existing.completion_paths = candidate.completion_paths
                changed = True
            if changed:
                try:
                    await asyncio.to_thread(self.store.save, config)
                except Conflict:
                    return None
            return existing.id
        self.pending.pop(candidate.base_url, None)
        config.engines.append(candidate)
        try:
            await asyncio.to_thread(self.store.save, config)
        except Conflict:
            return None
        obs = self.observation(candidate.id)
        obs.models, obs.status = models, "available"
        obs.checked_at = obs.observed_at = time.time()
        return candidate.id

    async def consume_network(self):
        report = await asyncio.to_thread(read_report, self.store.discovery_directory)
        if not self.store.config().discovery.enabled:
            manual = await asyncio.to_thread(
                read_json, self.store.discovery_directory / "request.json", {}
            )
            if not manual.get("id") or manual["id"] != report.get("job_id"):
                return
        for host in report.get("hosts", []):
            for service in host.get("services", []):
                # A catalog identifies a model but not necessarily which
                # completion request it accepts. Old reports have no exact
                # operation evidence, so preserve them for review rather than
                # inventing a compatibility contract during auto-registration.
                completion_paths = service.get("completion_paths")
                if (
                    service.get("registration_eligible") is True
                    and isinstance(completion_paths, list)
                    and completion_paths
                ):
                    await self.discover_url(
                        service["base_url"],
                        "network",
                        service.get("capabilities"),
                        completion_paths=completion_paths,
                        catalog_protocol=service.get(
                            "catalog_adapter", service.get("protocol", "openai")
                        ),
                        hostname=host.get("name"),
                    )

    async def record_mdns_services(self, candidate: str, services: list[dict]) -> None:
        """Retain mDNS evidence in the shared network report for operator review.

        An mDNS advertisement proves only that an address and port were
        announced.  Every classified API surface is kept even when it lacks a
        model catalog or OpenAI transport, so the Network view can show the
        operator exactly what was found instead of silently dropping it.
        """
        try:
            parsed = urlsplit(candidate)
            address, port = parsed.hostname, parsed.port
        except ValueError:
            return
        if not address or port is None:
            return
        checked_at = time.time()

        def record(report: dict) -> None:
            hosts = report.setdefault("hosts", [])
            host = next(
                (row for row in hosts if row.get("address") == address), None
            )
            if host is None:
                host = {
                    "address": address,
                    "name": "",
                    "ports": [],
                    "services": [],
                    "scope": "mdns",
                    "status": "up",
                    "evidence": "mDNS service announcement",
                    "scan_complete": False,
                }
                hosts.append(host)
            host.setdefault("ports", [])
            host.setdefault("services", [])
            host.setdefault("scope", "mdns")
            host["status"] = "up"
            host["seen_at"] = checked_at
            if port not in host["ports"]:
                host["ports"] = sorted([*host["ports"], port])
            previous = {
                str(row.get("surface_id") or row.get("origin")): row
                for row in host["services"]
                if row.get("port") == port
            }
            observed = []
            for service in services:
                surface = dict(service)
                origin = surface.get("origin")
                if not isinstance(origin, str) or not origin:
                    origin = candidate
                surface_id = str(surface.get("surface_id") or origin)
                prior = previous.get(surface_id, {})
                surface.update(
                    origin=origin,
                    surface_id=surface_id,
                    port=port,
                    checked_at=checked_at,
                    discovery_source="mdns",
                    catalog_tracked=prior.get("catalog_tracked", False)
                    or (
                        surface.get("status")
                        in {
                            "model_service",
                            "model_surface",
                            "native_inventory",
                            "gateway",
                        }
                    ),
                )
                observed.append(surface)
            observed_ids = {surface["surface_id"] for surface in observed}
            host["services"] = [
                row
                for row in host["services"]
                if str(row.get("surface_id") or row.get("origin")) not in observed_ids
                and not (
                    row.get("discovery_source") == "mdns"
                    and row.get("port") == port
                )
            ] + observed
            report["updated_at"] = checked_at

        await asyncio.to_thread(mutate_report, self.store.discovery_directory, record)

    async def inspect_mdns_candidate(self, candidate: str) -> None:
        """Classify one trusted mDNS address on both bounded web transports."""
        try:
            parsed = urlsplit(candidate)
            hostname, port = parsed.hostname or "", parsed.port
        except ValueError:
            return
        if not hostname or port is None or not await self.trusted_host(hostname):
            return
        authority = f"[{hostname}]" if ":" in hostname else hostname
        services = await inspect_transports(self.http, authority, port)
        await self.record_mdns_services(candidate, services)
        for service in services:
            base_url = service.get("base_url")
            if not (
                service.get("registration_eligible") is True
                and isinstance(base_url, str)
                and base_url
            ):
                continue
            completion_paths = service.get("completion_paths")
            if not isinstance(completion_paths, list) or not completion_paths:
                # Keep the inspected mDNS surface for the operator. Its model
                # catalog alone cannot authorize a guessed completion path.
                continue
            await self.discover_url(
                base_url,
                "mDNS",
                service.get("capabilities"),
                completion_paths=completion_paths,
                name=service.get("name"),
                catalog_protocol=service.get(
                    "catalog_adapter", service.get("protocol", "openai")
                ),
            )

    async def scan(self):
        async with self.scan_lock:
            self.scanning = True
            try:
                await self.consume_network()
                self.scan_error = ""
                for url in list(self.mdns_candidates):
                    await self.inspect_mdns_candidate(url)
            except (OSError, ValueError, RuntimeError) as error:
                self.scan_error = str(error)
            finally:
                self.scanning = False
                self.last_scan = time.time()

    async def run(self):
        scan_task = None
        mdns_task = None
        try:
            while True:
                config = self.store.config()
                if config.discovery.enabled and config.discovery.mdns:
                    if mdns_task is None or mdns_task.done():
                        self.mdns_error = ""
                        mdns_task = asyncio.create_task(self.mdns())
                elif mdns_task and not mdns_task.done():
                    mdns_task.cancel()
                    await asyncio.gather(mdns_task, return_exceptions=True)
                    self.mdns_candidates.clear()
                await self.refresh()
                if (
                    config.discovery.enabled
                    and time.time() - self.last_scan
                    >= config.discovery.interval_seconds
                    and (scan_task is None or scan_task.done())
                ):
                    scan_task = asyncio.create_task(self.scan())
                await asyncio.sleep(config.discovery.refresh_seconds)
        finally:
            for task in (scan_task, mdns_task):
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def mdns(self):
        try:
            await listen(
                self.mdns_candidates,
                self.trusted_host,
                self.store.config().discovery.max_addresses,
            )
        except (RuntimeError, OSError) as error:
            self.mdns_error = str(error)
