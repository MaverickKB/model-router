from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import callers
from .accounts.api import admin_router, portal_router
from .accounts.devices import warnings as device_warnings
from .accounts.portal import PortalIdentity
from .accounts.principal import derive_principal, level_for
from .adapters.jevos_native import MAX_BODY as NATIVE_BODY_LIMIT
from .discovery import DiscoveryService
from .engine_identity import MergeEngines
from .engine_suggestions import suggestions
from .identity import Identity
from .network.collector import Collector
from .network.report import read_json, read_report, request_scan, write_json
from .network.views import network_view
from .origin_policy import OriginPolicy
from .proxy import Proxy, require_origin_route
from .routing import client_reason, decide, matches, route_requires_caller_key
from .schema import Configuration, Engine
from .security.body_limit import BodyLimit
from .speech import register_speech, speech_decision
from .store import Conflict, Store
from .systemone_native import NativeOwner
from .topology import route_map
from .updates import Updates


def create_app(state_dir: str | None = None, background=True, transport=None, speech_connector=None, native_probe=None, native_exit_probe=None):
    store = Store(state_dir or os.environ.get("MODEL_ROUTER_STATE", "./state"))
    identity = Identity(store)
    origins = OriginPolicy(str(store.directory))
    store.private_event_filter = origins.is_private_event
    http = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
    )
    discovery = DiscoveryService(store, http)
    proxy = Proxy(store, discovery, http, identity)
    proxy.origins = origins
    native = NativeOwner(store, discovery, http, identity, probe=native_probe, exit_probe=native_exit_probe)
    portal = PortalIdentity(store, identity)
    updates = Updates(http)

    async def refresh_source_names(store):
        """Keep the address -> name cache warm, off the request path.

        Every caller row and every scanned host reads this cache. Refreshing it
        here means a lookup can never stall a request, and a slow or absent
        lookup source degrades to an honest blank instead of a hung router.

        Only the lookup itself is guarded. A bug in the assembly above is left
        to surface, because a refresher that dies quietly would leave the cache
        cold and every row an address again, with nothing to show for it.
        """
        while True:
            engines = [
                {"base_url": engine.base_url, "name": engine.name}
                for engine in store.config().engines
            ]
            store.source_names.set_engines(engines)
            addresses = set(await asyncio.to_thread(store.recent_source_addresses))
            addresses.update(
                str(host.get("address") or "")
                for host in (
                    read_report(store.discovery_directory).get("hosts") or []
                )
                if host.get("status") == "up"
            )
            if addresses:
                try:
                    await asyncio.to_thread(store.source_names.refresh, addresses)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            await asyncio.sleep(store.source_names.ttl / 2)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.interrupt_unfinished)
        await native.start()
        tasks = []
        if background:
            tasks.append(asyncio.create_task(discovery.run()))
            tasks.append(asyncio.create_task(refresh_source_names(store)))
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
            await native.close()
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
    app.state.native_owner = native
    app.state.proxy = proxy
    app.state.origins = origins
    app.state.portal = portal
    app.state.updates = updates

    async def identify_caller(request: Request):
        """Observe connection evidence before returning any caller error."""
        origin = origins.classify(request.client.host if request.client else None)
        request.state.origin = origin
        if origin is not None:
            if origins.in_maintenance(origin):
                raise HTTPException(503, {"error": {
                    "message": origin.maintenance_message,
                    "type": "maintenance", "code": "down_for_maintenance"}})
            if origin.allowed_paths is not None and (
                request.scope.get("type") != "http"
                or not origin.path_allowed(request.url.path)
            ):
                raise HTTPException(403, "This endpoint is not available to this origin")
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
            if (client.id in configured or getattr(request.state, "account_id", None))
            and getattr(request.state, "identity_basis", "") != "unassigned"
            else None
        )
        await callers.observe(
            store,
            request,
            observed_policy,
        )
        return client

    register_speech(app, store, discovery, identity, identify_caller, speech_connector)

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
        # Most recent first. Two callers often tie on both name and address --
        # one host running several clients -- so last_seen breaks the tie before
        # the id does, or a row days stale can sit above one seen a minute ago.
        connections = sorted(
            await asyncio.to_thread(store.callers),
            key=lambda c: (
                -float(c.get("last_seen") or 0),
                c["name"],
                c["source_address"],
                c["id"],
            ),
        )
        return {
            "setup_required": store.setup_required,
            "observed_callers": [
                c for c in connections if not origins.is_private_caller(c)
            ],
            "config": config.model_dump(),
            "network": await asyncio.to_thread(
                network_view,
                store.discovery_directory,
                engines,
                limit=0,
                resolved_names=store.source_names.names,
            ),
            "engines": engines,
            "engine_merge_suggestions": suggestions(engines),
            "route_map": route_map(config, engines, connections, store.accounts()),
            "account_levels_in_use": store.level_account_counts(),
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
            ]
            + device_warnings(config, store.devices(), store.accounts())
            + discovery.recovery_warnings(),
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
            resolved_names=store.source_names.names,
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

    @management.get("/updates")
    async def update_status(request: Request, refresh: bool = False):
        await identity.require_operator(request)
        return await updates.status(store.config(), refresh=refresh)

    @management.post("/updates/apply")
    async def apply_update(request: Request):
        await identity.require_operator(request, True)
        body = await request.json()
        tag = str(body.get("tag") or "")
        try:
            result = await updates.apply(store.config(), tag)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except subprocess.CalledProcessError as exc:
            raise HTTPException(
                500, exc.stderr.strip() or "The update command failed"
            ) from exc

        async def restart():
            await asyncio.sleep(0.4)
            updates.restart()

        asyncio.create_task(restart())
        return result

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
        if (
            saved.security.operator_auth_enabled
            and not identity.admin_verifier
            and not os.environ.get("MODEL_ROUTER_ADMIN_TOKEN")
        ):
            identity.install_bootstrap_key()
        if setup_required or old.security != saved.security:
            return await identity.new_session(request, saved.model_dump())
        return saved.model_dump()

    @management.put("/engines/{engine_id}/credential")
    async def credential(engine_id: str, request: Request):
        await identity.require_operator(request, True)
        engine = next((e for e in store.config().engines if e.id == engine_id), None)
        if not engine:
            raise HTTPException(404, "Engine does not exist")
        body = await request.json()
        value = str(body.get("key", ""))
        if "\n" in value or "\r" in value:
            raise HTTPException(422, "Credential must be one line")
        kind = body.get("type", "static")
        if kind not in {"static", "xai_oauth", "codex_oauth"}:
            raise HTTPException(422, "Unknown credential type")
        if engine.credential_type != kind:
            old = store.config()
            engine = next(e for e in old.engines if e.id == engine_id)
            engine.credential_type = kind
            await asyncio.to_thread(store.save, old)
        await asyncio.to_thread(store.set_secret, engine_id, value)
        await discovery.refresh_engine(engine)
        return {"has_credential": bool(value), "credential_type": kind}

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

    @management.put("/caller-sources/{source_key}/name")
    async def name_caller_source(source_key: str, request: Request):
        await identity.require_operator(request, True)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Caller source name must be a JSON object")
        try:
            return await asyncio.to_thread(
                store.set_caller_source_name,
                source_key,
                str(body.get("name") or ""),
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc))

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
        if client is None and body.get("account_id"):
            account = store.account_snapshot(str(body["account_id"]))
            level = level_for(config, account["level_id"]) if account else None
            if account and level:
                client = derive_principal(account, level)
        if not client:
            raise HTTPException(422, "Choose a configured client or account")
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
        chat_views = [e for e in views if e["protocol"] != "xai_tts"]
        for route in config.routes:
            decision = decide(
                config,
                chat_views,
                client,
                {"model": route.name},
                consider_capacity=False,
                caller_key_present=caller_key_present,
            )
            serving = "chat"
            if not decision["candidates"]:
                decision = decide(
                    config,
                    chat_views,
                    client,
                    {"model": route.name, "questions": []},
                    consider_capacity=False,
                    caller_key_present=caller_key_present,
                )
                serving = "systemone"
                if decision["candidates"] and all(
                    next(e for e in views if e["id"] == c["engine_id"])["protocol"] == "jevos_native"
                    for c in decision["candidates"]
                ):
                    serving = "systemone-native"
            speech_route = route.name == "cairn_voice"
            if speech_route:
                decision = speech_decision(config, views, client, request, consider_capacity=False)
                serving = "speech"
            recovering_alias = (
                route.enabled and route.recovery_seconds != 0 and route.primary.hosts
                and client.enabled and matches(route.name, client.route_names)
                and decision.get("status") not in {401, 403}
            )
            if recovering_alias and not decision["candidates"] and not speech_route:
                # A speech route has no chat fallback; keep declaring speech and
                # report it unavailable rather than advertising a chat row.
                serving = "chat"
            if decision["candidates"] or recovering_alias:
                row = {
                    "id": route.name,
                    "object": "model",
                    "owned_by": "model-router",
                    "description": route.purpose,
                }
                if serving != "chat":
                    row["serving"] = serving
                if recovering_alias:
                    row["available"] = bool(decision["candidates"])
                if speech_route and not decision["candidates"]:
                    row["available"] = False
                selected_engines = set()
                contexts = []
                capacity = 0
                for candidate in decision["candidates"]:
                    engine = next(engine for engine in views if engine["id"] == candidate["engine_id"])
                    model = next(model for model in engine["models"] if model["id"] == candidate["model"])
                    contexts.append(model.get("context_length"))
                    if engine["id"] not in selected_engines:
                        selected_engines.add(engine["id"])
                        capacity += engine["max_inflight"]
                if recovering_alias and contexts and all(contexts):
                    row["context_length"] = min(contexts)
                if recovering_alias and capacity:
                    row["max_concurrency"] = capacity
                data.append(row)
        seen = {row["id"] for row in data}
        for engine in views if client.allow_direct_models else []:
            if engine["status"] != "available" or engine["protocol"] == "xai_tts":
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
        elif not identity.origin_allowed(request):
            # The same browser-origin test as every other state-changing
            # management POST, applied even when operator auth is off.
            raise HTTPException(403, "Settings require a same-origin operator request")
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
        if isinstance(payload, dict) and "questions" in payload:
            raise HTTPException(
                400, "Chat completions do not accept System One questions"
            )
        return await proxy.dispatch_connected(request, payload, client)

    @app.post("/v1/systemone")
    async def systemone(request: Request):
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
        if not isinstance(payload, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        questions = payload.get("questions")
        if isinstance(questions, dict):
            if len(raw) > NATIVE_BODY_LIMIT:
                raise HTTPException(413, "Native request exceeds 8 KiB")
            # Native dispatch resolves its route from payload["model"] exactly
            # as the proxy does, so the origin allowlist must gate it here too.
            route_name = payload.get("model")
            if isinstance(route_name, str) and route_name:
                require_origin_route(request, route_name)
            return await native.dispatch(request, payload, client)
        if not isinstance(questions, list) or not questions:
            raise HTTPException(400, "questions must be a nonempty array")
        if payload.get("stream"):
            raise HTTPException(400, "System One routes do not stream")
        if "messages" in payload:
            raise HTTPException(400, "System One routes do not accept chat messages")
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

    management.include_router(
        admin_router(store, identity, discovery, proxy, portal=portal)
    )
    management.include_router(
        portal_router(store, identity, portal, discovery, proxy), prefix="/portal"
    )
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

    # The portal is the same build in path mode; the page chooses at load time.
    @app.get("/portal")
    @app.get("/portal/{path:path}")
    async def portal_page(path: str = ""):
        return await index()

    return app
