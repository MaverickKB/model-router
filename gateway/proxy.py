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

from .discovery import DiscoveryService, Observation
from .routing import apply_defaults, decide
from .schema import Client
from .store import Store


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

    async def relay(self, first, iterator, finish):
        try:
            yield first
            async for chunk in iterator:
                yield chunk
            self.observation.last_success = time.time()
            await finish("completed", 200)
        except asyncio.CancelledError:
            # Starlette cancellation remains active at every await. Cleanup must
            # finish inside its own shield before that cancellation propagates.
            with CancelScope(shield=True):
                await finish("cancelled")
            raise
        except httpx.HTTPError:
            self.observation.circuit_until = time.time() + self.failure_cooldown
            await finish("failed", 502)
            yield b'event: error\ndata: {"error":{"message":"Upstream stream interrupted"}}\n\n'
        finally:
            with CancelScope(shield=True):
                await self.release()


class Proxy:
    def __init__(
        self, store: Store, discovery: DiscoveryService, http: httpx.AsyncClient
    ):
        self.store, self.discovery, self.http = store, discovery, http

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
        decision = decide(config, self.discovery.views(), client, payload)
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

        if not decision["candidates"]:
            status = decision.get("status", 503)
            await finish("denied" if status == 403 else "unavailable", status)
            return JSONResponse(
                {
                    "error": {
                        "message": decision.get(
                            "error", "No allowed, available model matches this request"
                        ),
                        "type": "route_unavailable",
                    },
                    "request_id": event["id"],
                },
                status_code=status,
            )

        attempted = set()
        while True:
            # Re-evaluate the complete policy after each failed attempt.
            current_config = self.store.config()
            current_client = next(
                (c for c in current_config.clients if c.id == client.id), None
            )
            if current_client is None:
                await finish("denied", 403)
                raise HTTPException(403, "Client was removed")
            fresh = decide(
                current_config, self.discovery.views(), current_client, payload
            )
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
                headers = {
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                    **self.discovery.headers(engine),
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
                    iterator = upstream.aiter_raw()
                    first = await anext(iterator, None)
                    if first is None:
                        obs.circuit_until = (
                            time.time() + engine.failure_cooldown_seconds
                        )
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
                    obs.last_success = time.time()
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
