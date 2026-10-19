"""Actual Delivery/ASGI regressions for the observed paid transport boundary."""
import asyncio
import base64
import json
import time
from uuid import uuid4
import pytest
from gateway import speech
from gateway.adapters import xai_tts
from cairn_sentinel.presence_rex import RexRenderer
from test_speech import Provider, Socket, WORDS, audio, connected, make_router


def provider_frame(pcm):
    return json.dumps(audio(pcm), separators=(",", ":"))


@pytest.mark.parametrize("length", [52848, 98304, 196580])
async def test_large_provider_frames_rechunk_without_loss_or_replay(tmp_path, length):
    pcm = b"".join((i % 65536).to_bytes(2, "little") for i in range(length // 2))
    raw = provider_frame(pcm)
    if length == 52848:
        assert len(raw.encode()) == 70497
    assert 65536 < len(raw.encode()) <= 262144
    app, engine, _, key, provider, _ = await make_router(
        tmp_path, Provider([raw, {"type": "audio.done"}]))
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": WORDS})
    await ws.send({"type": "text.done"})
    parts = []
    for _ in range(32):
        frame = await ws.next()
        if frame["type"] == "audio.done":
            assert frame["heard"] is False
            break
        assert frame["type"] == "audio.delta"
        part = base64.b64decode(frame["delta"], validate=True)
        assert 0 < len(part) <= 12288 and len(part) % 2 == 0
        assert len(json.dumps(frame).encode()) <= 65536
        parts.append(part)
    else:
        pytest.fail("Bounded delivery did not terminate")
    assert b"".join(parts) == pcm
    assert len(parts) == (length + 12287) // 12288
    await ws.close()
    assert provider.closed == len(provider.calls) == 1
    assert app.state.discovery.observation(engine.id).inflight == 0
    await app.state.proxy.http.aclose()


async def test_provider_cap_plus_one_fails_privately_with_circuit_and_release(tmp_path):
    raw = provider_frame(b"\0" * 196584)
    assert len(raw.encode()) == 262145
    app, engine, _, key, provider, _ = await make_router(tmp_path, Provider([raw]))
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": WORDS})
    await ws.send({"type": "text.done"})
    assert await ws.next() == {"type": "error", "code": "invalid_provider_frame",
                              "partial": False, "heard": False}
    await ws.close()
    observation = app.state.discovery.observation(engine.id)
    assert observation.inflight == 0 and observation.circuit_until > time.time()
    assert provider.closed == len(provider.calls) == 1
    await app.state.proxy.http.aclose()


@pytest.mark.parametrize("length,code", [(65536, "invalid_control"), (65537, "invalid_frame")])
async def test_client_limit_unchanged_and_no_control_forwarded(tmp_path, length, code):
    prefix, suffix = '{"type":"text.done","padding":"', '"}'
    raw = prefix + "x" * (length - len(prefix) - len(suffix)) + suffix
    assert len(raw.encode()) == length
    app, engine, _, key, provider, _ = await make_router(tmp_path)
    ws, _ = await connected(app, key)
    await ws.input.put({"type": "websocket.receive", "text": raw})
    assert await ws.next() == {"type": "error", "code": code,
                              "partial": False, "heard": False}
    await ws.close()
    observation = app.state.discovery.observation(engine.id)
    assert not provider.sent and provider.closed == 1
    assert observation.inflight == 0 and observation.circuit_until == 0
    await app.state.proxy.http.aclose()


async def test_actual_delivery_rechecks_stop_between_pcm_chunks():
    raw = provider_frame(b"\x01\0" * 50000)
    emitted = []
    class Upstream:
        async def recv(self):
            return raw
    class Sink:
        async def send_json(self, frame):
            emitted.append(base64.b64decode(frame["delta"], validate=True))
            delivery.stopped = True
    async def authorize():
        return None
    delivery = speech.Delivery(Sink(), Upstream(), authorize)
    delivery.first_text.set()
    assert await delivery.audio() == "cancelled"
    assert emitted == [b"\x01\0" * 6144]
    assert delivery.emitted == 12288 and delivery.pcm == 100000
    assert delivery.completed is False


async def test_actual_asgi_cancel_during_rechunk_releases_and_discards(tmp_path):
    stalled = asyncio.Event()
    class PausedOutput(asyncio.Queue):
        chunks = 0
        async def put(self, value):
            if value["type"] == "websocket.send":
                frame = json.loads(value["text"])
                if frame["type"] == "audio.delta":
                    self.chunks += 1
                    if self.chunks == 2:
                        stalled.set()
                        await asyncio.Event().wait()
            await super().put(value)
    provider = Provider([provider_frame(b"\x01\0" * 50000), {"type": "audio.done"}])
    app, engine, _, key, provider, _ = await make_router(tmp_path, provider)
    ws = Socket(app, key)
    ws.output = PausedOutput(maxsize=16)
    assert (await ws.start())["type"] == "websocket.accept"
    assert (await ws.next())["type"] == "route.bound"
    await ws.send({"type": "text.delta", "delta": WORDS})
    await ws.send({"type": "text.done"})
    assert len(base64.b64decode((await ws.next())["delta"])) == 12288
    await asyncio.wait_for(stalled.wait(), 3)
    await ws.send({"type": "text.clear"})
    assert await ws.next() == {"type": "audio.clear", "provider_acknowledged": False,
                              "transport": "closed"}
    assert (await ws.next())["type"] == "websocket.close"
    await ws.close()
    assert provider.closed == len(provider.calls) == 1
    assert app.state.discovery.observation(engine.id).inflight == 0
    await app.state.proxy.http.aclose()


async def test_actual_rex_consumer_receives_identical_odd_provider_pcm(tmp_path):
    pcm = b"".join((i % 65536).to_bytes(2, "little") for i in range(75576))
    provider = Provider([provider_frame(pcm[:98305]), provider_frame(pcm[98305:]),
                         {"type": "audio.done"}])
    app, engine, _, key, provider, _ = await make_router(
        tmp_path, provider, speech_engine_id=str(uuid4()), id=str(uuid4()))
    ws = Socket(app, key)
    class Channel:
        async def __aenter__(self):
            assert (await ws.start())["type"] == "websocket.accept"
            return self
        async def __aexit__(self, *_):
            await ws.close()
        async def send(self, value):
            await ws.send(json.loads(value))
        async def recv(self):
            frame = await ws.next()
            assert len(json.dumps(frame).encode()) <= 65536
            return json.dumps(frame)
    def connect(_url, **options):
        assert options["max_size"] == 65536 and options["max_queue"] == 4
        return Channel()
    renderer = RexRenderer(key, connect=connect)
    chunks = [part async for part in renderer.render(WORDS)]
    assert b"".join(chunks) == pcm
    assert all(0 < len(part) <= 12288 and len(part) % 2 == 0 for part in chunks)
    assert renderer.last_route["provider_voice"] == "rex"
    assert provider.closed == len(provider.calls) == 1
    assert app.state.discovery.observation(engine.id).inflight == 0
    await app.state.proxy.http.aclose()
