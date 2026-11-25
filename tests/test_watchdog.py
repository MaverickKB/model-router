"""Runaway-output watchdog: synthetic repeating, reasoning-only, and normal streams."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from test_transport import serving

from gateway.app import create_app
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    Route,
    Selector,
    WatchdogSettings,
)
from gateway.watchdog import Watchdog, extract_stream_text


def sse(content=None, reasoning=None, done=False):
    if done:
        return b"data: [DONE]\n\n"
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode() + b"\n\n"


def make_watchdog(**overrides):
    params = dict(
        repeat_window=8,
        max_reasoning_bytes_without_answer=128 * 1024,
        first_token_timeout_seconds=300,
        no_progress_timeout_seconds=120,
    )
    params.update(overrides)
    return Watchdog(**params)


def test_extract_stream_text_splits_content_and_reasoning():
    content, reasoning = extract_stream_text(sse("hi") + sse(reasoning="think"))
    assert content == "hi"
    assert reasoning == "think"


def test_normal_stream_does_not_trip():
    watchdog = make_watchdog()
    for text in ("one ", "two ", "three "):
        assert watchdog.feed(sse(text)) is None
    assert watchdog.feed(sse(done=True)) is None


def test_repeated_text_trips():
    watchdog = make_watchdog(repeat_window=3)
    assert watchdog.feed(sse("again")) is None
    assert watchdog.feed(sse("again")) is None
    assert watchdog.feed(sse("again")) == "repeated_text"


def test_reasoning_without_answer_trips():
    watchdog = make_watchdog(max_reasoning_bytes_without_answer=4)
    assert watchdog.feed(sse(reasoning="12345")) == "reasoning_without_answer"


def test_first_token_timeout_trips():
    watchdog = make_watchdog(first_token_timeout_seconds=0)
    assert watchdog.feed(sse(reasoning="x")) == "first_token_timeout"


def test_answer_clears_reasoning_growth():
    watchdog = make_watchdog(max_reasoning_bytes_without_answer=1)
    watchdog.feed(sse("answer"))
    assert watchdog.feed(sse(reasoning="lots of thinking")) is None


async def test_repeating_stream_is_stopped_and_marked_watchdog(tmp_path):
    async def generate():
        for _ in range(20):
            yield sse("again")
            await asyncio.sleep(0.01)

    upstream = FastAPI()

    @upstream.get("/v1/models")
    async def models():
        return {"data": [{"id": "DeepSeek-V4.1-Flash-1017"}]}

    @upstream.post("/v1/chat/completions")
    async def completion():
        return StreamingResponse(generate(), media_type="text/event-stream")

    async with serving(upstream) as upstream_url:
        app = create_app(str(tmp_path), background=False)
        engine = Engine(
            name="Spark",
            base_url=upstream_url + "/v1",
            watchdog=WatchdogSettings(repeat_window=3),
        )
        client = Client(name="Watchdog test")
        app.state.store.save(
            Configuration(
                engines=[engine],
                clients=[client],
                routes=[
                    Route(
                        name="auto",
                        strategy="ordered",
                        primary=Selector(engine_ids=[engine.id]),
                    )
                ],
                discovery=Discovery(enabled=False, mdns=False),
            )
        )
        key = app.state.store.issue_key(client.id)
        await app.state.discovery.refresh()
        async with (
            serving(app) as url,
            httpx.AsyncClient(
                base_url=url,
                timeout=8,
                headers={"Authorization": f"Bearer {key}"},
            ) as http,
        ):
            async with http.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as response:
                body = (await response.aread()).decode()

            assert '"code": "repeated_text"' in body
            event = app.state.store.events()[0]
            assert event["status"] == "watchdog"
