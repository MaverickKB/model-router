"""Forward requests through the current policy; own admission, fallback and cancellation."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
from anyio import CancelScope
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from .discovery import DiscoveryService, Observation
from .identity import Identity
from .routing import CompletionOperation, apply_defaults, completion_operation, decide
from .schema import Client
from .store import Store
from .stream_protocol import CompletionMarker


class ResponseTooLarge(Exception):
    pass


def is_openai_completion_response(content: bytes) -> bool:
    """Require the minimum OpenAI completion envelope before recording proof."""
    try:
        body = json.loads(content)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(body, dict) and isinstance(body.get("choices"), list)


def attempt_identity(engine, model: str, credential_epoch: int) -> tuple:
    """Identify one forwardable engine contract without retaining a credential.

    The retry loop must suppress a failed attempt, but a same-ID engine can be
    replaced while that request is in flight.  Endpoint, request translation,
    declared inventory, and credential generation all define the contract that
    was actually attempted.  A changed contract receives one fresh attempt.
    """
    return (
        engine.id,
        model,
        engine.base_url,
        engine.catalog_protocol,
        engine.model_inventory_source,
        tuple(engine.declared_models),
        tuple(engine.completion_paths),
        tuple(engine.unsupported_parameters),
        json.dumps(engine.value_mappings, sort_keys=True, separators=(",", ":")),
        credential_epoch,
    )


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
        self,
        observation: Observation,
        failure_cooldown: float,
        record_success: Callable[[], Awaitable[None]] | None = None,
        record_contract_failure: Callable[[str], Awaitable[None]] | None = None,
        requires_response_envelope: bool = False,
    ):
        self.observation = observation
        self.failure_cooldown = failure_cooldown
        self.record_success = record_success
        self.record_contract_failure = record_contract_failure
        # A normal SSE terminal marker is enough to finish a client request.
        # A declared inventory has no catalog proof, so it needs the stronger
        # response-envelope evidence before it can become healthy.
        self.requires_response_envelope = requires_response_envelope
        self.response: httpx.Response | None = None
        self.released = False
        observation.inflight += 1

    @classmethod
    def claim(
        cls,
        observation: Observation,
        limit: int,
        failure_cooldown: float,
        record_success: Callable[[], Awaitable[None]] | None = None,
        record_contract_failure: Callable[[str], Awaitable[None]] | None = None,
        requires_response_envelope: bool = False,
    ):
        # This synchronous check-and-increment is atomic on the owning event loop.
        # No database or transport await may enter this reservation boundary.
        if observation.identity_locked or observation.inflight >= limit:
            return None
        return cls(
            observation,
            failure_cooldown,
            record_success,
            record_contract_failure,
            requires_response_envelope,
        )

    async def release(self):
        if not self.released:
            self.released = True
            try:
                if self.response is not None:
                    await self.response.aclose()
            finally:
                self.observation.inflight = max(0, self.observation.inflight - 1)

    async def relay(self, first, iterator, finish):
        terminal = CompletionMarker()
        status, code = "cancelled", None
        contract_failure = ""
        try:
            terminal.feed(first)
            yield first
            if not terminal.complete:
                async for chunk in iterator:
                    terminal.feed(chunk)
                    yield chunk
                    if terminal.complete:
                        break
            if terminal.complete:
                status, code = "completed", 200
                if (
                    self.requires_response_envelope
                    and not terminal.response_envelope
                ):
                    # Preserve the terminal event for the client. It still
                    # completed the stream protocol, but it cannot renew a
                    # declared engine's OpenAI response proof.
                    contract_failure = (
                        "Upstream stream completed without an OpenAI response envelope"
                    )
            else:
                # A terminal marker, not HTTP EOF, defines a completed SSE
                # response. Do not turn a truncated stream into success.
                status, code = "failed", 502
                contract_failure = "Upstream stream ended before completion"
                self.observation.circuit_until = time.time() + self.failure_cooldown
                yield (
                    b'event: error\ndata: {"error":{"message":"'
                    + contract_failure.encode()
                    + b'"}}\n\n'
                )
        except httpx.HTTPError:
            # A transport failure before the terminal marker invalidates a
            # declared engine's durable proof. The client still receives the
            # same terminal SSE error it received before proof tracking was
            # added.
            if not terminal.complete:
                status, code = "failed", 502
                contract_failure = "Upstream stream interrupted"
                self.observation.circuit_until = time.time() + self.failure_cooldown
                yield b'event: error\ndata: {"error":{"message":"Upstream stream interrupted"}}\n\n'
        finally:
            # OpenAI clients close on the terminal event without waiting for
            # HTTP EOF. Preserve that outcome through disconnect and protect
            # success proof, history write, and connection cleanup from
            # cancellation.
            if terminal.complete:
                status, code = "completed", 200
            with CancelScope(shield=True):
                try:
                    if contract_failure and self.record_contract_failure:
                        await self.record_contract_failure(contract_failure)
                    elif status == "completed":
                        if (
                            self.record_success
                            and (
                                not self.requires_response_envelope
                                or terminal.response_envelope
                            )
                        ):
                            await self.record_success()
                        elif not self.requires_response_envelope:
                            self.observation.last_success = time.time()
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

    async def dispatch(
        self,
        request: Request,
        payload: dict,
        client: Client,
        *,
        completion_path: CompletionOperation | None = None,
    ):
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("model"), str)
            or not payload["model"].strip()
        ):
            raise HTTPException(400, "A model or route name is required")
        if "messages" in payload and not isinstance(payload["messages"], list):
            raise HTTPException(400, "messages must be an array")
        path = completion_path or completion_operation(request.url.path)
        if path is None:
            # The ASGI application mounts only the OpenAI operations above,
            # but do not silently coerce a future route into chat semantics.
            raise HTTPException(404, "Unsupported completion operation")
        config = self.store.config()
        caller_key_present = getattr(request.state, "caller_key_present", True)
        operator_test = getattr(request.state, "identity_basis", "") == "operator_test"
        decision = decide(
            config,
            self.discovery.views(),
            client,
            payload,
            caller_key_present=caller_key_present,
            completion_path=path,
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
        started = time.monotonic()
        await asyncio.to_thread(self.store.event, event)

        async def finish(status, code=None):
            if event["attempts"] and event["attempts"][-1].get("status") == "waiting":
                event["attempts"][-1]["status"] = status
            event.update(
                status=status,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                http_status=code,
            )
            await asyncio.to_thread(self.store.event, event)

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

        if not decision["candidates"]:
            return await refuse(decision)

        # A failed upstream attempt is not retried. A saturated engine has not
        # been sent any traffic, so keep it separate: another eligible engine
        # may still serve this request without turning a capacity check into a
        # false upstream attempt.
        attempted: set[tuple] = set()
        saturated: set[tuple] = set()
        terminal_contract_error: tuple[int, str] | None = None
        while True:
            # Re-evaluate the complete policy after each failed attempt.
            try:
                if operator_test:
                    await self.identity.require_operator(request)
                    current_client = next(
                        (c for c in self.store.config().clients if c.id == client.id),
                        None,
                    )
                    if current_client is None:
                        raise HTTPException(403, "Test permission policy was removed")
                else:
                    current_client = await self.identity.identify(request)
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
                completion_path=path,
            )
            event.update(
                client_id=current_client.id,
                client=current_client.name,
                decision=fresh,
                revision=current_config.revision,
            )
            if not fresh["candidates"] and fresh.get("status"):
                return await refuse(fresh)
            candidate = None
            engine = None
            request_snapshot = None
            attempt_key = None
            snapshot_changed = False
            for possible in fresh["candidates"]:
                possible_engine = next(
                    (
                        item
                        for item in current_config.engines
                        if item.id == possible["engine_id"]
                    ),
                    None,
                )
                if possible_engine is None:
                    continue
                possible_snapshot = self.store.engine_request_snapshot(possible_engine)
                if possible_snapshot is None:
                    snapshot_changed = True
                    continue
                possible_key = attempt_identity(
                    possible_engine,
                    possible["model"],
                    possible_snapshot.credential_epoch,
                )
                if possible_key in attempted or possible_key in saturated:
                    continue
                candidate = possible
                engine = possible_engine
                request_snapshot = possible_snapshot
                attempt_key = possible_key
                break
            if candidate is None or engine is None or request_snapshot is None:
                if snapshot_changed:
                    # The configuration changed between policy selection and
                    # request binding. Yield once, then derive candidates from
                    # the new contract instead of treating it as an outage.
                    await asyncio.sleep(0)
                    continue
                break
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
            credential = request_snapshot.credential
            proof_token = request_snapshot.declared_proof
            connection = InflightRequest.claim(
                obs,
                engine.max_inflight,
                engine.failure_cooldown_seconds,
                lambda engine=engine, obs=obs, token=proof_token: self.discovery.record_success(
                    engine, obs, token
                ),
                lambda reason, engine=engine, obs=obs, token=proof_token: self.discovery.record_failure(
                    engine, obs, reason, token
                ),
                requires_response_envelope=proof_token is not None,
            )
            if connection is None:
                saturated.add(attempt_key)
                continue
            attempted.add(attempt_key)
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
                headers = {
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                    **({"Authorization": f"Bearer {credential}"} if credential else {}),
                }
                req = self.http.build_request(
                    "POST",
                    engine.base_url + path,
                    json=body,
                    headers=headers,
                    timeout=httpx.Timeout(engine.timeout_seconds, connect=8),
                )
                upstream = connection.response = await self.http.send(req, stream=True)
                attempt.update(http_status=upstream.status_code, status="responded")
                content = None
                if upstream.status_code == 404:
                    content = await read_limited(upstream, engine.max_response_bytes)
                    if engine.model_inventory_source == "declared":
                        await self.discovery.record_failure(
                            engine,
                            obs,
                            "Completion endpoint or declared model returned HTTP 404",
                            proof_token,
                        )
                        terminal_contract_error = (
                            404,
                            "Completion endpoint or selected model returned HTTP 404",
                        )
                        await connection.release()
                        continue
                    # A catalog-backed engine can refresh its model list after
                    # a selected ID disappears. Preserve the upstream 404 when
                    # that catalog still advertises the exact ID: replaying a
                    # caller request after a 404 risks duplicate work.
                    await self.discovery.refresh_engine(engine)
                    if obs.status == "available" and candidate["model"] not in {
                        model["id"] for model in obs.models
                    }:
                        await connection.release()
                        continue
                if upstream.status_code in {401, 403}:
                    content = await read_limited(upstream, engine.max_response_bytes)
                    if engine.model_inventory_source == "declared":
                        await self.discovery.record_failure(
                            engine,
                            obs,
                            f"Completion endpoint returned HTTP {upstream.status_code}",
                            proof_token,
                        )
                        terminal_contract_error = (
                            upstream.status_code,
                            f"Completion endpoint returned HTTP {upstream.status_code}",
                        )
                        await connection.release()
                        continue
                if upstream.status_code in {408, 429, 500, 502, 503, 504}:
                    status = upstream.status_code
                    if engine.model_inventory_source == "declared":
                        await self.discovery.record_failure(
                            engine,
                            obs,
                            f"Completion endpoint returned HTTP {status}",
                            proof_token,
                        )
                    else:
                        obs.circuit_until = time.time() + (
                            engine.rate_limit_cooldown_seconds
                            if status == 429
                            else engine.failure_cooldown_seconds
                        )
                    await connection.release()
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
                        await self.discovery.record_failure(
                            engine,
                            obs,
                            "Upstream stream ended before completion",
                            proof_token,
                        )
                        terminal_contract_error = (502, "Upstream stream ended before completion")
                        await connection.release()
                        continue

                    return StreamingResponse(
                        connection.relay(first, iterator, finish),
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
                    content = await read_limited(upstream, engine.max_response_bytes)
                code = upstream.status_code
                content_type = upstream.headers.get("content-type", "application/json")
                if upstream.is_success:
                    if not is_openai_completion_response(content):
                        await self.discovery.record_failure(
                            engine,
                            obs,
                            "Completion response did not match the OpenAI response format",
                            proof_token,
                        )
                        await connection.release()
                        # A 2xx response may already represent work upstream.
                        # Never replay it through a fallback merely because its
                        # response contract was wrong.
                        await finish("failed", 502)
                        return JSONResponse(
                            {
                                "error": {
                                    "message": "Upstream completion response did not match the OpenAI response format"
                                },
                                "request_id": event["id"],
                            },
                            status_code=502,
                        )
                    await self.discovery.record_success(engine, obs, proof_token)
                await finish("completed" if upstream.is_success else "failed", code)
                await connection.release()
                return Response(
                    content, status_code=code, media_type=content_type, headers=receipt
                )
            except ResponseTooLarge:
                await connection.release()
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
                if engine.model_inventory_source == "declared":
                    await self.discovery.record_failure(
                        engine,
                        obs,
                        "Completion transport failed",
                        proof_token,
                    )
                else:
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
        if terminal_contract_error:
            code, message = terminal_contract_error
            await finish("failed", code)
            return JSONResponse(
                {"error": {"message": message}, "request_id": event["id"]},
                status_code=code,
            )
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

    async def dispatch_connected(
        self,
        request: Request,
        payload: dict,
        client: Client,
        *,
        completion_path: CompletionOperation | None = None,
    ):
        # ASGI does not cancel non-streaming handlers when their caller leaves.
        # Cancel the upstream work as soon as that request is disconnected.
        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        operation = asyncio.create_task(
            self.dispatch(
                request,
                payload,
                client,
                completion_path=completion_path,
            )
        )
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
