"""Forward requests through the current policy; own admission, fallback and cancellation."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import httpx
from anyio import CancelScope
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from .accounts.limits import AccountLimits, Refusal, UsageMeter
from .accounts.principal import level_for
from .discovery import DiscoveryService, Observation
from .identity import Identity
from .performance import ServedTiming
from .routing import apply_defaults, decide
from .schema import Client
from .store import Store
from .stream_protocol import CompletionMarker


class ResponseTooLarge(Exception):
    pass


async def read_limited(response: httpx.Response, limit: int) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise ResponseTooLarge
    return bytes(content)


class InflightRequest:
    """Own one admission slot and its upstream connection through stream completion."""

    def __init__(self, observation: Observation, failure_cooldown: float):
        self.observation = observation
        self.failure_cooldown = failure_cooldown
        self.response: httpx.Response | None = None
        self.released = False
        observation.inflight += 1

    @classmethod
    def claim(cls, observation: Observation, limit: int, failure_cooldown: float):
        # This synchronous check-and-increment is atomic on the owning event loop.
        # No database or transport await may enter this reservation boundary.
        if observation.identity_locked or observation.inflight >= limit:
            return None
        return cls(observation, failure_cooldown)

    async def release(self):
        if not self.released:
            self.released = True
            try:
                if self.response is not None:
                    await self.response.aclose()
            finally:
                self.observation.inflight = max(0, self.observation.inflight - 1)

    async def relay(self, first, iterator, finish, observe=None):
        terminal = CompletionMarker()
        status, code = "cancelled", None
        try:
            terminal.feed(first)
            if observe is not None:
                observe(first)
            yield first
            if not terminal.complete:
                async for chunk in iterator:
                    terminal.feed(chunk)
                    if observe is not None:
                        observe(chunk)
                    yield chunk
                    if terminal.complete:
                        break
            status, code = "completed", 200
        except httpx.HTTPError:
            status, code = "failed", 502
            self.observation.circuit_until = time.time() + self.failure_cooldown
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
        # Open windows are preloaded once; the request path never loads one.
        self.limits = AccountLimits()
        self.limits.ledger.seed(store.open_usage_windows(time.time()))

    async def dispatch(self, request: Request, payload: dict, client: Client):
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("model"), str)
            or not payload["model"].strip()
        ):
            raise HTTPException(400, "A model or route name is required")
        if "messages" in payload and not isinstance(payload["messages"], list):
            raise HTTPException(400, "messages must be an array")
        config = self.store.config()
        caller_key_present = getattr(request.state, "caller_key_present", True)
        operator_test = getattr(request.state, "identity_basis", "") == "operator_test"
        decision = decide(
            config,
            self.discovery.views(),
            client,
            payload,
            caller_key_present=caller_key_present,
        )
        event = {
            "id": uuid4().hex,
            "ts": time.time(),
            "caller": getattr(request.state, "caller", None),
            "client_id": client.id,
            "client": client.name,
            "requested": payload["model"],
            "status": "routing",
            "attempts": [],
            "decision": decision,
            "stream": bool(payload.get("stream")),
            "revision": config.revision,
        }
        # Bound before finish exists: finish settles an admission when one was
        # claimed and must stay callable on every earlier exit.
        admission = None
        # Every request counts its output for performance history. Only an
        # account admission also settles those counts against a budget.
        meter = UsageMeter(payload)
        timing = ServedTiming()
        account_id = getattr(request.state, "account_id", None)
        started = time.monotonic()
        await asyncio.to_thread(self.store.event, event)

        async def finish(status, code=None):
            row = None
            usage = meter.result(status, code)
            if admission is not None and not admission.settled:
                # Settle synchronously before the first await so a cancellation
                # landing here can lose durability, never a slot or reservation.
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
            # finish can run again when cancellation lands during the first
            # write, so the stored metrics always follow the final status.
            event.pop("performance", None)
            if status == "completed":
                performance = timing.summary(usage)
                if performance is not None:
                    event["performance"] = performance
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

        def observe_chunk(chunk):
            # The relay observes every chunk exactly once, the prefetched
            # first chunk included, so each chunk is timed once. A stream
            # delivered in a single read therefore has no generation interval.
            meter.feed(chunk)
            timing.mark_chunk()

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
                if not fresh["candidates"] and fresh.get("status"):
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
                    break
                attempted.add((candidate["engine_id"], candidate["model"]))
                engine = next(
                    e for e in current_config.engines if e.id == candidate["engine_id"]
                )
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
                    admission is not None
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
                path = (
                    "/completions"
                    if request.url.path.endswith("/completions")
                    and not request.url.path.endswith("/chat/completions")
                    else "/chat/completions"
                )
                connection = InflightRequest.claim(
                    obs, engine.max_inflight, engine.failure_cooldown_seconds
                )
                if connection is None:
                    continue
                attempt = {**candidate, "status": "waiting"}
                event["attempts"].append(attempt)
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
                        },
                        timeout=httpx.Timeout(engine.timeout_seconds, connect=8),
                    )
                    timing.mark_sent()
                    upstream = connection.response = await self.http.send(
                        req, stream=True
                    )
                    attempt.update(http_status=upstream.status_code, status="responded")
                    content = None
                    if upstream.status_code == 404:
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
                    if payload.get("stream") and upstream.is_success:
                        # Content-Encoding is not forwarded. Relay decoded bytes,
                        # including when an upstream compresses its event stream.
                        iterator = upstream.aiter_bytes()
                        first = await anext(iterator, None)
                        if first is None:
                            obs.circuit_until = (
                                time.time() + engine.failure_cooldown_seconds
                            )
                            await connection.release()
                            continue

                        return StreamingResponse(
                            connection.relay(
                                first,
                                iterator,
                                finish,
                                observe=observe_chunk,
                            ),
                            status_code=upstream.status_code,
                            media_type=upstream.headers.get(
                                "content-type", "text/event-stream"
                            ),
                            headers={
                                **receipt,
                                "Cache-Control": "no-cache",
                                "X-Accel-Buffering": "no",
                            },
                        )
                    if content is None:
                        content = await read_limited(
                            upstream, engine.max_response_bytes
                        )
                    timing.mark_body_read()
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
                    if admission is not None:
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
                    obs.error = "Completion transport failed"
                    await connection.release()
                except BaseException:
                    with CancelScope(shield=True):
                        try:
                            await finish("cancelled")
                        finally:
                            await connection.release()
                    raise
            await finish("failed", 503)
            return JSONResponse(
                {
                    "error": {
                        "message": "All permitted engines were unavailable",
                        "type": "upstream_unavailable",
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
