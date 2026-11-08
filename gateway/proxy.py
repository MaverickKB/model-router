"""Forward requests through the current policy; own admission, fallback and cancellation."""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import httpx
from anyio import CancelScope
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
from starlette.background import BackgroundTask

from .accounts.limits import AccountLimits, Refusal, UsageMeter
from .accounts.principal import level_for
from .discovery import DiscoveryService, Observation
from .identity import Identity
from .routing import apply_defaults, decide
from .schema import Client
from .store import Store
from .stream_protocol import CompletionMarker, StreamProgress


# One recovery loop can reject admission thousands of times; keep recent
# evidence only, and count every rejection separately.
ADMISSION_REJECTIONS_KEPT = 50


class ResponseTooLarge(Exception):
    pass


class RecoverableStreamError(Exception):
    """The upstream failed before any answer, reasoning, or tool output."""


def require_origin_route(request: Request, route_name: str):
    origin = getattr(request.state, "origin", None)
    if origin is None:
        return
    if origin.route_allowed(route_name):
        return
    allowed = ", ".join(repr(r) for r in origin.allowed_routes or [])
    raise HTTPException(
        403,
        f"This origin may only use these routes: {allowed}"
        if allowed
        else "No routes are available to this origin",
    )


def validate_payload(payload):
    if (not isinstance(payload, dict) or not isinstance(payload.get("model"), str)
            or not payload["model"].strip()):
        raise HTTPException(400, "A model or route name is required")
    if "messages" in payload and not isinstance(payload["messages"], list):
        raise HTTPException(400, "messages must be an array")


async def read_limited(response: httpx.Response, limit: int) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise ResponseTooLarge
    return bytes(content)


class InflightRequest:
    """Own one admission slot and its upstream connection through stream completion."""

    def __init__(
        self, observation: Observation, failure_cooldown: float, *, background=False
    ):
        self.observation = observation
        self.failure_cooldown = failure_cooldown
        self.response: httpx.Response | None = None
        self.released = False
        self._background = bool(background)
        observation.inflight += 1
        if self._background:
            observation.background_inflight += 1

    @property
    def background(self):
        return self._background

    @classmethod
    def claim(
        cls,
        observation: Observation,
        limit: int,
        failure_cooldown: float,
        *,
        background=False,
        foreground_reserved_slots=0,
    ):
        # This synchronous check-and-increment is atomic on the owning event loop.
        # No database or transport await may enter this reservation boundary.
        if observation.identity_locked or observation.inflight >= limit:
            return None
        if (
            background
            and observation.background_inflight >= limit - foreground_reserved_slots
        ):
            return None
        return cls(observation, failure_cooldown, background=background)

    async def release(self):
        if not self.released:
            self.released = True
            with CancelScope(shield=True):
                try:
                    if self.response is not None:
                        await self.response.aclose()
                finally:
                    self.observation.inflight = max(0, self.observation.inflight - 1)
                    if self._background:
                        self.observation.background_inflight = max(
                            0, self.observation.background_inflight - 1
                        )

    async def relay(self, first, iterator, finish, observe=None, recoverable=False):
        terminal = CompletionMarker()
        progress = StreamProgress()
        status, code = "cancelled", None
        try:
            terminal.feed(first)
            if recoverable:
                progress.feed(first)
            if observe is not None:
                observe(first)
            yield first
            if not terminal.complete:
                async for chunk in iterator:
                    terminal.feed(chunk)
                    if recoverable:
                        progress.feed(chunk)
                    if observe is not None:
                        observe(chunk)
                    yield chunk
                    if terminal.complete:
                        break
            if recoverable and not terminal.complete and not progress.has_output:
                status = "recovering"
                raise RecoverableStreamError("IncompleteStream")
            status, code = "completed", 200
        except httpx.HTTPError as error:
            status, code = "failed", 502
            self.observation.circuit_until = time.time() + self.failure_cooldown
            self.observation.circuit_reason = "transport"
            if recoverable and not progress.has_output:
                status = "recovering"
                raise RecoverableStreamError(type(error).__name__) from error
            yield b'event: error\ndata: {"error":{"message":"Upstream stream interrupted"}}\n\n'
        finally:
            # OpenAI clients close on the terminal event without waiting for
            # HTTP EOF. Preserve that outcome through disconnect and protect
            # the single history write and connection cleanup from cancellation.
            if terminal.complete:
                status, code = "completed", 200
            if status == "completed":
                self.observation.last_success = time.time()
            with CancelScope(shield=True):
                try:
                    await finish(status, code)
                finally:
                    await self.release()


class Proxy:
    def __init__(
        self,
        store: Store,
        discovery: DiscoveryService,
        http: httpx.AsyncClient,
        identity: Identity,
    ):
        self.store, self.discovery, self.http = store, discovery, http
        self.identity = identity
        self.origins = None
        # Open windows are preloaded once; the request path never loads one.
        self.limits = AccountLimits()
        self.limits.ledger.seed(store.open_usage_windows(time.time()))

    async def dispatch(self, request: Request, payload: dict, client: Client):
        validate_payload(payload)
        origin = getattr(request.state, "origin", None)
        if origin is not None:
            require_origin_route(request, payload["model"].strip())
            origin.sanitize(payload)
        config = self.store.config()
        route = next((route for route in config.routes if route.name == payload["model"]), None)
        recovery_seconds = route.recovery_seconds if route and route.enabled else 0
        recovery_enabled = recovery_seconds is None or recovery_seconds > 0
        caller_key_present = getattr(request.state, "caller_key_present", True)
        operator_test = getattr(request.state, "identity_basis", "") == "operator_test"
        decision = decide(
            config,
            self.discovery.views(),
            client,
            payload,
            caller_key_present=caller_key_present,
        )
        background = payload.get("priority") == "background"
        event = getattr(request.state, "router_event", None) or {
            "id": getattr(request.state, "router_request_id", None) or uuid4().hex,
            "ts": time.time(),
            "caller": getattr(request.state, "caller", None),
            "client_id": client.id,
            "client": client.name,
            "requested": payload["model"],
            "status": "routing",
            "attempts": [],
            "decision": decision,
            "stream": bool(payload.get("stream")),
            "request_class": "background" if background else "foreground",
            "revision": config.revision,
        }
        request.state.router_event = event
        if origin is not None:
            event["origin"] = origin.public()
        # Bound before finish exists: finish settles an admission when one was
        # claimed and must stay callable on every earlier exit.
        admission = None
        meter = None
        account_id = getattr(request.state, "account_id", None)
        started = getattr(request.state, "router_started", None) or time.monotonic()
        request.state.router_started = started
        await asyncio.to_thread(self.store.event, event)

        async def finish(status, code=None):
            if status == "recovering":
                event.setdefault("recovering_since", time.time())
            row = None
            if admission is not None and not admission.settled:
                # Settle synchronously before the first await so a cancellation
                # landing here can lose durability, never a slot or reservation.
                usage = meter.result(status, code)
                admission.settle(usage)
                event["usage"] = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "estimated": usage.estimated,
                }
                event["account_id"] = account_id
                row = (
                    account_id,
                    admission.window_start,
                    admission.window_seconds,
                    *usage.row(),
                )
            if event["attempts"] and event["attempts"][-1].get("status") == "waiting":
                event["attempts"][-1]["status"] = status
            event.update(
                status=status,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                http_status=code,
            )

            def persist():
                self.store.event(event)
                if row is not None:
                    self.store.record_usage(*row)

            await asyncio.to_thread(persist)

        async def refuse(decision):
            status = decision.get("status", 503)
            outcome = (
                "denied"
                if status in {401, 403}
                else "failed"
                if 400 <= status < 500
                else "unavailable"
            )
            await finish(outcome, status)
            return JSONResponse(
                {
                    "error": {
                        "message": decision.get(
                            "error", "No allowed, available model matches this request"
                        ),
                        "type": decision.get("error_type", "route_unavailable"),
                        **(
                            {"code": decision["error_code"]}
                            if "error_code" in decision
                            else {}
                        ),
                    },
                    "request_id": event["id"],
                },
                status_code=status,
            )

        async def recover():
            if not recovery_enabled:
                return False
            remaining = 1
            if recovery_seconds is not None:
                deadline = getattr(request.state, "router_recovery_deadline", None)
                if deadline is None:
                    deadline = time.monotonic() + recovery_seconds
                    request.state.router_recovery_deadline = deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            if event["status"] != "recovering":
                event["status"] = "recovering"
                event.setdefault("recovering_since", time.time())
                await asyncio.to_thread(self.store.event, event)
            await asyncio.sleep(min(1, remaining))
            await self.discovery.refresh_route(payload["model"])
            return True

        if not decision["candidates"]:
            while decision.get("status", 503) == 503 and await recover():
                client = await self.identity.identify(request)
                if getattr(request.state, "account_id", None) != account_id:
                    raise HTTPException(403, "Account access was removed")
                decision = decide(self.store.config(), self.discovery.views(), client, payload,
                                  caller_key_present=request.state.caller_key_present)
                event["decision"] = decision
                if decision["candidates"]:
                    break
            if not decision["candidates"]:
                return await refuse(decision)

        if account_id:
            # Account limits are admitted after decide and before any upstream
            # send, then held across every attempt until finish settles them.
            account = self.store.account_snapshot(account_id)
            level = level_for(config, account["level_id"]) if account else None
            if level is None:
                await finish("denied", 403)
                raise HTTPException(403, "Account access was removed")
            outcome = self.limits.admit(
                account_id, level, payload, decision, time.time()
            )
            if isinstance(outcome, Refusal):
                event["limit"] = {
                    "code": outcome.code,
                    "retry_after": outcome.retry_after,
                }
                event["account_id"] = account_id
                await finish("limited", 429)
                return JSONResponse(
                    outcome.body(event["id"]),
                    status_code=429,
                    headers=outcome.headers(),
                )
            admission = outcome
            meter = UsageMeter(payload)

        try:
            attempted = set()
            while True:
                # Re-evaluate the complete policy after each failed attempt.
                try:
                    if operator_test:
                        await self.identity.require_operator(request)
                        current_client = next(
                            (
                                c
                                for c in self.store.config().clients
                                if c.id == client.id
                            ),
                            None,
                        )
                        if current_client is None:
                            raise HTTPException(
                                403, "Test permission policy was removed"
                            )
                    else:
                        current_client = await self.identity.identify(request)
                        # The principal is bound at admission. A request whose
                        # account changed since (a device row or switch toggled,
                        # an address re-registered, or a host that became a
                        # device mid-request) is refused rather than continued
                        # under an admission it never passed or no longer owns.
                        if getattr(request.state, "account_id", None) != account_id:
                            raise HTTPException(403, "Account access was removed")
                except HTTPException as exc:
                    event["decision"] = {
                        "candidates": [],
                        "rejections": [],
                        "status": exc.status_code,
                        "error": exc.detail,
                    }
                    await finish("denied", exc.status_code)
                    raise
                current_config = self.store.config()
                caller_key_present = (
                    True if operator_test else request.state.caller_key_present
                )
                fresh = decide(
                    current_config,
                    self.discovery.views(),
                    current_client,
                    payload,
                    caller_key_present=caller_key_present,
                )
                event.update(
                    client_id=current_client.id,
                    client=current_client.name,
                    decision=fresh,
                    revision=current_config.revision,
                )
                if not fresh["candidates"] and fresh.get("status") and fresh["status"] != 503:
                    return await refuse(fresh)
                candidate = next(
                    (
                        c
                        for c in fresh["candidates"]
                        if (c["engine_id"], c["model"]) not in attempted
                    ),
                    None,
                )
                if candidate is None:
                    if fresh.get("status", 503) == 503 and await recover():
                        attempted.clear()
                        continue
                    break
                attempted.add((candidate["engine_id"], candidate["model"]))
                engine = next(
                    e for e in current_config.engines if e.id == candidate["engine_id"]
                )
                systemone_request = request.url.path.rstrip("/").endswith("systemone")
                if engine.protocol != ("systemone" if systemone_request else "openai"):
                    continue
                obs = self.discovery.observation(engine.id)
                optional_defaults = {
                    k: v
                    for k, v in fresh.get("defaults", {}).items()
                    if k not in engine.unsupported_parameters
                }
                body = apply_defaults(payload, optional_defaults)
                body["model"] = candidate["model"]
                body.pop("session_id", None)
                for field, mapping in engine.value_mappings.items():
                    if isinstance(body.get(field), str):
                        body[field] = mapping.get(body[field], body[field])
                injected = (
                    meter is not None
                    and bool(body.get("stream"))
                    and "stream_options" not in engine.unsupported_parameters
                )
                if injected:
                    # A key holder must not be able to force the estimate path, so
                    # a caller-supplied include_usage is overridden, not honoured.
                    options = body.get("stream_options")
                    body["stream_options"] = {
                        **(options if isinstance(options, dict) else {}),
                        "include_usage": True,
                    }
                    event["stream_options_injected"] = True
                else:
                    # Per attempt: a retry to an engine that cannot take the option
                    # must not inherit the flag from an attempt that was not served.
                    event.pop("stream_options_injected", None)
                if engine.protocol == "systemone":
                    path = "/api/evaluate"
                    body.pop("model", None)
                    body.pop("stream", None)
                    body.pop("stream_options", None)
                elif request.url.path.endswith(
                    "/completions"
                ) and not request.url.path.endswith("/chat/completions"):
                    path = "/completions"
                else:
                    path = "/chat/completions"
                max_inflight = obs.max_inflight or engine.max_inflight
                connection = InflightRequest.claim(
                    obs,
                    max_inflight,
                    engine.failure_cooldown_seconds,
                    background=background,
                    foreground_reserved_slots=engine.foreground_reserved_slots,
                )
                if connection is None:
                    code = (
                        "background_quota_exceeded"
                        if background
                        and not obs.identity_locked
                        and obs.inflight < max_inflight
                        and obs.background_inflight
                        >= max_inflight - engine.foreground_reserved_slots
                        else "engine_busy"
                    )
                    rejections = event.setdefault("admission_rejections", [])
                    rejections.append(
                        {
                            "engine_id": engine.id,
                            "code": code,
                            "request_class": event["request_class"],
                            "inflight": obs.inflight,
                            "background_inflight": obs.background_inflight,
                            "max_inflight": max_inflight,
                            "foreground_reserved_slots": engine.foreground_reserved_slots,
                        }
                    )
                    event["admission_rejections_total"] = (
                        event.get("admission_rejections_total", 0) + 1
                    )
                    if len(rejections) > ADMISSION_REJECTIONS_KEPT:
                        del rejections[0]
                    continue
                attempt = {**candidate, "status": "waiting"}
                attempts = event["attempts"]
                event["attempt_count"] = event.get("attempt_count", len(attempts)) + 1
                if recovery_enabled and len(attempts) >= 32:
                    # Keep the first failure and recent evidence through long
                    # outages without growing a request record indefinitely.
                    del attempts[1]
                attempts.append(attempt)
                event.update(
                    engine_id=engine.id,
                    engine=engine.name,
                    model=candidate["model"],
                    tier=candidate["tier"],
                    status="waiting",
                )
                try:
                    await asyncio.to_thread(self.store.event, event)
                    req = self.http.build_request(
                        "POST",
                        engine.base_url + path,
                        json=body,
                        headers={
                            "Content-Type": "application/json",
                            "Accept-Encoding": "identity",
                            **await self.discovery.headers(engine),
                            **(
                                self.origins.upstream_headers(origin)
                                if self.origins is not None
                                else {}
                            ),
                        },
                        timeout=httpx.Timeout(engine.timeout_seconds, connect=8),
                    )
                    upstream = connection.response = await self.http.send(
                        req, stream=True
                    )
                    attempt.update(http_status=upstream.status_code, status="responded")
                    content = None
                    if upstream.status_code == 404 or (recovery_enabled and upstream.status_code == 400):
                        content = await read_limited(
                            upstream, engine.max_response_bytes
                        )
                        await self.discovery.refresh_engine(engine)
                        if obs.status == "available" and candidate["model"] not in {
                            m["id"] for m in obs.models
                        }:
                            await connection.release()
                            continue
                    if upstream.status_code in {408, 429, 500, 502, 503, 504}:
                        status = upstream.status_code
                        await connection.release()
                        obs.circuit_until = time.time() + (
                            engine.rate_limit_cooldown_seconds
                            if status == 429
                            else engine.failure_cooldown_seconds
                        )
                        obs.circuit_reason = "rate_limit" if status == 429 else "transport"
                        continue
                    event.update(
                        engine_id=engine.id,
                        engine=engine.name,
                        model=candidate["model"],
                        tier=candidate["tier"],
                        status="running",
                    )
                    await asyncio.to_thread(self.store.event, event)
                    receipt = {
                        "x-router-engine": engine.id,
                        "x-router-model": candidate["model"],
                        "x-router-request": event["id"],
                    }
                    response_content_type = upstream.headers.get("content-type", "")
                    is_event_stream = "text/event-stream" in response_content_type.lower()
                    if payload.get("stream") and upstream.is_success and is_event_stream:
                        # Content-Encoding is not forwarded. Relay decoded bytes,
                        # including when an upstream compresses its event stream.
                        iterator = upstream.aiter_bytes()
                        first = await anext(iterator, None)
                        if first is None:
                            obs.circuit_until = (
                                time.time() + engine.failure_cooldown_seconds
                            )
                            obs.circuit_reason = "transport"
                            await connection.release()
                            continue

                        async def abandon_stream():
                            if not connection.released:
                                try:
                                    await finish("cancelled")
                                finally:
                                    await connection.release()

                        return StreamingResponse(
                            connection.relay(
                                first,
                                iterator,
                                finish,
                                observe=meter.feed if meter is not None else None,
                                recoverable=recovery_enabled,
                            ),
                            status_code=upstream.status_code,
                            media_type=response_content_type,
                            headers={
                                **receipt,
                                "Cache-Control": "no-cache",
                                "X-Accel-Buffering": "no",
                            },
                            background=BackgroundTask(abandon_stream),
                        )
                    if content is None:
                        content = await read_limited(
                            upstream, engine.max_response_bytes
                        )
                    if meter is not None:
                        meter.feed_json(content)
                    code = upstream.status_code
                    content_type = upstream.headers.get(
                        "content-type", "application/json"
                    )
                    if upstream.is_success:
                        obs.last_success = time.time()
                    await finish("completed" if upstream.is_success else "failed", code)
                    await connection.release()
                    return Response(
                        content,
                        status_code=code,
                        media_type=content_type,
                        headers=receipt,
                    )
                except ResponseTooLarge:
                    await connection.release()
                    if meter is not None:
                        meter.oversized(admission.reserve)
                    await finish("failed", 502)
                    return JSONResponse(
                        {
                            "error": {
                                "message": "Upstream response exceeded the configured size limit"
                            }
                        },
                        status_code=502,
                    )
                except (httpx.HTTPError, OSError):
                    attempt.update(
                        error="Upstream connection failed or timed out", status="failed"
                    )
                    obs.circuit_until = time.time() + engine.failure_cooldown_seconds
                    obs.circuit_reason = "transport"
                    obs.error = "Completion transport failed"
                    await connection.release()
                except BaseException:
                    with CancelScope(shield=True):
                        try:
                            await finish("cancelled")
                        finally:
                            await connection.release()
                    raise
            quota_only = (
                not event["attempts"]
                and bool(event.get("admission_rejections"))
                and all(
                    rejection["code"] == "background_quota_exceeded"
                    for rejection in event["admission_rejections"]
                )
            )
            await finish("unavailable" if quota_only else "failed", 503)
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "Background request quota is full; foreground capacity is reserved"
                            if quota_only
                            else "All permitted engines were unavailable"
                        ),
                        "type": "route_unavailable" if quota_only else "upstream_unavailable",
                        **({"code": "background_quota_exceeded"} if quota_only else {}),
                    },
                    "request_id": event["id"],
                },
                status_code=503,
            )
        except BaseException:
            # Every exit must settle the admission, including a cancellation
            # that lands in a handler's own cleanup await. finish settles
            # synchronously before its first await, so only durability is
            # at stake here, never a slot or a reservation.
            if admission is not None and not admission.settled:
                with CancelScope(shield=True):
                    await finish("cancelled")
            raise

    async def dispatch_connected(self, request: Request, payload: dict, client: Client):
        validate_payload(payload)
        # The streaming branch below commits to an SSE response before dispatch
        # runs, so authorization refusals must resolve here to keep a 403 a 403.
        require_origin_route(request, payload["model"].strip())
        route = next((route for route in self.store.config().routes if route.name == payload.get("model")), None)
        preflight = decide(self.store.config(), self.discovery.views(), client, payload,
                           caller_key_present=getattr(request.state, "caller_key_present", True))
        if (payload.get("stream") and route and route.enabled and route.recovery_seconds != 0
                and preflight.get("status") not in {400, 401, 403, 404}):
            # Recovery owns one downstream stream. Heartbeats keep it connected
            # while discovery or an accepted upstream request is working.
            request.state.router_request_id = uuid4().hex
            return StreamingResponse(self.recovering_stream(request, payload, client),
                                     media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                              "X-Router-Request": request.state.router_request_id})
        # ASGI does not cancel non-streaming handlers when their caller leaves.
        # Cancel the upstream work as soon as that request is disconnected.
        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        operation = asyncio.create_task(self.dispatch(request, payload, client))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                {operation, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                return operation.result()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise HTTPException(499, "Client disconnected")
        finally:
            for task in (operation, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, watcher, return_exceptions=True)
            if operation.cancelled():
                await self.cancel_pending_event(request)

    async def cancel_pending_event(self, request):
        event = getattr(request.state, "router_event", None)
        if event and event["status"] in {"routing", "recovering", "waiting", "running"}:
            event.update(status="cancelled", http_status=None,
                         elapsed_ms=round((time.monotonic() - request.state.router_started) * 1000))
            with CancelScope(shield=True):
                await asyncio.to_thread(self.store.event, event)

    async def recovering_stream(self, request, payload, client):
        pending = None
        iterator = None
        response = None
        emitted = False
        try:
            while True:
                pending = asyncio.create_task(self.dispatch(request, payload, client))
                while not pending.done():
                    await asyncio.wait({pending}, timeout=5)
                    if not pending.done():
                        yield b": router waiting for serving endpoint\n\n"
                response = pending.result()
                pending = None
                if not isinstance(response, StreamingResponse):
                    # A refusal is an error event, not an empty completion, so
                    # it must not carry the [DONE] terminator.
                    if response.status_code >= 400:
                        yield b"data: " + response.body + b"\n\n"
                    else:
                        yield b"data: " + response.body + b"\n\ndata: [DONE]\n\n"
                    return
                iterator = response.body_iterator
                try:
                    while True:
                        pending = asyncio.create_task(anext(iterator, None))
                        while not pending.done():
                            await asyncio.wait({pending}, timeout=5)
                            if not pending.done():
                                yield b": router waiting for serving endpoint\n\n"
                        chunk = pending.result()
                        pending = None
                        if chunk is None:
                            return
                        yield chunk
                        emitted = True
                except RecoverableStreamError as error:
                    event = request.state.router_event
                    event["attempts"][-1]["error_type"] = str(error)
                    if emitted:
                        # Bytes already reached the caller, so this attempt
                        # cannot be replayed. Close with an interruption frame.
                        event["attempts"][-1].update(status="failed", http_status=502)
                        event.update(status="failed", http_status=502,
                                     elapsed_ms=round((time.monotonic() - request.state.router_started) * 1000))
                        await asyncio.to_thread(self.store.event, event)
                        yield b'event: error\ndata: {"error":{"message":"Upstream stream interrupted"}}\n\n'
                        return
                    route = next((route for route in self.store.config().routes if route.name == payload["model"]), None)
                    if not route or not route.enabled or route.recovery_seconds == 0:
                        raise
                    deadline = getattr(request.state, "router_recovery_deadline", None)
                    if deadline is None and route.recovery_seconds is not None:
                        deadline = time.monotonic() + route.recovery_seconds
                        request.state.router_recovery_deadline = deadline
                    if deadline is not None and time.monotonic() >= deadline:
                        event.update(status="failed", http_status=504,
                                     elapsed_ms=round((time.monotonic() - request.state.router_started) * 1000))
                        await asyncio.to_thread(self.store.event, event)
                        yield b'data: {"error":{"message":"Serving endpoint is still recovering"}}\n\n'
                        return
                    await asyncio.sleep(1)
                    await self.discovery.refresh_route(payload["model"])
        except HTTPException as error:
            body = json.dumps({"error": {"message": str(error.detail), "status": error.status_code}}).encode()
            # A refusal is an error event; it must not carry the success terminator.
            yield b"data: " + body + b"\n\n"
        finally:
            with CancelScope(shield=True):
                if pending is not None:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    if not pending.cancelled() and pending.exception() is None:
                        result = pending.result()
                        if isinstance(result, StreamingResponse) and result.background:
                            await result.background()
                if iterator is not None:
                    await iterator.aclose()
                if response is not None and response.background:
                    await response.background()
                await self.cancel_pending_event(request)
