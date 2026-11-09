"""Origin refusals must be HTTP 403 in both stream modes, and stream error
events must never carry the success terminator."""

import json

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Engine, Route, Security, Selector

POLICY_IP = "192.0.2.66"   # TEST-NET-1, documentation range


def write_policy(tmp_path, monkeypatch):
    policy = tmp_path / "origin_policy.json"
    origin = {"name": "restricted-advisor",
              "source_networks": [f"{POLICY_IP}/32"],
              "allowed_routes": ["other"]}
    policy.write_text(json.dumps({"origins": [origin], "tag_resolver": None}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(policy))
    monkeypatch.setenv("MODEL_ROUTER_MAINTENANCE_FLAG", str(tmp_path / "global.flag"))


def make_app(tmp_path, seen):
    def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "served"}]})
        seen.append(request.url.path)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    app = create_app(str(tmp_path / "state"), background=False,
                     transport=httpx.MockTransport(upstream))
    engine = Engine(name="API", base_url="http://provider.test/v1")
    shared = Client(name="Shared access", route_names=["auto", "other"])
    app.state.store.save(Configuration(
        engines=[engine],
        routes=[
            Route(name="auto", primary=Selector(engine_ids=[engine.id]),
                  recovery_seconds=None),
            Route(name="other", primary=Selector(engine_ids=[engine.id])),
        ],
        clients=[shared],
        security=Security(operator_auth_enabled=False, anonymous_client_id=shared.id),
    ))
    return app


async def post_from(app, ip, payload):
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 1)),
        base_url="http://router.test",
    ) as http:
        return await http.post("/v1/chat/completions", json=payload)


@pytest.mark.asyncio
async def test_forbidden_origin_stream_returns_403(tmp_path, monkeypatch):
    seen = []
    write_policy(tmp_path, monkeypatch)
    app = make_app(tmp_path, seen)
    response = await post_from(app, POLICY_IP,
                               {"model": "auto", "messages": [], "stream": True})
    assert response.status_code == 403
    assert "This origin may only use these routes" in response.text
    assert [p for p in seen if p != "/health"] == []


@pytest.mark.asyncio
async def test_forbidden_origin_nonstream_returns_403(tmp_path, monkeypatch):
    seen = []
    write_policy(tmp_path, monkeypatch)
    app = make_app(tmp_path, seen)
    response = await post_from(app, POLICY_IP,
                               {"model": "auto", "messages": [], "stream": False})
    assert response.status_code == 403
    assert [p for p in seen if p != "/health"] == []


@pytest.mark.asyncio
async def test_allowed_origin_stream_reaches_upstream(tmp_path, monkeypatch):
    seen = []
    write_policy(tmp_path, monkeypatch)
    app = make_app(tmp_path, seen)
    response = await post_from(app, POLICY_IP,
                               {"model": "other", "messages": [], "stream": True})
    assert response.status_code == 200
    assert [p for p in seen if p != "/health"] == ["/v1/chat/completions"]
