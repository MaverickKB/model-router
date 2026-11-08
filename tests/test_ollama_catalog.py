"""Native Ollama enrichment: bounded concurrency and honest empty capabilities."""

import asyncio
import time

import httpx
import pytest

from gateway.adapters import ollama


def tags(count):
    return httpx.Response(200, json={"models": [{"model": f"m{n}"} for n in range(count)]})


@pytest.mark.asyncio
async def test_show_requests_run_concurrently_and_finish_inside_the_deadline():
    active = {"now": 0, "peak": 0}

    async def handler(request):
        if request.url.path == "/api/tags":
            return tags(8)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.1)
        active["now"] -= 1
        return httpx.Response(200, json={"capabilities": ["embedding"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        started = time.monotonic()
        catalog = await ollama.read_catalog(http, "http://runtime.test:49191/v1", {})
        elapsed = time.monotonic() - started

    assert active["peak"] == ollama.SHOW_CONCURRENCY
    # Eight serial four-second-budget calls would take at least 0.8s here.
    assert elapsed < 0.5
    assert [row["capabilities"] for row in catalog["data"]] == [["embeddings"]] * 8


@pytest.mark.asyncio
async def test_failed_show_declares_no_capabilities():
    async def handler(request):
        if request.url.path == "/api/tags":
            return tags(2)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"model": "m1"}]})
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        catalog = await ollama.read_catalog(http, "http://runtime.test:49191/v1", {})

    assert catalog["data"] == [
        {"id": "m0", "loaded": False, "capabilities": []},
        {"id": "m1", "loaded": True, "capabilities": []},
    ]


@pytest.mark.asyncio
async def test_show_without_a_capability_list_declares_none(monkeypatch):
    async def handler(request):
        if request.url.path == "/api/tags":
            return tags(1)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"details": {"family": "bert"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        catalog = await ollama.read_catalog(http, "http://runtime.test:49191/v1", {})

    assert catalog["data"][0]["capabilities"] == []


@pytest.mark.asyncio
async def test_show_that_misses_the_overall_deadline_declares_no_capabilities(monkeypatch):
    monkeypatch.setattr(ollama, "SHOW_DEADLINE_SECONDS", 0.2)

    async def handler(request):
        if request.url.path == "/api/tags":
            return tags(1)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        await asyncio.sleep(1)
        return httpx.Response(200, json={"capabilities": ["completion"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        started = time.monotonic()
        catalog = await ollama.read_catalog(http, "http://runtime.test:49191/v1", {})
        elapsed = time.monotonic() - started

    assert elapsed < 0.9
    assert catalog["data"][0]["capabilities"] == []
