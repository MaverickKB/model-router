"""Admission rejection evidence is bounded while the total stays honest."""

import asyncio

import httpx

from gateway.app import create_app
from gateway.proxy import InflightRequest
from gateway.schema import Client, Configuration, Engine, Route

REJECTIONS = 60


async def test_recovery_rejections_stay_capped_with_running_total(tmp_path, monkeypatch):
    calls = 0
    sleeps = 0
    real_sleep = asyncio.sleep

    async def upstream(request):
        nonlocal calls
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        if request.method == "POST":
            calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Serving endpoint", base_url="http://127.0.0.1:8101/v1", max_inflight=1
    )
    caller = Client(name="Local caller")
    route = Route(name="auto", recovery_seconds=None)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[route])
    )
    key = app.state.store.issue_key(caller.id)
    real_claim = InflightRequest.claim
    claims = 0

    def refuse_capacity(*args, **kwargs):
        nonlocal claims
        claims += 1
        if claims <= REJECTIONS:
            return None
        return real_claim(*args, **kwargs)

    def next_tick(_seconds):
        nonlocal sleeps
        sleeps += 1
        return real_sleep(0)

    monkeypatch.setattr(InflightRequest, "claim", staticmethod(refuse_capacity))

    monkeypatch.setattr("gateway.proxy.asyncio.sleep", next_tick)

    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": "Bearer " + key},
        ) as http:
            response = await http.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "x"}]},
            )

    assert response.status_code == 200
    assert calls == 1
    event = app.state.store.events()[0]
    assert event["admission_rejections_total"] == REJECTIONS
    assert len(event["admission_rejections"]) == 50
