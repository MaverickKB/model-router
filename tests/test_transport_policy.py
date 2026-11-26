"""Admission and error classification must hold across independent callers."""

import asyncio
import time

import httpx
from pydantic import TypeAdapter

from gateway.app import create_app
from gateway.contracts import Decision, EngineView
from gateway.schema import Client, Configuration, Engine, Route, Selector


async def make_router(tmp_path, handler, **engine_options):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    local = Engine(name="Primary", base_url="http://primary.test/v1", **engine_options)
    backup = Engine(
        name="Backup",
        base_url="http://backup.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    caller = Client(name="Caller", allow_cloud=True)
    app.state.store.save(
        Configuration(
            engines=[local, backup],
            clients=[caller],
            routes=[Route(name="auto", fallback=Selector(kind="cloud"))],
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    return (
        app,
        local,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        ),
    )


async def test_concurrent_admission_reserves_once_and_releases(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        requests.append(request.url.host)
        if request.url.host == "primary.test":
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"ok": True})

    app, local, http = await make_router(tmp_path, handler, max_inflight=1)
    async with app.router.lifespan_context(app), http:
        payload = {"model": "auto", "messages": []}
        first = asyncio.create_task(http.post("/v1/chat/completions", json=payload))
        await asyncio.wait_for(entered.wait(), 2)
        others = await asyncio.gather(
            *(http.post("/v1/chat/completions", json=payload) for _ in range(12))
        )
        assert requests.count("primary.test") == 1
        assert requests.count("backup.test") == 12
        assert all(r.status_code == 200 for r in others)
        assert app.state.discovery.observation(local.id).inflight == 1
        release.set()
        assert (await first).status_code == 200
        assert app.state.discovery.observation(local.id).inflight == 0
        TypeAdapter(list[EngineView]).validate_python(app.state.discovery.views())
        for event in app.state.store.events():
            TypeAdapter(Decision).validate_python(event["decision"])


async def test_wrong_endpoint_404_does_not_invent_model_removal_or_replay(tmp_path):
    calls = []

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "still-advertised"}]})
        calls.append(request.url.host)
        return httpx.Response(404, json={"error": "incorrect completion path"})

    app, local, http = await make_router(tmp_path, handler)
    async with app.router.lifespan_context(app), http:
        result = await http.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert result.status_code == 404
        assert calls == ["primary.test"]
        assert (
            app.state.discovery.observation(local.id).models[0]["id"]
            == "still-advertised"
        )


async def test_upstream_response_limit_and_configured_cooldown(tmp_path):
    mode = {"value": "oversize"}

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.host == "backup.test":
            return httpx.Response(200, json={"ok": True})
        if mode["value"] == "oversize":
            return httpx.Response(200, content=b"x" * 2048)
        return httpx.Response(429)

    app, local, http = await make_router(
        tmp_path, handler, max_response_bytes=1024, rate_limit_cooldown_seconds=37
    )
    async with app.router.lifespan_context(app), http:
        assert (
            await http.post("/v1/chat/completions", json={"model": "auto"})
        ).status_code == 502
        assert app.state.discovery.observation(local.id).inflight == 0
        mode["value"] = "limited"
        before = time.time()
        assert (
            await http.post("/v1/chat/completions", json={"model": "auto"})
        ).status_code == 200
        assert (
            before + 37
            <= app.state.discovery.observation(local.id).circuit_until
            <= time.time() + 37
        )
