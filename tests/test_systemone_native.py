"""Exact codec/schema/ASGI fixtures; no model, provider or production state."""
import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from gateway.adapters import jevos_native as wire
from gateway.app import create_app
from gateway.schema import Client, Configuration, Engine, NativeOwnerSettings, Route, Security, Selector
from gateway.systemone_native import producer_headers

FP, BIN = "a" * 64, "b" * 64
HEALTH = {"status": "ready", "model": "fixture-native", "fingerprint": FP}
TIMING = "parse;dur=0.1, queue;dur=0, inference;dur=1.0, total;dur=1.5"
PAYLOAD = {"model": "sys1_presence", "state": {"transcript": "INERT_PRIVATE_CASE_MARKER"},
           "questions": {"address": {"type": "noul", "instructions": "An inert semantic question?"}}}
ANSWER = {"model": "fixture-native", "answers": {"address": {"type": "noul", "noul": .75}},
          "usage": {"input_tokens": 22, "output_tokens": 0}}


def engine_args(base_url="http://127.0.0.1:18001"):
    return dict(name="fixture native", base_url=base_url, protocol="jevos_native",
                max_inflight=1, timeout_seconds=1, max_response_bytes=4096,
                native_owner=NativeOwnerSettings(expected_fingerprint=FP, expected_binary_sha256=BIN,
                                                 owner_unit="fixture-native.service"))


def configured(tmp_path, handler=None, base_url="http://127.0.0.1:18001", probe=None, exit_probe=None):
    native = Engine(**engine_args(base_url))
    def inspect(value):
        return {"unit": "fixture-native.service", "pid": 999999999, "starttoken": "123",
                "cgroup": "/user.slice/user-1000.slice/fixture-native.service",
                "binary_sha256": BIN, "fingerprint": FP, "endpoint": value.base_url}
    app = create_app(str(tmp_path), background=False,
                     transport=httpx.MockTransport(handler) if handler else None,
                     native_probe=probe or inspect, native_exit_probe=exit_probe)
    existing = next((e for e in app.state.store.config().engines if e.protocol == "jevos_native"), None)
    if existing is not None:
        return app, existing
    caller = Client(name="inert unkeyed fixture", route_names=["sys1_presence", "sys1", "auto"])
    route = Route(name="sys1_presence", primary=Selector(kind="local", engine_ids=[native.id]))
    app.state.store.save(Configuration(engines=[native], clients=[caller], routes=[route],
                                       security=Security(anonymous_client_id=caller.id)))
    return app, native


def handler(request):
    if request.method == "GET":
        return httpx.Response(200, json=HEALTH)
    assert request.url.path == "/v1/systemone"
    actual = json.loads(request.content)
    assert actual == {**PAYLOAD, "model": "fixture-native"}
    return httpx.Response(200, content=json.dumps(ANSWER).encode(), headers={"Server-Timing": TIMING})


@pytest.mark.parametrize("change", [
    lambda p: p.update(stream=True), lambda p: p.update(model=None),
    lambda p: p["questions"].update(second={"type": "noul", "instructions": "other"}),
    lambda p: p["questions"]["address"].update(criteria={"true": "ignored"}),
    lambda p: p["questions"]["address"].update(type="choice"),
    lambda p: p["questions"]["address"].update(instructions=""),
    lambda p: p.update(state={"x": float("nan")}),
    lambda p: p.update(state="x" * 8192),
    lambda p: p.update(state={str(i): i for i in range(300)}),
    lambda p: p.update(state=[[[[[[[[[["deep"]]]]]]]]]]),
])
def test_native_request_rejects_unsupported_or_unbounded(change):
    value = copy.deepcopy(PAYLOAD)
    change(value)
    with pytest.raises(wire.NativeFault):
        wire.request_body(value)


@pytest.mark.parametrize("change", [
    lambda b: b.update(api_version="fake-v1"), lambda b: b.update(model="foreign"),
    lambda b: b["answers"].update(other=b["answers"].pop("address")),
    lambda b: b["answers"]["address"].update(type="boolean"),
    lambda b: b["answers"]["address"].update(noul=True),
    lambda b: b["answers"]["address"].update(noul=float("nan")),
    lambda b: b["answers"]["address"].update(noul=1.1),
    lambda b: b["usage"].update(input_tokens=2049),
    lambda b: b["usage"].update(input_tokens=True),
    lambda b: b["usage"].update(output_tokens=1),
    lambda b: b["usage"].update(truncated_inputs=0),
])
def test_native_response_cannot_fabricate_v1_or_decode(change):
    value = copy.deepcopy(ANSWER)
    change(value)
    with pytest.raises(wire.NativeFault):
        wire.response_body(json.dumps(value), "address", "fixture-native", 2048)


@pytest.mark.parametrize("changes", [
    {"kind": "cloud"}, {"base_url": "http://example.test:8018"},
    {"base_url": "http://127.0.0.1:8018/v1"}, {"max_inflight": 2},
    {"timeout_seconds": 11}, {"aliases": ["http://127.0.0.1:8019"]},
    {"value_mappings": {"instructions": {"a": "b"}}}, {"credential_type": "xai_oauth"},
])
def test_native_engine_requires_bounded_local_contract(changes):
    with pytest.raises(ValidationError):
        Engine(**{**engine_args(), **changes})


def test_private_future_credential_contract(tmp_path):
    path = tmp_path / "fixture.key"
    path.write_text("inert-fixture-only"); path.chmod(0o600)
    assert producer_headers(None) == {}
    assert producer_headers(str(path)) == {"Authorization": "Bearer inert-fixture-only"}
    path.chmod(0o644)
    with pytest.raises(wire.NativeFault):
        producer_headers(str(path))
    link = tmp_path / "link"; link.symlink_to(path)
    with pytest.raises(wire.NativeFault):
        producer_headers(str(link))


async def test_actual_asgi_native_wire_usage_catalog_and_private_metadata(tmp_path):
    app, engine = configured(tmp_path, handler)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            catalog = (await client.get("/v1/models")).json()["data"]
            assert catalog[0]["serving"] == "systemone-native"
            response = await client.post("/v1/systemone", json=PAYLOAD)
            assert response.status_code == 200, response.text
            assert response.content == json.dumps(ANSWER).encode()
            assert response.headers["x-router-model"] == "fixture-native"
            assert response.headers["x-router-contract"] == "jevos.noul.v4"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["server-timing"] == TIMING
            row = app.state.native_owner._sql("read")
            assert row["phase"] == "completed" and row["native_usage"] == ANSWER["usage"]
            assert len(json.dumps(row).encode()) <= 2048
            assert "INERT_PRIVATE_CASE_MARKER" not in json.dumps(row)
            assert "INERT_PRIVATE_CASE_MARKER" not in json.dumps(app.state.store.events())
            assert app.state.discovery.observation(engine.id).inflight == 0


@pytest.mark.parametrize("source", ["192.0.2.9", "unknown"])
async def test_actual_asgi_foreign_and_spoofed_source_never_submit(tmp_path, source):
    calls = []
    def counted(request):
        if request.method == "POST": calls.append(request)
        return handler(request)
    app, engine = configured(tmp_path, counted)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        transport = httpx.ASGITransport(app=app, client=(source, 123))
        async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as client:
            response = await client.post("/v1/systemone", json=PAYLOAD,
                                         headers={"X-Forwarded-For": "127.0.0.1", "X-Principal": "Cairn"})
            assert response.status_code == 403
    assert not calls


async def test_actual_asgi_unsupported_criteria_rejected_without_post(tmp_path):
    posts = []
    def count(request):
        if request.method == "POST": posts.append(request)
        return handler(request)
    app, engine = configured(tmp_path, count)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            payload = copy.deepcopy(PAYLOAD)
            payload["questions"]["address"]["criteria"] = {"true": "ignored upstream", "false": "also ignored"}
            assert (await client.post("/v1/systemone", json=payload)).status_code == 400
    assert not posts


async def test_existing_auto_and_v1_are_unchanged_beside_native(tmp_path):
    posts = []
    def transport(request):
        if request.url.host == "127.0.0.1": return handler(request)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ready", "model": "fixture-v1"}) if request.url.host == "v1.test" else httpx.Response(200, json={"data": [{"id": "fixture-auto"}]})
        posts.append((request.url.host, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"fixture": request.url.host})
    app, native = configured(tmp_path, transport)
    config = app.state.store.config()
    v1 = Engine(name="unchanged V1", base_url="http://v1.test", protocol="systemone", catalog_protocol="systemone")
    auto = Engine(name="unchanged local auto", base_url="http://auto.test/v1")
    config.engines.extend([v1, auto])
    config.routes.extend([Route(name="sys1", primary=Selector(engine_ids=[v1.id])),
                          Route(name="auto", primary=Selector(engine_ids=[auto.id]))])
    app.state.store.save(config)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 200
            old = {"model": "sys1", "state": "old fixture", "questions": [{"type": "boolean", "question": "Old?"}]}
            response = await client.post("/v1/systemone", json=old)
            assert response.status_code == 200, response.text
            assert posts[-1] == ("v1.test", "/api/evaluate", {k: v for k, v in old.items() if k != "model"})
            chat = {"model": "auto", "messages": [{"role": "user", "content": "inert"}]}
            response = await client.post("/v1/chat/completions", json=chat)
            assert response.status_code == 200, response.text
            assert posts[-1] == ("auto.test", "/v1/chat/completions", {**chat, "model": "fixture-auto"})


async def test_local_auto_only_principal_cannot_gain_native_or_voice_grant(tmp_path):
    posts = []
    def count(request):
        if request.method == "POST": posts.append(request)
        return handler(request)
    app, engine = configured(tmp_path, count)
    config = app.state.store.config(); config.clients[0].route_names = ["auto"]
    app.state.store.save(config)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/systemone", json=PAYLOAD)
            assert response.status_code == 403
    assert not posts
