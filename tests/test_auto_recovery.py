"""Automatic routes follow serving evidence and recover without replaying output."""

import asyncio
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.network.report import write_json
from gateway.routing import decide
from gateway.schema import Client, Configuration, Discovery, Engine, Route, Selector


async def router(tmp_path, handler, *, managed=False):
    app = create_app(str(tmp_path), background=False, transport=httpx.MockTransport(handler))
    engine = Engine(name="Serving endpoint", base_url="http://127.0.0.1:8101/v1", max_inflight=8)
    client = Client(name="Local caller")
    route = Route(name="auto")
    if managed:
        route = Route(name="auto", primary=Selector(hosts=["127.0.0.1"]), recovery_seconds=3)
    app.state.store.save(Configuration(
        engines=[engine], clients=[client], routes=[route],
        discovery=Discovery(enabled=True, targets=["127.0.0.1"], auto_register=False, mdns=False),
    ))
    key = app.state.store.issue_key(client.id)
    await app.state.discovery.refresh()
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test",
        headers={"Authorization": "Bearer " + key},
    )
    return app, engine, client, http


async def test_runtime_catalog_supplies_context_and_serving_capacity(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "context_length": 262144, "streams": {"max": 4}})
        return httpx.Response(404)

    app, engine, client, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        view = app.state.discovery.views()[0]
        assert view["models"][0]["context_length"] == 262144
        assert view["max_inflight"] == 4
        app.state.discovery.observation(engine.id).inflight = 4
        decision = decide(app.state.store.config(), app.state.discovery.views(), client, {"model": "auto"})
        assert not decision["candidates"]


async def test_positive_probe_reopens_only_a_transport_circuit(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream)
    async with app.router.lifespan_context(app), http:
        observation = app.state.discovery.observation(engine.id)
        observation.circuit_until = time.time() + 60
        observation.circuit_reason = "transport"
        await app.state.discovery.refresh_engine(engine)
        assert app.state.discovery.views()[0]["status"] == "available"
        observation.circuit_until = time.time() + 60
        observation.circuit_reason = "rate_limit"
        await app.state.discovery.refresh_engine(engine)
        assert app.state.discovery.views()[0]["status"] == "unavailable"


async def test_one_catalog_blink_preserves_recent_live_evidence(tmp_path):
    failing = False

    async def upstream(request):
        if request.url.path.endswith("/models"):
            if failing:
                raise httpx.ReadTimeout("temporary catalog failure")
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        failing = True
        await app.state.discovery.refresh_engine(engine)
        assert app.state.discovery.views()[0]["status"] == "available"
        assert app.state.discovery.views()[0]["models"][0]["id"] == "current-model"
        app.state.discovery.observation(engine.id).observed_at = time.time() - 1000
        await app.state.discovery.refresh_engine(engine)
        assert app.state.discovery.views()[0]["status"] != "available"


async def test_auto_admits_replacement_port_only_on_its_scoped_hosts(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            model = "replacement" if request.url.port == 9823 else "previous"
            return httpx.Response(200, json={"data": [{"id": model}]})
        return httpx.Response(404)

    app, engine, client, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        report = {"hosts": [{"address": "127.0.0.1", "services": [{
            "status": "model_service", "protocol": "openai", "models": [{"id": "replacement"}],
            "base_url": "http://127.0.0.1:9823/v1", "capabilities": ["text", "streaming"],
        }]}]}
        write_json(app.state.store.discovery_directory / "network.json", report)
        await app.state.discovery.consume_network()
        engines = app.state.store.config().engines
        assert len(engines) == 2
        assert engines[0].base_url == engine.base_url
        observation = app.state.discovery.observation(engine.id)
        observation.status = "offline"
        observation.models = []
        decision = decide(app.state.store.config(), app.state.discovery.views(), client, {"model": "auto"})
        assert [candidate["model"] for candidate in decision["candidates"]] == ["replacement"]
        assert app.state.store.config().routes[0].primary.engine_ids == []
        assert await app.state.discovery.discover_url("http://192.0.2.7:9823/v1", "network") is None


@pytest.mark.parametrize("recovery_seconds", [3, None])
async def test_pending_request_survives_temporary_backend_failure(tmp_path, recovery_seconds):
    calls = 0

    async def upstream(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"model": "current-model", "choices": [{"message": {"content": "recovered"}}]})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    config = app.state.store.config()
    config.routes[0].recovery_seconds = recovery_seconds
    app.state.store.save(config)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "messages": []})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "recovered"
        assert calls == 2
        assert len(app.state.store.events()) == 1
        assert app.state.store.events()[0]["status"] == "completed"
        assert app.state.discovery.observation(engine.id).inflight == 0


async def test_prolonged_outage_has_one_route_notice_and_clears_on_recovery(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        discovery = app.state.discovery
        observation = discovery.observation(engine.id)
        discovery.started_at = time.time() - 1000
        observation.status = "offline"
        observation.models = []
        observation.observed_at = time.time() - 30
        assert discovery.recovery_warnings() == []
        observation.observed_at = time.time() - 1000
        assert len(discovery.recovery_warnings()) == 1
        assert "15 minutes" in discovery.recovery_warnings()[0]
        await discovery.refresh_engine(engine)
        assert discovery.recovery_warnings() == []
        event = {"id": "pending", "ts": time.time() - 1000, "status": "recovering",
                 "requested": "auto", "recovering_since": time.time() - 1000}
        app.state.store.event(event)
        # A healthy catalog cannot hide prolonged generation failures.
        assert len(discovery.recovery_warnings()) == 1
        event["status"] = "completed"
        app.state.store.event(event)
        assert discovery.recovery_warnings() == []


async def test_long_recovery_keeps_one_bounded_history_entry(tmp_path, monkeypatch):
    calls = 0
    sleep = asyncio.sleep

    async def next_tick(_seconds):
        await sleep(0)

    async def upstream(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            calls += 1
            if calls <= 64:
                return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": "returned"}}]})
        return httpx.Response(404)

    app, _, _, http = await router(tmp_path, upstream, managed=True)
    config = app.state.store.config()
    config.routes[0].recovery_seconds = None
    app.state.store.save(config)
    monkeypatch.setattr("gateway.proxy.asyncio.sleep", next_tick)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "messages": []})
        assert response.status_code == 200
        events = app.state.store.events()
        assert len(events) == 1
        assert events[0]["status"] == "completed"
        assert len(events[0]["attempts"]) <= 32
        assert events[0]["attempt_count"] == 65


async def test_auto_name_survives_backend_restart_and_reports_live_limits(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"context_length": 262144, "streams": {"max": 4}})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        rows = (await http.get("/v1/models")).json()["data"]
        assert rows[0]["context_length"] == 262144
        assert rows[0]["max_concurrency"] == 4
        observation = app.state.discovery.observation(engine.id)
        observation.status = "offline"
        observation.models = []
        rows = (await http.get("/v1/models")).json()["data"]
        assert [row["id"] for row in rows] == ["auto"]


async def test_stream_that_delivered_output_is_never_replayed(tmp_path):
    calls = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("interrupted after output")

    async def upstream(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            calls += 1
            return httpx.Response(200, stream=Stream(), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "stream": True})
        assert response.text.count("partial") == 1
        assert calls == 1
        assert app.state.discovery.observation(engine.id).inflight == 0


async def test_disconnected_recovery_stream_releases_its_upstream(tmp_path):
    from test_transport import serving

    handed_off = asyncio.Event()
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            await asyncio.Future()

        async def aclose(self):
            closed.set()

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, stream=Stream(), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    config = app.state.store.config()
    config.routes[0].recovery_seconds = None
    app.state.store.save(config)
    original = app.state.proxy.dispatch

    async def observe_handoff(*args):
        response = await original(*args)
        handed_off.set()
        return response

    app.state.proxy.dispatch = observe_handoff
    async with http, serving(app) as url, httpx.AsyncClient(base_url=url, headers=http.headers) as socket_client:
            async with socket_client.stream("POST", "/v1/chat/completions", json={"model": "auto", "stream": True}):
                await asyncio.wait_for(handed_off.wait(), 3)
            await asyncio.wait_for(closed.wait(), 3)
            assert app.state.discovery.observation(engine.id).inflight == 0


async def test_collector_finds_changed_port_and_resumes_a_real_request(tmp_path):
    from fastapi import FastAPI
    from test_transport import serving

    from gateway.network.collector import Collector
    from gateway.network.scanner import scan

    def backend(name, context, capacity):
        service = FastAPI()

        @service.get("/v1/models")
        async def models():
            return {"data": [{"id": name}]}

        @service.get("/health")
        async def health():
            return {"context_length": context, "streams": {"max": capacity}}

        @service.post("/v1/chat/completions")
        async def complete():
            return {"model": name, "choices": [{"message": {"content": name}}]}

        return service

    old_server = serving(backend("before", 4096, 2))
    old_url = await old_server.__aenter__()
    old_closed = False
    tasks = []
    try:
        async with serving(backend("after", 8192, 3)) as new_url:
            # The actual scanner and HTTP recognizer inspect only these two
            # controlled fixture listeners. No unrelated host ports are probed.
            ports = old_url.rsplit(":", 1)[1] + "," + new_url.rsplit(":", 1)[1]

            async def fixture_scan(targets, **options):
                assert options["ports"] == "1-65535"
                options["ports"] = ports
                async for result in scan(targets, **options):
                    yield result

            app = create_app(str(tmp_path), background=False)
            engine = Engine(name="Initial endpoint", base_url=old_url + "/v1")
            caller = Client(name="Socket caller")
            app.state.store.save(Configuration(
                engines=[engine], clients=[caller],
                routes=[Route(name="auto", primary=Selector(hosts=["127.0.0.1"]), recovery_seconds=15)],
                discovery=Discovery(enabled=True, targets=["127.0.0.1"], mdns=False, refresh_seconds=2),
            ))
            key = app.state.store.issue_key(caller.id)
            await app.state.discovery.refresh()
            async with (
                serving(app) as router_url,
                httpx.AsyncClient(base_url=router_url, headers={"Authorization": "Bearer " + key}, timeout=20) as http,
            ):
                    payload = {"model": "auto", "messages": []}
                    assert (await http.post("/v1/chat/completions", json=payload)).json()["model"] == "before"
                    await old_server.__aexit__(None, None, None)
                    old_closed = True
                    collector = Collector(app.state.store.discovery_directory, runner=fixture_scan)
                    tasks.append(asyncio.create_task(collector.watch_followed_hosts()))
                    response = await http.post("/v1/chat/completions", json=payload)
                    assert response.status_code == 200
                    assert response.json()["model"] == "after"
                    catalog = (await http.get("/v1/models")).json()["data"][0]
                    assert catalog["context_length"] == 8192
                    assert catalog["max_concurrency"] == 3
                    assert len(app.state.store.events()) == 2
                    assert all(event["status"] == "completed" for event in app.state.store.events())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not old_closed:
            await old_server.__aexit__(None, None, None)


async def test_stream_that_emitted_bytes_is_not_replayed_after_interruption(tmp_path):
    calls = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            raise httpx.ReadTimeout("interrupted after the first frame")

    async def upstream(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            calls += 1
            return httpx.Response(200, stream=Stream(), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(404)

    app, engine, _, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "messages": [], "stream": True})
        assert response.status_code == 200
        assert response.text.count('"role":"assistant"') == 1
        assert "Upstream stream interrupted" in response.text
        assert "[DONE]" not in response.text
        assert calls == 1
        event = app.state.store.events()[0]
        assert event["status"] == "failed"
        assert event["http_status"] == 502
        assert app.state.discovery.observation(engine.id).inflight == 0


async def test_stream_request_with_json_upstream_body_returns_bounded_json(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={"model": "current-model", "choices": [{"message": {"content": "bounded"}}]},
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404)

    app, _, _, http = await router(tmp_path, upstream)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "messages": [], "stream": True})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["choices"][0]["message"]["content"] == "bounded"
        assert "data:" not in response.text
        assert "[DONE]" not in response.text


async def test_recovery_stream_refusal_is_an_sse_error_without_done(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        return httpx.Response(404)

    app, _, _, http = await router(tmp_path, upstream, managed=True)
    config = app.state.store.config()
    config.routes[0].primary = Selector(hosts=["192.0.2.99"])
    config.routes[0].recovery_seconds = 0.01
    app.state.store.save(config)
    async with app.router.lifespan_context(app), http:
        response = await http.post("/v1/chat/completions", json={"model": "auto", "messages": [], "stream": True})
        assert response.status_code == 200
        assert response.text.startswith('data: {"error"')
        assert "No allowed, available model matches this request" in response.text
        assert "[DONE]" not in response.text


async def test_recovery_deadline_error_has_no_success_terminator(tmp_path):
    class DyingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout("no frames survive")
            yield b""

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        return httpx.Response(
            200, stream=DyingStream(), headers={"Content-Type": "text/event-stream"}
        )

    app, engine, client, http = await router(tmp_path, upstream, managed=True)
    async with app.router.lifespan_context(app), http:
        response = await http.post(
            "/v1/chat/completions", json={"model": "auto", "messages": [], "stream": True}
        )
        assert response.status_code == 200
        # The refusal after the recovery window is an error event; it must
        # never carry the success terminator.
        assert "All permitted engines were unavailable" in response.text
        assert "[DONE]" not in response.text
