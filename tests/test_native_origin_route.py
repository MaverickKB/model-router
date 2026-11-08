"""Origin route policy must gate native System One dispatch before native work."""

import json

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import (
    Client,
    Configuration,
    Engine,
    NativeOwnerSettings,
    Route,
    Security,
    Selector,
)

NATIVE_IP = "192.0.2.66"
FP = "a" * 64
BIN = "b" * 64
NATIVE_PAYLOAD = {
    "model": "sys1_presence",
    "state": {"transcript": "inert fixture"},
    "questions": {
        "address": {"type": "noul", "instructions": "An inert semantic question?"}
    },
}


def native_engine():
    return Engine(
        name="fixture native",
        base_url="http://127.0.0.1:18001",
        protocol="jevos_native",
        max_inflight=1,
        timeout_seconds=1,
        max_response_bytes=4096,
        native_owner=NativeOwnerSettings(
            expected_fingerprint=FP,
            expected_binary_sha256=BIN,
            owner_unit="fixture-native.service",
        ),
    )


def make_app(tmp_path, calls):
    async def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"status": "ready", "model": "fixture-native", "fingerprint": FP}
            )
        return httpx.Response(200, json={"data": [{"id": "fixture-native"}]})

    engine = native_engine()
    caller = Client(name="inert unkeyed fixture", route_names=["sys1_presence"])
    route = Route(name="sys1_presence", primary=Selector(kind="local", engine_ids=[engine.id]))
    app = create_app(
        str(tmp_path / "state"),
        background=False,
        transport=httpx.MockTransport(upstream),
    )
    app.state.store.save(
        Configuration(
            engines=[engine],
            clients=[caller],
            routes=[route],
            security=Security(anonymous_client_id=caller.id),
        )
    )
    return app


def write_policy(tmp_path, monkeypatch, allowed_routes):
    policy = tmp_path / "origin_policy.json"
    origin = {
        "name": "fixture origin",
        "source_networks": [f"{NATIVE_IP}/32"],
        "allowed_routes": allowed_routes,
    }
    policy.write_text(json.dumps({"origins": [origin], "tag_resolver": None}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(policy))


@pytest.mark.asyncio
async def test_native_route_outside_origin_allowlist_is_forbidden(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, ["auto"])
    calls = []
    app = make_app(tmp_path, calls)
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(NATIVE_IP, 1)),
            base_url="http://router.test",
        ) as http:
            response = await http.post("/v1/systemone", json=NATIVE_PAYLOAD)
    assert response.status_code == 403
    assert response.json()["detail"] == "This origin may only use these routes: 'auto'"
    assert "/v1/systemone" not in calls


@pytest.mark.asyncio
async def test_native_route_inside_origin_allowlist_reaches_native_checks(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch, ["sys1_presence"])
    calls = []
    app = make_app(tmp_path, calls)
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(NATIVE_IP, 1)),
            base_url="http://router.test",
        ) as http:
            response = await http.post("/v1/systemone", json=NATIVE_PAYLOAD)
    assert response.status_code == 403
    assert response.json()["detail"] == "Native decision ingress is local only"
    assert "/v1/systemone" not in calls
