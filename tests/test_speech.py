"""Exercise the real ASGI speech entrypoint; never connect a paid provider."""

import asyncio
import base64
import json
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import ValidationError

from gateway import speech
from gateway.adapters import xai_tts
from gateway.app import create_app
from gateway.schema import Client, Configuration, Engine, Route, Security, Selector

QUERY = urlencode(speech.OPTIONS)
WORDS = "Literal words chosen by Cairn, not a conversational prompt."
FAKE_SECRET = "test-provider-secret-never-log"
PCM = b"\x01\x00\x02\x00"


def audio(value=PCM):
    return {"type": "audio.delta", "delta": base64.b64encode(value).decode()}


class Provider:
    def __init__(self, frames=None, *, enter_delay=0, fail=False):
        self.frames = frames
        self.enter_delay, self.fail = enter_delay, fail
        self.calls, self.sent, self.closed = [], [], 0
        self.output = asyncio.Queue(maxsize=128)

    def __call__(self, engine, headers, voice):
        self.calls.append((engine.id, headers, voice))
        return self

    async def __aenter__(self):
        self.connection = self
        self.connection_lost_waiter = asyncio.get_running_loop().create_future()
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        if self.fail:
            raise RuntimeError(FAKE_SECRET + WORDS)
        return self

    async def __aexit__(self, *args):
        self.closed += 1
        self.connection_lost_waiter.set_result(None)

    async def wait_closed(self):
        await asyncio.shield(self.connection_lost_waiter)

    async def send(self, value):
        msg = json.loads(value)
        self.sent.append(msg)
        if msg["type"] == "text.done":
            frames = (
                self.frames
                if self.frames is not None
                else [audio(), {"type": "audio.done"}]
            )
            for frame in frames:
                await self.output.put(
                    frame if isinstance(frame, str) else json.dumps(frame)
                )
        if msg["type"] == "text.clear":
            await self.output.put(json.dumps(audio()))
            await self.output.put(json.dumps({"type": "audio.clear"}))

    async def recv(self):
        return await self.output.get()


class Socket:
    """An ASGI WebSocket client; queues impose real application backpressure."""

    def __init__(self, app, key=None, *, query=QUERY, headers=(), queue=16):
        self.app = app
        self.input, self.output = (
            asyncio.Queue(maxsize=16),
            asyncio.Queue(maxsize=queue),
        )
        header_list = [(b"host", b"router.test"), *headers]
        if key:
            header_list.append((b"authorization", f"Bearer {key}".encode()))
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": query.encode(),
            "headers": header_list,
            "client": ("127.0.0.1", 42),
            "server": ("router.test", 80),
            "subprotocols": [],
            "state": {},
        }
        self.task = None

    async def start(self):
        await self.input.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(
            self.app(self.scope, self.input.get, self.output.put)
        )
        return await self.next()

    async def next(self):
        raw = await asyncio.wait_for(self.output.get(), 3)
        if raw["type"] == "websocket.send":
            return json.loads(raw["text"])
        return raw

    async def send(self, value):
        await self.input.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def close(self):
        if self.task and not self.task.done():
            await self.input.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            await asyncio.wait_for(self.task, 3)


async def make_router(tmp_path, provider=None, *, speech_engine_id=None, **client_options):
    provider = provider or Provider()
    http_calls = []

    async def handle(request):
        http_calls.append(str(request.url))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "systemone", "ready": True})
        return httpx.Response(200, json={"model": "local-model", "choices": []})

    app = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(handle),
        speech_connector=provider,
    )
    engine_options = {"id": speech_engine_id} if speech_engine_id else {}
    engine = Engine(
        **engine_options,
        name="Paid speech",
        base_url="https://api.x.ai/v1",
        kind="cloud",
        protocol="xai_tts",
        model_patterns=[xai_tts.SERVICE],
        value_mappings={"voice": {"cairn": "rex"}},
        max_inflight=1,
    )
    local = Engine(name="Local", base_url="http://local.test/v1")
    options = {
        "name": "Cairn renderer",
        "kind": "agent",
        "allow_cloud": True,
        "route_names": ["cairn_voice", "auto"],
        "engine_ids": [engine.id, local.id],
        "model_patterns": [xai_tts.SERVICE, "local-model"],
    }
    options.update(client_options)
    caller = Client(**options)
    shared = Client(
        name="Shared",
        kind="shared",
        route_names=["auto"],
        model_patterns=["local-model"],
        allow_cloud=False,
    )
    app.state.store.save(
        Configuration(
            engines=[engine, local],
            clients=[caller, shared],
            security=Security(anonymous_client_id=shared.id),
            routes=[
                Route(name="auto"),
                Route(
                    name="cairn_voice",
                    require_caller_key=True,
                    primary=Selector(
                        kind="cloud",
                        engine_ids=[engine.id],
                        model_patterns=[xai_tts.SERVICE],
                    ),
                ),
            ],
        )
    )
    app.state.store.set_secret(engine.id, FAKE_SECRET)
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    http_calls.clear()
    return app, engine, caller, key, provider, http_calls


async def connected(app, key):
    ws = Socket(app, key)
    assert (await ws.start())["type"] == "websocket.accept"
    bound = await ws.next()
    assert bound["type"] == "route.bound" and bound["heard"] is False
    return ws, bound


async def test_literal_pcm_delivery_and_no_secret_or_transcript_persistence(
    tmp_path, caplog
):
    app, engine, _caller, key, provider, calls = await make_router(tmp_path)
    async with app.router.lifespan_context(app):
        ws, bound = await connected(app, key)
        assert bound["protocol"] == "xai_tts" and bound["cloud"]
        assert bound["catalog_basis"] == "router_declared_service"
        await ws.send({"type": "text.delta", "delta": WORDS})
        await ws.send({"type": "text.done"})
        assert base64.b64decode((await ws.next())["delta"]) == PCM
        assert await ws.next() == {"type": "audio.done", "heard": False}
        assert (await ws.next())["type"] == "websocket.close"
        await ws.close()
        assert provider.sent == [
            {"type": "text.delta", "delta": WORDS},
            {"type": "text.done"},
        ]
        assert provider.calls == [
            (engine.id, {"Authorization": f"Bearer {FAKE_SECRET}"}, "rex")
        ]
        assert provider.closed == 1 and not calls
        assert app.state.discovery.observation(engine.id).inflight == 0
        persisted = (
            json.dumps(app.state.store.events())
            + json.dumps(app.state.store.callers())
            + caplog.text
        )
        assert (
            WORDS not in persisted
            and FAKE_SECRET not in persisted
            and key not in persisted
        )
        assert app.state.store.callers()[0]["last_method"] == "WEBSOCKET"


@pytest.mark.parametrize(
    "principal",
    [
        "anonymous",
        "placeholder",
        "shared",
        "person",
        "machine",
        "moore",
        "cloud_denied",
        "route_denied",
        "model_denied",
        "engine_denied",
        "disabled",
        "revoked",
        "account",
        "spoofed",
    ],
)
async def test_denied_identity_never_connects_provider(tmp_path, principal):
    options = {}
    if principal in {"shared", "person", "machine", "moore"}:
        options["kind"] = "person" if principal == "moore" else principal
    if principal == "moore":
        options["name"] = "Dr Moore local clinical principal"
    if principal == "cloud_denied":
        options["allow_cloud"] = False
    if principal == "route_denied":
        options["route_names"] = ["auto"]
    if principal == "model_denied":
        options["model_patterns"] = ["local-model"]
    if principal == "engine_denied":
        options["engine_ids"] = []
    app, engine, caller, key, provider, calls = await make_router(tmp_path, **options)
    if principal == "disabled":
        config = app.state.store.config()
        config.clients[0].enabled = False
        app.state.store.save(config)
    if principal == "revoked":
        app.state.store.revoke_keys(caller.id)
    chosen_key = (
        None
        if principal in {"anonymous", "spoofed"}
        else "bad-key"
        if principal == "placeholder"
        else "mru_test"
        if principal == "account"
        else key
    )
    ws = Socket(
        app,
        chosen_key,
        headers=[
            (b"x-client-id", caller.id.encode()),
            (b"x-forwarded-for", b"127.0.0.1"),
            (b"cookie", b"operator_session=fake"),
        ],
    )
    async with app.router.lifespan_context(app):
        assert (await ws.start())["type"] == "websocket.close"
        await ws.close()
    assert not provider.calls and not calls
    assert app.state.discovery.observation(engine.id).inflight == 0


@pytest.mark.parametrize(
    "query",
    [
        "model=xai-tts",
        QUERY.replace("cairn_voice", "xai-tts"),
        QUERY + "&model=cairn_voice",
        QUERY + "&provider_url=https%3A%2F%2Fevil.test",
        QUERY.replace("pcm", "mp3"),
        QUERY.replace("24000", "48000"),
        QUERY.replace("cairn&", "rex&"),
    ],
)
async def test_unsupported_or_direct_options_denied_before_provider(tmp_path, query):
    app, _engine, _caller, key, provider, calls = await make_router(
        tmp_path, allow_direct_models=True
    )
    ws = Socket(app, key, query=query)
    assert (await ws.start())["type"] == "websocket.close"
    await ws.close()
    assert not provider.calls and not calls


@pytest.mark.parametrize(
    "frame,code",
    [
        ({"type": "text.delta", "delta": "x" * 2049}, "text_limit"),
        ({"type": "text.delta", "delta": "x" * 65536}, "invalid_frame"),
        ({"type": "text.delta", "delta": "\ud800"}, "invalid_text"),
        ({"type": "text.delta", "delta": 1}, "invalid_text"),
        ({"type": "text.delta", "delta": "ok", "messages": []}, "invalid_control"),
        ({"type": "input_audio", "delta": "AAAA"}, "invalid_control"),
        ({"type": "text.done"}, "empty_delivery"),
    ],
)
async def test_invalid_client_frames_fail_and_release(tmp_path, frame, code):
    app, engine, _caller, key, provider, _calls = await make_router(tmp_path)
    ws, _ = await connected(app, key)
    await ws.send(frame)
    assert await ws.next() == {
        "type": "error",
        "code": code,
        "partial": False,
        "heard": False,
    }
    await ws.close()
    assert not provider.sent and provider.closed == 1
    assert app.state.discovery.observation(engine.id).inflight == 0


@pytest.mark.parametrize(
    "frames,code,partial",
    [
        ([{"type": "audio.delta", "delta": "!!!"}], "invalid_audio", False),
        ([{"type": "audio.delta", "delta": 7}], "invalid_audio", False),
        ([audio(b"x"), {"type": "audio.done"}], "incomplete_audio", False),
        (["x" * 65537], "invalid_provider_frame", False),
        ([{"type": "error", "message": FAKE_SECRET + WORDS}], "provider_error", False),
        ([audio(), {"type": "error"}], "provider_error", True),
        ([{"type": "session.update"}], "invalid_provider_frame", False),
    ],
)
async def test_bad_provider_is_bounded_and_truthful(
    tmp_path, frames, code, partial, caplog
):
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider(frames)
    )
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": WORDS})
    await ws.send({"type": "text.done"})
    if partial:
        assert (await ws.next())["type"] == "audio.delta"
    assert await ws.next() == {
        "type": "error",
        "code": code,
        "partial": partial,
        "heard": False,
    }
    await ws.close()
    assert (
        provider.closed == 1
        and app.state.discovery.observation(engine.id).inflight == 0
    )
    assert WORDS not in caplog.text and FAKE_SECRET not in caplog.text


async def test_alignment_text_and_audio_boundaries(tmp_path, monkeypatch):
    provider = Provider(
        [audio(b"\x01"), audio(b"\x00\x02"), audio(b"\x00"), {"type": "audio.done"}]
    )
    app, engine, _caller, key, provider, _calls = await make_router(tmp_path, provider)
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": "x" * speech.MAX_TEXT})
    await ws.send({"type": "text.done"})
    chunks = [base64.b64decode((await ws.next())["delta"]) for _ in range(2)]
    assert b"".join(chunks) == PCM and all(len(c) % 2 == 0 for c in chunks)
    assert (await ws.next())["type"] == "audio.done"
    await ws.close()
    assert xai_tts.MAX_PCM == speech.MAX_PCM == 2880000
    monkeypatch.setattr(speech, "MAX_PCM", 2)
    provider.frames = [audio()]
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": "next"})
    await ws.send({"type": "text.done"})
    assert (await ws.next())["code"] == "audio_limit"
    await ws.close()
    assert app.state.discovery.observation(engine.id).inflight == 0


async def test_cancel_discards_late_audio_and_busy_releases_once(tmp_path):
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider([])
    )
    first, _ = await connected(app, key)
    second = Socket(app, key)
    assert (await second.start())["type"] == "websocket.close"
    await second.close()
    assert len(provider.calls) == 1
    await first.send({"type": "text.delta", "delta": "cancel me"})
    await first.send({"type": "text.clear"})
    terminal = await first.next()
    assert terminal == {
        "type": "audio.clear",
        "provider_acknowledged": False,
        "transport": "closed",
    }
    await first.close()
    assert (
        provider.closed == 1
        and app.state.discovery.observation(engine.id).inflight == 0
    )
    assert app.state.discovery.observation(engine.id).background_inflight == 0


async def test_disconnect_closes_upstream_and_releases(tmp_path):
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider([])
    )
    ws, _ = await connected(app, key)
    await ws.close()
    assert (
        provider.closed == 1
        and app.state.discovery.observation(engine.id).inflight == 0
    )


@pytest.mark.parametrize("timeout_kind", ["connect", "first_audio", "overall"])
async def test_timeouts_release(tmp_path, monkeypatch, timeout_kind):
    monkeypatch.setattr(speech, "RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(speech, "CONNECT_SECONDS", 0.03)
    monkeypatch.setattr(speech, "FIRST_AUDIO_SECONDS", 0.03)
    monkeypatch.setattr(
        speech, "OVERALL_SECONDS", 0.12 if timeout_kind == "overall" else 2
    )
    provider = Provider([], enter_delay=0.2 if timeout_kind == "connect" else 0)
    app, engine, _caller, key, provider, _calls = await make_router(tmp_path, provider)
    ws = Socket(app, key)
    assert (await ws.start())["type"] == "websocket.accept"
    if timeout_kind != "connect":
        assert (await ws.next())["type"] == "route.bound"
    if timeout_kind == "first_audio":
        await ws.send({"type": "text.delta", "delta": "waiting"})
    result = await ws.next()
    assert result["type"] == "error" and result["code"] in {
        "timeout",
        "first_audio_timeout",
    }
    await ws.close()
    assert (
        provider.closed == 1
        and app.state.discovery.observation(engine.id).inflight == 0
    )


async def test_key_revocation_cancels_existing_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "RECHECK_SECONDS", 0.01)
    app, engine, caller, key, provider, _calls = await make_router(
        tmp_path, Provider([])
    )
    ws, _ = await connected(app, key)
    app.state.store.revoke_keys(caller.id)
    result = await ws.next()
    assert result["type"] == "error" and result["code"] == "authorization_revoked"
    await ws.close()
    assert (
        provider.closed == 1
        and app.state.discovery.observation(engine.id).inflight == 0
    )


async def test_http_catalog_and_local_auto_never_use_speech(tmp_path):
    app, _engine, _caller, key, provider, calls = await make_router(
        tmp_path, allow_direct_models=True
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as http:
        assert (
            await http.post(
                "/v1/chat/completions", json={"model": "auto", "messages": []}
            )
        ).status_code == 200
        assert all("local.test" in url for url in calls)
        for model in ["cairn_voice", "xai-tts"]:
            response = await http.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": []},
            )
            assert response.status_code >= 400
        response = await http.post(
            "/v1/systemone",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "cairn_voice", "questions": [{"question": "speak?"}]},
        )
        assert response.status_code >= 400
        shared_catalog = (await http.get("/v1/models")).json()["data"]
        assert [r["id"] for r in shared_catalog] == ["auto"]
        keyed_catalog = (
            await http.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        ).json()["data"]
        voice = next(r for r in keyed_catalog if r["id"] == "cairn_voice")
        assert voice["serving"] == "speech"
        assert "xai-tts" not in [r["id"] for r in keyed_catalog]
    assert not provider.calls


@pytest.mark.parametrize(
    "options",
    [
        {"kind": "local"},
        {"base_url": "https://evil.test/v1"},
        {"credential_type": "xai_oauth"},
        {"model_settings": {"xai-tts": {"capabilities": ["text"]}}},
    ],
)
def test_speech_engine_cannot_be_disguised_as_local_or_chat(options):
    values = {
        "name": "TTS",
        "base_url": "https://api.x.ai/v1",
        "kind": "cloud",
        "protocol": "xai_tts",
    }
    values.update(options)
    with pytest.raises(ValidationError):
        Engine(**values)


def test_actual_connector_finite_controls_and_no_proxy(monkeypatch):
    import websockets.asyncio.client

    seen = []

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))

    monkeypatch.setattr(websockets.asyncio.client, "connect", FakeConnect)
    engine = Engine(
        name="TTS", base_url="https://api.x.ai/v1", kind="cloud", protocol="xai_tts"
    )
    connection = xai_tts.connect_provider(
        engine, {"Authorization": FAKE_SECRET, "Cookie": "must-not-forward"}, "rex"
    )
    sentinel = RuntimeError("redirect refused without response details")
    assert connection.process_redirect(sentinel) is sentinel
    args, options = seen[0]
    assert args == (
        "wss://api.x.ai/v1/tts?voice=rex&language=en&codec=pcm&sample_rate=24000",
    )
    assert options["additional_headers"] == {"Authorization": FAKE_SECRET}
    assert options["proxy"] is None and options["compression"] is None
    assert (
        options["max_size"] == 262144
        and options["max_queue"] == 4
        and options["write_limit"] == 32768
    )
    assert options["open_timeout"] == 5 and options["close_timeout"] == 1
    assert not options["logger"].propagate and not options["logger"].isEnabledFor(10)


async def test_binary_control_and_control_count_bound(tmp_path, monkeypatch):
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider([])
    )
    ws, _ = await connected(app, key)
    await ws.input.put({"type": "websocket.receive", "bytes": b"not microphone audio"})
    assert (await ws.next())["code"] == "invalid_frame"
    assert (await ws.next())["code"] == 1011
    await ws.close()
    assert not provider.sent
    monkeypatch.setattr(speech, "MAX_CONTROLS", 1)
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": "one"})
    await ws.send({"type": "text.delta", "delta": "two"})
    assert (await ws.next())["code"] == "too_many_controls"
    await ws.close()
    assert provider.sent == [{"type": "text.delta", "delta": "one"}]
    assert app.state.discovery.observation(engine.id).inflight == 0


async def test_output_backpressure_ends_with_bound_and_slot_release(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(speech, "OVERALL_SECONDS", 0.8)
    monkeypatch.setattr(speech, "CLOSE_SECONDS", 0.03)
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider([audio()] * 8)
    )
    ws = Socket(app, key, queue=1)
    assert (await ws.start())["type"] == "websocket.accept"
    assert (await ws.next())["type"] == "route.bound"
    await ws.send({"type": "text.delta", "delta": "short"})
    await ws.send({"type": "text.done"})
    # Intentionally do not drain output: no unbounded task or PCM queue.
    await asyncio.wait_for(ws.task, 2)
    assert provider.closed == 1
    assert app.state.discovery.observation(engine.id).inflight == 0
    assert ws.output.qsize() == 1
    assert (await ws.next())["type"] == "audio.delta"


async def test_provider_connect_failure_has_no_private_error_or_replay(
    tmp_path, caplog
):
    app, engine, _caller, key, provider, _calls = await make_router(
        tmp_path, Provider(fail=True)
    )
    ws = Socket(app, key)
    assert (await ws.start())["type"] == "websocket.accept"
    assert await ws.next() == {
        "type": "error",
        "code": "transport_failed",
        "partial": False,
        "heard": False,
    }
    assert (await ws.next())["code"] == 1011
    await ws.close()
    assert len(provider.calls) == provider.closed == 1
    assert app.state.discovery.observation(engine.id).inflight == 0
    assert app.state.discovery.observation(engine.id).circuit_until > 0
    assert FAKE_SECRET not in caplog.text and WORDS not in caplog.text


async def test_auto_has_no_cloud_fallback_when_local_fails(tmp_path):
    app, _engine, _caller, _key, provider, calls = await make_router(tmp_path)
    local = next(e for e in app.state.store.config().engines if e.kind == "local")
    app.state.discovery.observation(local.id).status = "offline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as http:
        response = await http.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert response.status_code >= 400
    assert not calls and not provider.calls


async def test_systemone_success_remains_typed_local_and_chat_guarded(tmp_path):
    requests = []

    async def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ready", "model": "AgentJev"})
        body = json.loads(request.content)
        requests.append((request.url.host, request.url.path, body))
        return httpx.Response(200, json={"result": [{"answer": "attend"}]})

    provider = Provider()
    app = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(handle),
        speech_connector=provider,
    )
    engine = Engine(
        name="System One", base_url="http://sys1.test", protocol="systemone"
    )
    shared = Client(
        name="Shared", kind="shared", route_names=["sys1"], model_patterns=["AgentJev"]
    )
    app.state.store.save(
        Configuration(
            engines=[engine],
            clients=[shared],
            security=Security(anonymous_client_id=shared.id),
            routes=[Route(name="sys1")],
        )
    )
    await app.state.discovery.refresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as http:
        payload = {
            "model": "sys1",
            "questions": [{"question": "Attend?", "choices": ["attend", "ignore"]}],
        }
        result = await http.post("/v1/systemone", json=payload)
        assert result.status_code == 200
        assert requests == [
            ("sys1.test", "/api/evaluate", {"questions": payload["questions"]})
        ]
        result = await http.post(
            "/v1/chat/completions", json={"model": "sys1", "messages": []}
        )
        assert result.status_code >= 400
        catalog = (await http.get("/v1/models")).json()["data"]
        assert catalog[0]["id"] == "sys1" and catalog[0]["serving"] == "systemone"
    assert not provider.calls


async def test_enabled_account_with_cloud_grants_still_cannot_use_voice(tmp_path):
    from gateway.schema import AccountLevel
    from gateway.security.credentials import digest

    app, engine, _caller, _key, provider, calls = await make_router(tmp_path)
    config = app.state.store.config()
    level = AccountLevel(
        name="Moore local clinical",
        allow_cloud=True,
        route_names=["cairn_voice"],
        engine_ids=[engine.id],
        model_patterns=["xai-tts"],
    )
    config.accounts.enabled = True
    config.account_levels = [level]
    app.state.store.save(config)
    account = app.state.store.create_account(
        "clinical-test", "Clinical fixture", level.id
    )
    activation, _ = app.state.store.issue_activation(account["id"], "activate")
    app.state.store.activate_account(activation, digest("test-only-private-password"))
    key, _ = app.state.store.create_account_key(account["id"], "fixture")
    ws = Socket(app, key)
    assert (await ws.start())["type"] == "websocket.close"
    await ws.close()
    assert not provider.calls and not calls


async def test_auth_revoked_after_credential_read_still_before_paid_connection(
    tmp_path, monkeypatch
):
    app, engine, caller, key, provider, calls = await make_router(tmp_path)
    original = app.state.discovery.headers

    async def headers(selected):
        result = await original(selected)
        app.state.store.revoke_keys(caller.id)
        return result

    monkeypatch.setattr(app.state.discovery, "headers", headers)
    ws = Socket(app, key)
    assert (await ws.start())["type"] == "websocket.close"
    await ws.close()
    assert not provider.calls and not calls
    assert app.state.discovery.observation(engine.id).inflight == 0


async def test_corrupt_catalog_cannot_cross_speech_into_chat_or_systemone(tmp_path):
    app, engine, _caller, key, provider, calls = await make_router(
        tmp_path, allow_direct_models=True
    )
    # A malformed upstream capability observation must not weaken transport.
    app.state.discovery.observation(engine.id).models[0]["capabilities"] = [
        "text",
        "streaming",
        "decision",
        "speech",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://router.test",
        headers={"Authorization": f"Bearer {key}"},
    ) as http:
        for endpoint, fields in [
            ("chat/completions", {"messages": []}),
            ("systemone", {"questions": [{"question": "speak?"}]}),
        ]:
            result = await http.post(
                "/v1/" + endpoint, json={"model": "xai-tts", **fields}
            )
            assert result.status_code >= 400
        catalog = (await http.get("/v1/models")).json()["data"]
        assert (
            next(r for r in catalog if r["id"] == "cairn_voice")["serving"] == "speech"
        )
        assert "xai-tts" not in [r["id"] for r in catalog]
    assert not provider.calls and not calls


async def test_actual_uvicorn_websocket_backend_with_finite_inbound_frames(tmp_path):
    """Temporary loopback-only test listener; no live service or provider."""
    import socket as network_socket

    import uvicorn
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosedError, InvalidStatus

    app, engine, _caller, key, provider, _calls = await make_router(tmp_path)
    listener = network_socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ws="websockets-sansio",
            ws_max_size=65536,
            ws_max_queue=4,
            ws_per_message_deflate=False,
            lifespan="off",
            log_level="critical",
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        url = f"ws://127.0.0.1:{port}/v1/audio/speech?{QUERY}"
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {key}"},
            proxy=None,
            compression=None,
        ) as ws:
            assert json.loads(await ws.recv())["type"] == "route.bound"
            await ws.send(json.dumps({"type": "text.delta", "delta": WORDS}))
            await ws.send(json.dumps({"type": "text.done"}))
            assert base64.b64decode(json.loads(await ws.recv())["delta"]) == PCM
            assert json.loads(await ws.recv()) == {"type": "audio.done", "heard": False}
        assert provider.closed == 1
        with pytest.raises(InvalidStatus) as denied:
            async with connect(url, proxy=None):
                pytest.fail("Anonymous voice handshake must fail")
        assert denied.value.response.status_code == 403 and len(provider.calls) == 1
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {key}"},
            proxy=None,
            compression=None,
        ) as ws:
            assert json.loads(await ws.recv())["type"] == "route.bound"
            await ws.send("x" * 65537)
            with pytest.raises(ConnectionClosedError) as closed:
                await ws.recv()
            assert closed.value.rcvd.code == 1009
        async with asyncio.timeout(3):
            while app.state.discovery.observation(engine.id).inflight:
                await asyncio.sleep(0.01)
        assert provider.closed == 2
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 3)
        listener.close()
        await app.state.proxy.http.aclose()


@pytest.mark.parametrize(
    "control_type",
    [[], {}, [WORDS, FAKE_SECRET], {"private": WORDS, "secret": FAKE_SECRET}],
    ids=["list", "dict", "private-list", "private-dict"],
)
async def test_non_string_control_type_is_client_fault_without_provider_cooldown(
    tmp_path, caplog, control_type
):
    app, engine, _caller, key, provider, calls = await make_router(tmp_path)
    observation = app.state.discovery.observation(engine.id)
    assert observation.circuit_until == 0
    ws, _ = await connected(app, key)
    await ws.send({"type": control_type})
    error = await ws.next()
    assert error == {
        "type": "error",
        "code": "invalid_control",
        "partial": False,
        "heard": False,
    }
    assert await ws.next() == {"type": "websocket.close", "code": 1011, "reason": ""}
    await ws.close()
    assert not provider.sent and not calls
    assert provider.closed == 1 and observation.inflight == 0
    assert observation.circuit_until == 0
    retained = (
        json.dumps(error)
        + json.dumps(app.state.store.events())
        + json.dumps(app.state.store.callers())
        + caplog.text
    )
    assert WORDS not in retained and FAKE_SECRET not in retained and key not in retained


async def test_text_limit_is_derived_from_the_audio_and_time_budget(tmp_path):
    # 15 spoken characters per second of 24 kHz s16le mono, less the provider
    # connect and close time that shares the same overall deadline.
    assert speech.SPEECH_SECONDS == speech.OVERALL_SECONDS - speech.SETUP_SECONDS
    assert speech.MAX_TEXT == speech.CHARS_PER_SECOND * speech.SPEECH_SECONDS
    assert speech.MAX_TEXT < 2048
    spoken_bytes = (
        speech.MAX_TEXT
        / speech.CHARS_PER_SECOND
        * speech.SAMPLE_RATE
        * speech.SAMPLE_BYTES
    )
    assert spoken_bytes <= speech.MAX_PCM
    assert speech.MAX_TEXT / speech.CHARS_PER_SECOND <= speech.SPEECH_SECONDS

    app, _engine, _caller, key, _provider, _calls = await make_router(tmp_path)
    ws, _ = await connected(app, key)
    await ws.send({"type": "text.delta", "delta": "x" * (speech.MAX_TEXT + 1)})
    assert await ws.next() == {
        "type": "error",
        "code": "text_limit",
        "partial": False,
        "heard": False,
    }
    await ws.close()


async def test_unavailable_speech_route_is_advertised_as_speech_not_chat(tmp_path):
    app, _engine, _caller, key, _provider, _calls = await make_router(tmp_path)
    config = app.state.store.config()
    for route in config.routes:
        if route.name == "cairn_voice":
            # A recovering alias (auto-discovered local host) with no speech
            # candidate: the row must stay a speech row and say it is unavailable.
            route.recovery_seconds = 30
            route.primary = Selector(kind="local", hosts=["local.test"])
    app.state.store.save(config)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://router.test",
        headers={"Authorization": f"Bearer {key}"},
    ) as http:
        answer = await http.get("/v1/models")
    rows = {row["id"]: row for row in answer.json()["data"]}
    assert answer.status_code == 200
    assert rows["cairn_voice"]["serving"] == "speech"
    assert rows["cairn_voice"]["available"] is False
