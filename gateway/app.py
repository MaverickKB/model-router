from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import callers
from .discovery import DiscoveryService
from .engine_identity import MergeEngines
from .engine_suggestions import suggestions
from .identity import Identity
from .network.collector import Collector
from .network.report import read_json, read_report, request_scan, write_json
from .network.views import network_view
from .proxy import Proxy
from .routing import client_reason, decide, route_requires_caller_key
from .schema import Configuration, Engine
from .security.body_limit import BodyLimit
from .store import Conflict, Store
from .topology import route_map


def create_app(state_dir: str | None = None, background=True, transport=None):
    store = Store(state_dir or os.environ.get("MODEL_ROUTER_STATE", "./state"))
    identity = Identity(store)
    http = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
    )
    discovery = DiscoveryService(store, http)
    proxy = Proxy(store, discovery, http, identity)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.interrupt_unfinished)
        tasks = []
        if background:
            tasks.append(asyncio.create_task(discovery.run()))
            if (
                os.environ.get("MODEL_ROUTER_DISCOVERY_WORKER", "embedded")
                == "embedded"
            ):
                tasks.append(
                    asyncio.create_task(Collector(store.discovery_directory).run())
                )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()

    app = FastAPI(
        title="Model Router",
        lifespan=lifespan,
        version="0.3.0",
        description="Authenticated, policy-controlled OpenAI-compatible chat routing with optional service discovery",
    )
    management = APIRouter()
    app.add_middleware(BodyLimit)
    app.state.store, app.state.discovery, app.state.identity = (
        store,
        discovery,
        identity,
    )

    async def identify_caller(request: Request):
        """Observe connection evidence before returning any caller error."""
        try:
            client = await identity.identify(request)
        except HTTPException as exc:
            # Rejected authentication still leaves direct connection evidence
            # so the operator can inspect the failed request.
            request.state.identity_basis = "unassigned"
            request.state.caller_key_present = False
            request.state.authentication_error = {
                "status": exc.status_code,
                "detail": str(exc.detail),
            }
            await callers.observe(store, request, None)
            raise
        configured = {policy.id for policy in store.config().clients}
        observed_policy = (
            client
            if client.id in configured
            and getattr(request.state, "identity_basis", "") != "unassigned"
            else None
        )
        await callers.observe(
            store,
            request,
            observed_policy,
        )
        return client

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(request, exc):
        return JSONResponse({"detail": "Request body must be JSON"}, status_code=400)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @management.get("/state")
    async def state(request: Request):
        await identity.require_operator(request)
        config = store.config()
        engines = discovery.views()
        connections = sorted(
            await asyncio.to_thread(store.callers),
            key=lambda c: (c["name"], c["source_address"], c["id"]),
        )
        return {
            "setup_required": store.setup_required,
            "observed_callers": connections,
            "config": config.model_dump(),
            "network": await asyncio.to_thread(
                network_view, store.discovery_directory, engines, limit=0
            ),
            "engines": engines,
            "engine_merge_suggestions": suggestions(engines),
            "route_map": route_map(config, engines, connections),
            "events": await asyncio.to_thread(store.events),
            "clients": [
                {**c.model_dump(), "has_key": store.has_key(c.id)}
                for c in config.clients
            ],
            "discovery": {
                "scanning": discovery.scanning,
                "last_scan": discovery.last_scan,
                "error": discovery.scan_error or discovery.mdns_error,
                "pending": discovery.pending_views(),
            },
            "warnings": [
                f"Route {route.name!r} takes precedence over an observed model with the same name"
                for route in config.routes
                if any(
                    model["id"] == route.name
                    for engine in engines
                    for model in engine["models"]
                )
            ],
            "server_time": time.time(),
            "environment_label": os.environ.get("MODEL_ROUTER_LABEL", ""),
            "operator_url": os.environ.get("MODEL_ROUTER_PUBLIC_URL") or None,
            "base_url": os.environ.get(
                "MODEL_ROUTER_PUBLIC_URL", str(request.base_url).rstrip("/")
            )
            + "/v1",
        }

    @management.get("/network")
    async def network(
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(25, ge=1, le=100),
        query: str = Query("", max_length=200),
        category: str = "All addresses",
    ):
        await identity.require_operator(request)
        return await asyncio.to_thread(
            network_view,
            store.discovery_directory,
            discovery.views(),
            offset=offset,
            limit=limit,
            query=query,
            category=category,
        )

    @management.post("/login")
    async def login(request: Request):
        return await identity.login(request)

    @management.post("/logout")
    async def logout(request: Request):
        return await identity.logout(request)

    @management.post("/operator/key")
    async def rotate_operator_key(request: Request):
        return await identity.rotate_key(request)

    @management.put("/config")
    async def configure(config: Configuration, request: Request):
        await identity.require_operator(request, True)
        for client in config.clients:
            for network in client.source_networks:
                try:
                    ipaddress.ip_network(network, strict=False)
                except ValueError:
                    raise HTTPException(
                        422, "Client networks must be valid IP addresses or CIDRs"
                    )
        old = store.config()
        setup_required = store.setup_required
        try:
            saved = await asyncio.to_thread(store.save, config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        changed = [e for e in saved.engines if e not in old.engines]
        await asyncio.gather(*(discovery.refresh_engine(e) for e in changed))
        if setup_required or old.security != saved.security:
            return await identity.new_session(request, saved.model_dump())
        return saved.model_dump()

    @management.put("/engines/{engine_id}/credential")
    async def credential(engine_id: str, request: Request):
        await identity.require_operator(request, True)
        engine = next((e for e in store.config().engines if e.id == engine_id), None)
        if not engine:
            raise HTTPException(404, "Engine does not exist")
        value = str((await request.json()).get("key", ""))
        if "\n" in value or "\r" in value:
            raise HTTPException(422, "Credential must be one line")
        await asyncio.to_thread(store.set_secret, engine_id, value)
        await discovery.refresh_engine(engine)
        return {"has_credential": bool(value)}

    @management.post("/engines/merge")
    async def merge_engines(body: MergeEngines, request: Request):
        await identity.require_operator(request, True)
        observations = [
            discovery.observation(key) for key in (body.source_id, body.target_id)
        ]
        if any(obs.inflight or obs.identity_locked for obs in observations):
            raise HTTPException(
                409, "Wait for these engines' active requests to finish, then merge"
            )
        for obs in observations:
            obs.identity_locked = True
        work = asyncio.create_task(asyncio.to_thread(store.merge_engines, body))
        try:
            try:
                saved = await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise
            discovery.observations.pop(body.source_id, None)
            target = next(
                engine for engine in saved.engines if engine.id == body.target_id
            )
            await discovery.refresh_engine(target)
            return saved.model_dump()
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        finally:
            for obs in observations:
                obs.identity_locked = False

    @management.post("/clients/{client_id}/key")
    async def issue_key(client_id: str, request: Request):
        await identity.require_operator(request, True)
        if not any(c.id == client_id for c in store.config().clients):
            raise HTTPException(404, "Client does not exist")
        await asyncio.to_thread(store.revoke_keys, client_id)
        return {"key": await asyncio.to_thread(store.issue_key, client_id)}

    @management.post("/engines/{engine_id}/refresh")
    async def refresh_one_engine(engine_id: str, request: Request):
        await identity.require_operator(request, True)
        engine = next((e for e in store.config().engines if e.id == engine_id), None)
        if engine is None:
            raise HTTPException(404, "Engine does not exist")
        await discovery.refresh_engine(engine)
        return {"ok": True}

    @management.delete("/clients/{client_id}/key")
    async def revoke_key(client_id: str, request: Request):
        await identity.require_operator(request, True)
        await asyncio.to_thread(store.revoke_keys, client_id)
        return {"ok": True}

    @management.post("/discover")
    async def discover(request: Request):
        await identity.require_operator(request, True)
        policy = store.config().discovery
        if not policy.targets and not policy.include_loopback:
            raise HTTPException(
                422, "Choose discovery targets in Settings before scanning"
            )
        return await asyncio.to_thread(request_scan, store.discovery_directory)

    @management.delete("/discover")
    async def cancel_discovery(request: Request):
        await identity.require_operator(request, True)
        report = await asyncio.to_thread(read_report, store.discovery_directory)
        pending = await asyncio.to_thread(
            read_json, store.discovery_directory / "request.json", {}
        )
        job_id = (
            pending.get("id")
            if pending.get("id") != report.get("job_id")
            else report.get("job_id")
        )
        await asyncio.to_thread(
            write_json,
            store.discovery_directory / "cancel.json",
            {"id": job_id},
        )
        return {"id": job_id, "status": "cancellation_requested"}

    @management.post("/refresh")
    async def refresh(request: Request):
        await identity.require_operator(request, True)
        await discovery.refresh()
        return {"ok": True}

    @management.post("/explain")
    async def explain(request: Request):
        await identity.require_operator(request, True)
        body = await request.json()
        config = store.config()
        client = next(
            (c for c in config.clients if c.id == body.get("client_id")), None
        )
        if not client:
            raise HTTPException(422, "Choose a configured client")
        return decide(config, discovery.views(), client, body.get("payload", {}))

    @app.get("/health")
    @app.get("/v1/health")
    async def health():
        engines = discovery.views()
        available = [
            e
            for e in engines
            if e["status"] == "available"
            and any(m.get("enabled", True) for m in e["models"])
        ]
        result = {"ok": bool(available), "service": "model-router"}
        return result

    @app.get("/v1/models")
    async def models(request: Request):
        client = await identify_caller(request)
        caller_key_present = getattr(request.state, "caller_key_present", True)
        config, views = store.config(), discovery.views()
        data = []
        for route in config.routes:
            decision = decide(
                config,
                views,
                client,
                {"model": route.name},
                consider_capacity=False,
                caller_key_present=caller_key_present,
            )
            if decision["candidates"]:
                data.append(
                    {
                        "id": route.name,
                        "object": "model",
                        "owned_by": "model-router",
                        "description": route.purpose,
                    }
                )
        seen = {row["id"] for row in data}
        for engine in views if client.allow_direct_models else []:
            if engine["status"] != "available":
                continue
            for model in engine["models"]:
                if (
                    model.get("enabled", True)
                    and model["id"] not in seen
                    and "text" in model["capabilities"]
                    and not client_reason(client, engine, model)
                ):
                    data.append(
                        {**model, "object": "model", "owned_by": engine["name"]}
                    )
                    seen.add(model["id"])
        if (
            not data
            and not caller_key_present
            and any(route.enabled for route in config.routes)
            and all(
                route_requires_caller_key(route)
                for route in config.routes
                if route.enabled
            )
        ):
            raise HTTPException(401, "A caller key is required for every enabled route")
        return {
            "object": "list",
            "data": data,
            "model_serving": {"version": 1, "kind": "router"},
        }

    @app.post("/v1/gateway/register")
    async def register(request: Request):
        if store.config().security.operator_auth_enabled:
            await identity.require_operator(request, True)
        identity.registration_limit.take(
            request.client.host if request.client else "unknown"
        )
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Registration must be a JSON object")
        try:
            url = Engine(
                name="Registration",
                base_url=str(body.get("base_url") or body.get("engine_url") or ""),
            ).base_url
        except ValueError:
            raise HTTPException(422, "Registration requires a valid model endpoint URL")
        host = urlsplit(url).hostname
        source = request.client.host if request.client else ""
        if (
            not host
            or not await discovery.trusted_host(host)
            or not await discovery.trusted_host(source)
        ):
            raise HTTPException(
                403, "Registration is outside the configured discovery scope"
            )
        result = await discovery.discover_url(url, "registration")
        if result:
            return {
                "ok": True,
                "engine_id": result,
                "note": "Registration does not imply inference readiness",
            }
        if any(item["base_url"] == url for item in discovery.pending_views()):
            return JSONResponse(
                {
                    "ok": True,
                    "status": "pending",
                    "note": "Operator connection is required",
                },
                status_code=202,
            )
        raise HTTPException(
            503, "The endpoint did not provide a registerable model catalog"
        )

    @app.post("/v1/chat/completions")
    @app.post("/v1/completions")
    @app.post("/chat/completions")
    @app.post("/completions")
    async def completion(request: Request):
        client = await identify_caller(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024 * 1024:
                raise HTTPException(413, "Request exceeds 16 MiB")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "Request body must be JSON")
        return await proxy.dispatch_connected(request, payload, client)

    @management.post("/try-route")
    async def try_route(request: Request):
        await identity.require_operator(request, True)
        body = await request.json()
        client = next(
            (c for c in store.config().clients if c.id == body.get("client_id")), None
        )
        if not client:
            raise HTTPException(422, "Choose a client")
        request.state.identity_basis = "operator_test"
        await callers.observe(store, request, client)
        payload = {
            "model": body.get("route"),
            "messages": [
                {
                    "role": "user",
                    "content": str(
                        body.get("message") or "Reply with a short greeting."
                    ),
                }
            ],
            "max_tokens": 64,
        }
        return await proxy.dispatch_connected(request, payload, client)

    app.include_router(management, prefix="/api/v1")
    app.include_router(management, prefix="/api", include_in_schema=False)

    assets = Path(
        os.environ.get(
            "MODEL_ROUTER_UI", str(Path(__file__).resolve().parents[1] / "dist")
        )
    )
    if (assets / "assets").exists():
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico():
        if (assets / "favicon.ico").exists():
            return FileResponse(assets / "favicon.ico", media_type="image/x-icon")
        return JSONResponse({"detail": "Favicon is not built"}, status_code=404)

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon_svg():
        if (assets / "favicon.svg").exists():
            return FileResponse(assets / "favicon.svg", media_type="image/svg+xml")
        return JSONResponse({"detail": "Favicon is not built"}, status_code=404)

    @app.get("/")
    async def index():
        if (assets / "index.html").exists():
            return FileResponse(assets / "index.html")
        return JSONResponse(
            {"detail": "Build the interface with npm run build"}, status_code=503
        )

    return app
