"""One authenticated, bounded literal-word delivery per WebSocket.

Cairn selects words locally. This transport owns neither conversation nor room
leases, and provider completion never claims that a person heard the output.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import suppress
from uuid import uuid4

from anyio import CancelScope
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from .adapters.xai_tts import (
    MAX_PCM,
    MAX_PROVIDER_FRAME,
    SERVICE,
    SpeechFault,
    connect_provider,
    decode_message,
    pcm_delta,
)
from .proxy import InflightRequest
from .routing import decide

MAX_CONTROLS = 128
MAX_OUTPUT_PCM = 12288
CONNECT_SECONDS = 5
FIRST_AUDIO_SECONDS = 10
OVERALL_SECONDS = 60
# One spoken second is 24000 frames of one s16le sample each, and the text
# budget is measured in characters spoken per second. The provider connect and
# close steps run inside the same overall deadline, so they cannot be spent on
# audio; without that reserve a maximum-length utterance is cut off mid-audio.
SAMPLE_RATE = 24000
SAMPLE_BYTES = 2
CHARS_PER_SECOND = 15
SETUP_SECONDS = 6
AUDIO_SECONDS = MAX_PCM // (SAMPLE_RATE * SAMPLE_BYTES)
SPEECH_SECONDS = min(AUDIO_SECONDS, OVERALL_SECONDS - SETUP_SECONDS)
MAX_TEXT = SPEECH_SECONDS * CHARS_PER_SECOND
CLOSE_SECONDS = 1
PROVIDER_CLOSE_SECONDS = 2.5
PROVIDER_GRACE_SECONDS = 1.5
RECHECK_SECONDS = 1
OPTIONS = {
    "model": "cairn_voice",
    "voice": "cairn",
    "language": "en",
    "codec": "pcm",
    "sample_rate": "24000",
}


def permitted(client, connection, config):
    """Only a dedicated keyed agent may receive this paid transport grant."""
    route = next((r for r in config.routes if r.name == "cairn_voice"), None)
    return bool(
        route
        and route.enabled
        and route.require_caller_key
        and getattr(connection.state, "identity_basis", "") == "api_key"
        and getattr(connection.state, "caller_key_present", False)
        and not getattr(connection.state, "account_id", None)
        and client.enabled
        and client.kind == "agent"
        and client.allow_cloud
        and "cairn_voice" in client.route_names
        and client.engine_ids
        and SERVICE in client.model_patterns
    )


def speech_decision(config, views, client, connection, *, consider_capacity=True):
    if not permitted(client, connection, config):
        return {"candidates": [], "rejections": [], "status": 403}
    result = decide(
        config,
        views,
        client,
        {"model": "cairn_voice"},
        consider_capacity=consider_capacity,
        caller_key_present=True,
        required_capabilities={"speech", "streaming"},
    )
    by_id = {e.id: e for e in config.engines}
    result["candidates"] = [
        c
        for c in result["candidates"]
        if c["model"] == SERVICE
        and c["kind"] == "cloud"
        and by_id[c["engine_id"]].protocol == "xai_tts"
        and by_id[c["engine_id"]].credential_type == "static"
    ]
    return result


class ProviderOwner:
    """Own the actual socket until connection_lost positively proves closure."""

    def __init__(self, context):
        self.context, self.upstream = context, None
        self.closed, self._job = False, None

    def _positive(self):
        waiter = getattr(self.upstream, "connection_lost_waiter", None)
        return bool(isinstance(waiter, asyncio.Future) and waiter.done()
                    and not waiter.cancelled() and waiter.exception() is None)

    async def _close(self):
        self.upstream = self.upstream or getattr(self.context, "connection", None)
        if self.upstream is None:
            # Native connect failed before assigning any connection; its
            # creation/cancellation path owns cleanup of the unopened socket.
            self.closed = True
            return
        failure = False
        try:
            async with asyncio.timeout(PROVIDER_CLOSE_SECONDS):
                try:
                    async with asyncio.timeout(PROVIDER_GRACE_SECONDS) as grace:
                        await self.context.__aexit__(None, None, None)
                except TimeoutError:
                    # Only OUR deadline permits forced-close completion. An
                    # unrelated context TimeoutError remains a real failure.
                    failure = not grace.expired()
                except Exception:  # noqa: BLE001
                    # External connector cleanup can fail with arbitrary errors;
                    # still prove transport closure before releasing admission.
                    failure = True
                if not self._positive():
                    transport = getattr(self.upstream, "transport", None)
                    if transport is None:
                        raise SpeechFault("provider_close_unknown")
                    transport.abort()
                await self.upstream.wait_closed()
                if not self._positive():
                    raise SpeechFault("provider_close_unknown")
                self.closed = True
        except TimeoutError:
            raise SpeechFault("provider_close_unknown") from None
        if failure:
            raise SpeechFault("provider_close_failed")

    async def close(self):
        if self._job is None:
            self._job = asyncio.create_task(self._close())
        interrupted = False
        with CancelScope(shield=True):
            while not self._job.done():
                try:
                    await asyncio.shield(self._job)
                except asyncio.CancelledError:
                    interrupted = True
            self._job.result()
        if interrupted:
            raise asyncio.CancelledError


class Delivery:
    def __init__(self, socket, upstream, authorize):
        self.socket, self.upstream, self.authorize = socket, upstream, authorize
        self.text = self.controls = self.pcm = self.emitted = 0
        self.first_text_at = None
        self.done = self.stopped = self.completed = False
        self.stop_reason = "cancelled"
        self.first_text, self.first_audio = asyncio.Event(), asyncio.Event()
        self.carry = b""

    async def clients(self):
        while True:
            raw = await self.socket.receive()
            if raw["type"] == "websocket.disconnect":
                self.stopped = True
                self.stop_reason = "disconnected"
                return self.stop_reason
            msg = decode_message(raw.get("text"))
            self.controls += 1
            if self.controls > MAX_CONTROLS:
                raise SpeechFault("too_many_controls")
            kind = msg.get("type")
            if not isinstance(kind, str):
                raise SpeechFault("invalid_control")
            fields = {"type", "delta"} if kind == "text.delta" else {"type"}
            if set(msg) != fields or kind not in {
                "text.delta",
                "text.done",
                "text.clear",
            }:
                raise SpeechFault("invalid_control")
            if kind == "text.clear":
                # Latch before any await: queued provider audio can no longer be
                # forwarded. Closure is authoritative; no invented provider ack.
                self.stopped = True
                with suppress(Exception):
                    await asyncio.wait_for(
                        self.upstream.send(json.dumps(msg)), CLOSE_SECONDS
                    )
                return "cancelled"
            if self.done:
                raise SpeechFault("delivery_already_done")
            if kind == "text.delta":
                delta = msg["delta"]
                if not isinstance(delta, str) or not delta:
                    raise SpeechFault("invalid_text")
                try:
                    delta.encode("utf-8")
                except UnicodeError:
                    raise SpeechFault("invalid_text") from None
                self.text += len(delta)
                if self.text > MAX_TEXT:
                    raise SpeechFault("text_limit")
                if self.first_text_at is None:
                    self.first_text_at = asyncio.get_running_loop().time()
                self.first_text.set()
            else:
                if not self.text:
                    raise SpeechFault("empty_delivery")
                self.done = True
            await self.authorize()
            await self.upstream.send(json.dumps(msg, ensure_ascii=False))

    async def audio(self):
        while not self.stopped:
            raw = await self.upstream.recv()
            if self.stopped:
                return "cancelled"
            try:
                msg = decode_message(raw, max_frame=MAX_PROVIDER_FRAME)
            except SpeechFault:
                raise SpeechFault("invalid_provider_frame") from None
            kind = msg.get("type")
            if kind == "audio.delta":
                if not self.first_text.is_set():
                    raise SpeechFault("unsolicited_audio")
                chunk = pcm_delta(msg)
                self.pcm += len(chunk)
                if self.pcm > MAX_PCM:
                    raise SpeechFault("audio_limit")
                chunk = self.carry + chunk
                self.carry = chunk[-1:] if len(chunk) % 2 else b""
                chunk = chunk[: len(chunk) - len(self.carry)]
                if chunk:
                    if not self.first_audio.is_set():
                        await self.authorize()
                        self.first_audio.set()
                    # Provider framing is independent of finite edge buffers.
                    for offset in range(0, len(chunk), MAX_OUTPUT_PCM):
                        if self.stopped:
                            return "cancelled"
                        part = chunk[offset : offset + MAX_OUTPUT_PCM]
                        await self.socket.send_json(
                            {
                                "type": "audio.delta",
                                "delta": base64.b64encode(part).decode("ascii"),
                            }
                        )
                        self.emitted += len(part)
            elif kind == "audio.done":
                if not self.done or self.carry or not self.first_audio.is_set():
                    raise SpeechFault("incomplete_audio")
                await self.authorize()
                if self.stopped:
                    return "cancelled"
                self.completed = True
                return "completed"
            elif kind == "error":
                raise SpeechFault("provider_error")
            elif kind == "audio.clear":
                raise SpeechFault("unexpected_clear")
            else:
                raise SpeechFault("invalid_provider_frame")
        return "cancelled"

    async def close_owned(self, owner, result):
        if result != "completed":
            await owner.close()
            return result
        closing = asyncio.create_task(owner.close())
        controls = asyncio.create_task(self.clients())
        try:
            await asyncio.wait([closing, controls], return_when=asyncio.FIRST_COMPLETED)
            if controls.done():
                result = controls.result()  # Clear/disconnect wins during close.
            await closing
            if controls.done():
                result = controls.result()
            return result
        finally:
            controls.cancel()
            closing.cancel()
            await asyncio.gather(controls, closing, return_exceptions=True)

    async def watch(self):
        # Recheck the original keyed principal periodically without expensive
        # hashing for each PCM chunk. The first-audio deadline includes awaits.
        while True:
            deadline = None
            if self.first_text_at is not None and not self.first_audio.is_set():
                deadline = self.first_text_at + FIRST_AUDIO_SECONDS
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.sleep(RECHECK_SECONDS)
                    await self.authorize()
            except TimeoutError:
                if not self.first_audio.is_set():
                    raise SpeechFault("first_audio_timeout") from None

    async def run(self):
        tasks = [
            asyncio.create_task(fn()) for fn in (self.clients, self.audio, self.watch)
        ]
        try:
            finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Cancellation/disconnect wins a simultaneous completion race.
            if self.stopped:
                return self.stop_reason
            return next(iter(finished)).result()
        finally:
            self.stopped = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def register_speech(app, store, discovery, identity, identify_caller, connector=None):
    connect = connector or connect_provider

    @app.websocket("/v1/audio/speech")
    async def speech(socket: WebSocket):
        slot = delivery = upstream = context = owner = None
        accepted = False
        close_code = 1008
        try:
            # Refuse before accept and before any provider/socket work. Duplicate
            # query fields are also forbidden: there is one canonical delivery.
            if (
                len(socket.query_params.multi_items()) != len(OPTIONS)
                or dict(socket.query_params) != OPTIONS
            ):
                raise SpeechFault("unsupported_options")
            client = await identify_caller(socket)

            async def authorize():
                try:
                    current = await identity.identify(socket)
                except HTTPException:
                    raise SpeechFault("authorization_revoked") from None
                config = store.config()
                if current.id != client.id or not permitted(current, socket, config):
                    raise SpeechFault("authorization_revoked")
                decision = speech_decision(
                    config, discovery.views(), current, socket, consider_capacity=False
                )
                if delivery is not None and not any(
                    c["engine_id"] == engine.id for c in decision["candidates"]
                ):
                    raise SpeechFault("authorization_revoked")
                return current, config, decision

            current, config, decision = await authorize()
            if not decision["candidates"]:
                raise SpeechFault("speech_unavailable")
            candidate = decision["candidates"][0]
            engine = next(e for e in config.engines if e.id == candidate["engine_id"])
            if engine.value_mappings.get("voice", {}).get("cairn") != "rex":
                raise SpeechFault("unsupported_voice")
            headers = await discovery.headers(engine)  # static-only, after grant
            current, config, fresh = await authorize()
            if not any(c["engine_id"] == engine.id for c in fresh["candidates"]):
                raise SpeechFault("authorization_revoked")
            engine = next(e for e in config.engines if e.id == engine.id)
            if engine.value_mappings.get("voice", {}).get("cairn") != "rex":
                raise SpeechFault("unsupported_voice")
            slot = InflightRequest.claim(
                discovery.observation(engine.id),
                engine.max_inflight,
                engine.failure_cooldown_seconds,
                background=False,
                foreground_reserved_slots=engine.foreground_reserved_slots,
            )
            if slot is None:
                raise SpeechFault("speech_busy")
            await socket.accept()
            accepted = True
            close_code = 1011
            async with asyncio.timeout(OVERALL_SECONDS):
                context = connect(engine, headers, "rex")
                owner = ProviderOwner(context)
                upstream = await asyncio.wait_for(context.__aenter__(), CONNECT_SECONDS)
                owner.upstream = upstream
                await socket.send_json(
                    {
                        "type": "route.bound",
                        "request_id": str(uuid4()),
                        "route": "cairn_voice",
                        "revision": config.revision,
                        "client_id": current.id,
                        "engine_id": engine.id,
                        "protocol": "xai_tts",
                        "service": SERVICE,
                        "cloud": True,
                        "voice": "cairn",
                        "provider_voice": "rex",
                        "codec": "pcm",
                        "sample_rate": 24000,
                        "channels": 1,
                        "sample_format": "s16le",
                        "catalog_basis": "router_declared_service",
                        "heard": False,
                    }
                )
                delivery = Delivery(socket, upstream, authorize)
                result = await delivery.run()
            # Provider is closed before reporting terminal state. Transport
            # completion is not an acoustic/human receipt.
            result = await delivery.close_owned(owner, result)
            context = upstream = None
            if result == "disconnected":
                close_code = 1000
                return
            if result == "completed":
                slot.observation.last_success = time.time()
                await asyncio.wait_for(
                    socket.send_json({"type": "audio.done", "heard": False}),
                    CLOSE_SECONDS,
                )
            else:
                await asyncio.wait_for(
                    socket.send_json(
                        {
                            "type": "audio.clear",
                            "provider_acknowledged": False,
                            "transport": "closed",
                        }
                    ),
                    CLOSE_SECONDS,
                )
            close_code = 1000
        except WebSocketDisconnect:
            close_code = 1000
        except Exception as exc:  # noqa: BLE001 - provider failures must never expose response/secret bodies
            code = (
                str(exc)
                if isinstance(exc, SpeechFault)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "transport_failed"
            )
            if slot and code in {
                "provider_error",
                "invalid_audio",
                "incomplete_audio",
                "invalid_provider_frame",
                "first_audio_timeout",
                "transport_failed",
                "timeout",
                "provider_close_failed",
            }:
                slot.observation.circuit_until = time.time() + slot.failure_cooldown
            if accepted:
                with suppress(Exception):
                    await asyncio.wait_for(
                        socket.send_json(
                            {
                                "type": "error",
                                "code": code,
                                "partial": bool(delivery and delivery.emitted),
                                "heard": False,
                            }
                        ),
                        CLOSE_SECONDS,
                    )
        finally:
            if delivery:
                delivery.stopped = True
            interrupted = False
            if owner:
                try:
                    await owner.close()
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:  # noqa: BLE001, S110
                    # External cleanup errors must not bypass client cleanup.
                    # Unknown closure retains admission as quarantine; do not
                    # log raw provider errors that may include private data.
                    pass
            if slot and (owner is None or owner.closed):
                await slot.release()
            with suppress(Exception):
                await asyncio.wait_for(socket.close(code=close_code), CLOSE_SECONDS)
            if interrupted:
                raise asyncio.CancelledError
