"""Served requests record engine timing and token counts, never content."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from test_transport import serving

from gateway import performance
from gateway.accounts.limits import Usage
from gateway.app import create_app
from gateway.performance import ServedTiming
from gateway.schema import Configuration, Discovery, Engine, Route, Selector


class Clock:
    """Stands in for the timing module's ``time``; the event loop keeps its own."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class SteppingClock:
    """Every reading is 10 ms after the previous one."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        self.now += 0.01
        return self.now


def test_stream_timing_measures_first_chunk_and_generation_rate(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(performance, "time", clock)
    timing = ServedTiming()
    timing.mark_sent()
    clock.now = 100.25
    timing.mark_chunk()
    clock.now = 100.75
    timing.mark_chunk()
    clock.now = 101.25
    timing.mark_chunk()

    summary = timing.summary(Usage(10, 50, estimated=False))

    assert summary == {
        "stream": True,
        "upstream_ms": 1250,
        "first_chunk_ms": 250,
        "generation_ms": 1000,
        "completion_tokens": 50,
        "tokens_estimated": False,
        "tokens_per_second": 50.0,
    }


def test_a_new_attempt_discards_the_previous_attempt_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(performance, "time", clock)
    timing = ServedTiming()
    timing.mark_sent()
    clock.now = 105.0
    timing.mark_chunk()
    clock.now = 110.0
    timing.mark_sent()
    clock.now = 110.5
    timing.mark_body_read()

    summary = timing.summary(Usage(3, 4, estimated=True))

    assert summary == {
        "stream": False,
        "upstream_ms": 500,
        "first_chunk_ms": None,
        "generation_ms": None,
        "completion_tokens": 4,
        "tokens_estimated": True,
        "tokens_per_second": None,
    }


def test_single_chunk_stream_has_no_generation_rate(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(performance, "time", clock)
    timing = ServedTiming()
    timing.mark_sent()
    clock.now = 100.2
    timing.mark_chunk()

    summary = timing.summary(Usage(1, 1, estimated=False))

    assert summary["first_chunk_ms"] == 200
    assert summary["generation_ms"] == 0
    assert summary["tokens_per_second"] is None


def test_nothing_is_recorded_without_a_served_attempt():
    assert ServedTiming().summary(Usage(0, 0, estimated=False)) is None


def chunk(content: str) -> str:
    return (
        "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n"
    )


async def test_real_http_requests_record_served_engine_timing(tmp_path):
    slow = FastAPI()
    fast = FastAPI()
    received = []

    @slow.get("/v1/models")
    async def slow_models():
        return {"data": [{"id": "shared"}]}

    @slow.post("/v1/chat/completions")
    async def slow_completion():
        await asyncio.sleep(0.4)
        return JSONResponse({"error": "busy"}, status_code=503)

    @fast.get("/v1/models")
    async def fast_models():
        return {"data": [{"id": "shared"}]}

    @fast.post("/v1/chat/completions")
    async def fast_completion(request: Request):
        body = await request.json()
        received.append(body)
        if not body.get("stream"):
            return {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            }
        report = body.get("stream_options", {}).get("include_usage")

        async def generate():
            await asyncio.sleep(0.15)
            yield chunk("one")
            await asyncio.sleep(0.1)
            yield chunk("two")
            if report:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                        }
                    )
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with serving(slow) as slow_url, serving(fast) as fast_url:
        app = create_app(str(tmp_path), background=False)
        first = Engine(name="Busy engine", base_url=slow_url + "/v1")
        second = Engine(name="Serving engine", base_url=fast_url + "/v1")
        app.state.store.save(
            Configuration(
                engines=[first, second],
                routes=[
                    Route(
                        name="auto",
                        strategy="ordered",
                        primary=Selector(engine_ids=[first.id, second.id]),
                    )
                ],
                discovery=Discovery(enabled=False, mdns=False),
            )
        )
        await app.state.discovery.refresh()
        payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        async with (
            serving(app) as url,
            httpx.AsyncClient(base_url=url, timeout=8) as http,
        ):
            plain = await http.post("/v1/chat/completions", json=payload)
            assert plain.status_code == 200
            event = app.state.store.events()[0]
            assert event["engine_id"] == second.id
            recorded = event["performance"]
            assert recorded["stream"] is False
            assert recorded["completion_tokens"] == 7
            assert recorded["tokens_estimated"] is False
            assert recorded["first_chunk_ms"] is None
            assert recorded["tokens_per_second"] is None
            # The failed 0.4 s attempt on the busy engine is not the served
            # engine's latency.
            assert 0 <= recorded["upstream_ms"] < 350

            app.state.discovery.observation(first.id).circuit_until = 0
            streamed = await http.post(
                "/v1/chat/completions", json={**payload, "stream": True}
            )
            assert streamed.status_code == 200
            assert "data: [DONE]" in streamed.text
            event = app.state.store.events()[0]
            recorded = event["performance"]
            assert recorded["stream"] is True
            assert 150 <= recorded["first_chunk_ms"] < 350
            assert recorded["generation_ms"] >= 90
            # The caller did not ask for usage, so the router must not add it:
            # the count is an estimate from the relayed chunks.
            assert "stream_options" not in received[-1]
            assert recorded["tokens_estimated"] is True
            assert recorded["completion_tokens"] == 2
            assert recorded["tokens_per_second"] > 0

            app.state.discovery.observation(first.id).circuit_until = 0
            reported = await http.post(
                "/v1/chat/completions",
                json={
                    **payload,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
            assert reported.status_code == 200
            recorded = app.state.store.events()[0]["performance"]
            assert recorded["completion_tokens"] == 3
            assert recorded["tokens_estimated"] is False

            stored = json.dumps(app.state.store.events())
            assert "hello" not in stored
            assert '"one"' not in stored


async def test_a_stream_delivered_in_one_read_has_no_generation_rate(
    tmp_path, monkeypatch
):
    upstream = FastAPI()

    @upstream.get("/v1/models")
    async def models():
        return {"data": [{"id": "burst"}]}

    @upstream.post("/v1/chat/completions")
    async def completion():
        async def whole():
            # One write: every delta and the terminal event arrive together.
            yield chunk("one") + chunk("two") + chunk("three") + "data: [DONE]\n\n"

        return StreamingResponse(whole(), media_type="text/event-stream")

    async with serving(upstream) as upstream_url:
        app = create_app(str(tmp_path), background=False)
        engine = Engine(name="Burst engine", base_url=upstream_url + "/v1")
        app.state.store.save(
            Configuration(
                engines=[engine], discovery=Discovery(enabled=False, mdns=False)
            )
        )
        await app.state.discovery.refresh()
        # Any second reading of the single chunk would open a 10 ms interval.
        monkeypatch.setattr(performance, "time", SteppingClock())
        async with (
            serving(app) as url,
            httpx.AsyncClient(base_url=url, timeout=8) as http,
        ):
            result = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": True,
                },
            )
            assert result.status_code == 200
            recorded = app.state.store.events()[0]["performance"]
            assert recorded["stream"] is True
            assert recorded["generation_ms"] == 0
            assert recorded["tokens_per_second"] is None
            assert recorded["completion_tokens"] == 3


async def test_failed_requests_record_no_performance(tmp_path):
    upstream = FastAPI()

    @upstream.get("/v1/models")
    async def models():
        return {"data": [{"id": "only"}]}

    @upstream.post("/v1/chat/completions")
    async def completion():
        return JSONResponse({"error": "down"}, status_code=503)

    async with serving(upstream) as upstream_url:
        app = create_app(str(tmp_path), background=False)
        engine = Engine(name="Down engine", base_url=upstream_url + "/v1")
        app.state.store.save(
            Configuration(
                engines=[engine], discovery=Discovery(enabled=False, mdns=False)
            )
        )
        await app.state.discovery.refresh()
        async with (
            serving(app) as url,
            httpx.AsyncClient(base_url=url, timeout=8) as http,
        ):
            result = await http.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "x"}]},
            )
            assert result.status_code == 503
            event = app.state.store.events()[0]
            assert event["status"] == "failed"
            assert "performance" not in event


def mock_engine_app(tmp_path, handle):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handle)
    )
    engine = Engine(name="Mock engine", base_url="http://engine.test/v1")
    app.state.store.save(
        Configuration(engines=[engine], discovery=Discovery(enabled=False, mdns=False))
    )
    return app


def asgi_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5200)),
        base_url="http://localhost",
    )


async def test_deeply_nested_upstream_json_is_relayed_not_failed(tmp_path):
    # Deep enough to exceed the JSON parser's recursion limit on 3.12.
    depth = 60000
    nested = b'{"choices":[],"extra":' + b"[" * depth + b"]" * depth + b"}"
    # The stream meter parses only a 32 KB tail. This depth still fits in that
    # tail and exceeds Python 3.11's default recursion limit.
    tail_depth = 15000
    usage_line = b'data: {"usage":' + b"[" * tail_depth + b"]" * tail_depth + b"}\n\n"

    async def handle(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deep"}]})
        if json.loads(request.content).get("stream"):
            body = chunk("one") + usage_line.decode() + "data: [DONE]\n\n"
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200, content=nested, headers={"content-type": "application/json"}
        )

    app = mock_engine_app(tmp_path, handle)
    await app.state.discovery.refresh()
    payload = {"model": "auto", "messages": [{"role": "user", "content": "x"}]}
    async with asgi_client(app) as http:
        plain = await http.post("/v1/chat/completions", json=payload)
        assert plain.status_code == 200
        assert plain.content == nested
        event = app.state.store.events()[0]
        assert event["status"] == "completed"
        assert event["performance"]["tokens_estimated"] is True

        streamed = await http.post(
            "/v1/chat/completions", json={**payload, "stream": True}
        )
        assert streamed.status_code == 200
        event = app.state.store.events()[0]
        assert event["status"] == "completed"
        assert event["performance"]["tokens_estimated"] is True


async def test_cancellation_during_the_completed_write_drops_performance(
    tmp_path, monkeypatch
):
    async def handle(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    app = mock_engine_app(tmp_path, handle)
    await app.state.discovery.refresh()
    store = app.state.store
    original_event = store.event
    interrupted = []

    def cancel_first_completed_write(event):
        if event.get("status") == "completed" and not interrupted:
            interrupted.append(True)
            raise asyncio.CancelledError
        original_event(event)

    monkeypatch.setattr(store, "event", cancel_first_completed_write)
    async with asgi_client(app) as http:
        # The cancellation propagates out of the handler by design.
        with pytest.raises(asyncio.CancelledError):
            await http.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "x"}]},
            )
    assert interrupted
    event = store.events()[0]
    assert event["status"] == "cancelled"
    assert "performance" not in event
