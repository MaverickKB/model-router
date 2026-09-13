from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from .adapters import CATALOG_ADAPTERS
from .contracts import EngineStatus, EngineView, ModelView
from .network.announcements import listen
from .network.protocols import is_gateway_catalog, unavailable_reason
from .network.report import read_json, read_report
from .routing import matches
from .schema import Engine
from .store import Conflict, Store


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

    async def probe(self, engine: Engine) -> list[ModelView]:
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
            model_id = row["id"]
            override = engine.model_settings.get(model_id)
            capabilities = list(engine.capabilities)
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
            if override:
                capabilities = override.capabilities
            context = row.get("context_length") or row.get("max_model_len")
            if override and override.context_length:
                context = override.context_length
            models.append(
                {
                    "id": model_id,
                    "capabilities": capabilities,
                    "context_length": context
                    if type(context) is int and context > 0
                    else None,
                    "enabled": (override.enabled if override else True)
                    and matches(model_id, engine.model_patterns)
                    and not unavailable_reason(row),
                    **(
                        {"loaded": row["loaded"]}
                        if isinstance(row.get("loaded"), bool)
                        else {}
                    ),
                }
            )
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
            if not engine.enabled:
                status = "disabled"
            elif not engine.model_patterns:
                status = "unconfigured"
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
            if changed:
                try:
                    await asyncio.to_thread(self.store.save, config)
                except Conflict:
                    return None
            return existing.id
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
                if (
                    service.get("status") == "model_service"
                    and service.get("protocol") == "openai"
                    and not service.get("catalog_conflict")
                    and service.get("models")
                ):
                    await self.discover_url(
                        service["base_url"],
                        "network",
                        service.get("capabilities"),
                        catalog_protocol=service.get(
                            "catalog_adapter", service.get("protocol", "openai")
                        ),
                        hostname=host.get("name"),
                    )

    async def scan(self):
        async with self.scan_lock:
            self.scanning = True
            try:
                await self.consume_network()
                self.scan_error = ""
                for url in list(self.mdns_candidates):
                    if await self.trusted_host(urlsplit(url).hostname or ""):
                        await self.discover_url(url, "mDNS")
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
