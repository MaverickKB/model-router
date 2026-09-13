import asyncio
import json
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Discovery, Engine, Route, Selector
from gateway.store import Store


class Fleet:
    def __init__(self):
        self.models = {"local.test": ["first-model"], "cloud.test": ["remote-model"]}
        self.failure = {}
        self.catalog_failure = set()
        self.calls = []

    async def handle(self, request):
        host = request.url.host
        if request.url.path.endswith("/openapi.json"):
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "/v1"}],
                    "paths": {
                        "/chat/completions": {
                            "post": {
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "model": {"type": "string"},
                                                    "messages": {"type": "array"},
                                                },
                                            }
                                        }
                                    }
                                },
                                "responses": {
                                    "200": {
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {
                                                        "choices": {"type": "array"}
                                                    },
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    },
                },
            )
        if request.url.path.endswith("/models"):
            if host in self.catalog_failure:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": m, "capabilities": {"tools": True, "vision": True}}
                        for m in self.models.get(host, [])
                    ]
                },
            )
        body = json.loads(request.content)
        self.calls.append((host, body, dict(request.headers)))
        if host in self.failure:
            return httpx.Response(
                self.failure[host], json={"error": {"message": "unavailable"}}
            )
        if body["model"] not in self.models[host]:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            },
        )


@pytest.fixture
async def setup(tmp_path):
    fleet = Fleet()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet.handle)
    )
    store, discovery = app.state.store, app.state.discovery
    local = Engine(name="Primary machine", base_url="http://local.test/v1")
    cloud = Engine(
        name="Remote provider",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    client = Client(name="Test application", allow_cloud=True, allow_direct_models=True)
    config = Configuration(
        engines=[local, cloud],
        clients=[client],
        routes=[
            Route(name="auto", fallback=Selector(kind="cloud", model_patterns=["*"]))
        ],
        discovery=Discovery(mdns=False),
    )
    store.save(config)
    key = store.issue_key(client.id)
    store.set_secret(cloud.id, "demo-provider-key")
    await discovery.refresh()

    async def operator_requests(request):
        if (
            request.url.path.startswith("/api/")
            or request.url.path == "/v1/gateway/register"
        ):
            request.headers.pop("Authorization", None)
            request.headers.setdefault("Origin", "http://localhost")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5123)),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {key}"},
            event_hooks={"request": [operator_requests]},
        ) as http,
    ):
        login = await http.post(
            "/api/login",
            json={
                "token": (store.directory / "operator-bootstrap.key")
                .read_text()
                .strip()
            },
        )
        assert login.status_code == 200
        yield app, http, fleet, local, cloud, client


async def send(http, model="auto", **extra):
    return await http.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "private prompt marker"}],
            **extra,
        },
    )


@pytest.mark.asyncio
async def test_built_favicons_are_served_from_the_application_root(
    tmp_path, monkeypatch
):
    assets = tmp_path / "dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<html></html>")
    (assets / "favicon.ico").write_bytes(b"ico fixture")
    (assets / "favicon.svg").write_text("<svg></svg>")
    monkeypatch.setenv("MODEL_ROUTER_UI", str(assets))
    app = create_app(str(tmp_path / "state"), background=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        ico = await http.get("/favicon.ico")
        svg = await http.get("/favicon.svg")
        assert ico.status_code == 200
        assert ico.headers["content-type"].startswith("image/x-icon")
        assert ico.content == b"ico fixture"
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert svg.text == "<svg></svg>"
        (assets / "favicon.ico").unlink()
        assert (await http.get("/favicon.ico")).status_code == 404
        (assets / "favicon.svg").unlink()
        assert (await http.get("/favicon.svg")).status_code == 404


@pytest.mark.asyncio
async def test_model_replacement_preserves_auto_and_removes_old_catalog(setup):
    app, http, fleet, *_ = setup
    first = await send(http)
    assert first.status_code == 200 and first.json()["model"] == "first-model"
    fleet.models["local.test"] = ["replacement-with-unseen-name"]
    await app.state.discovery.refresh()
    second = await send(http)
    assert (
        second.status_code == 200
        and second.json()["model"] == "replacement-with-unseen-name"
    )
    ids = [m["id"] for m in (await http.get("/v1/models")).json()["data"]]
    assert (
        "auto" in ids
        and "first-model" not in ids
        and "replacement-with-unseen-name" in ids
    )
    assert (await send(http, "first-model")).status_code == 404
    assert (await send(http, "*")).status_code == 404


@pytest.mark.asyncio
async def test_cloud_fallback_checks_client_and_does_not_leak_key(setup):
    app, http, fleet, _local, _cloud, _client = setup
    fleet.failure["local.test"] = 503
    result = await send(http)
    assert result.status_code == 200 and result.json()["model"] == "remote-model"
    assert fleet.calls[-1][2]["authorization"] == "Bearer demo-provider-key"
    assert "authorization" not in fleet.calls[0][2]
    events = app.state.store.events()
    assert [a["tier"] for a in events[0]["attempts"]] == ["primary", "fallback"]
    config = app.state.store.config()
    config.clients[0].allow_cloud = False
    app.state.store.save(config)
    fleet.calls.clear()
    assert (await send(http)).status_code == 503
    assert not fleet.calls
    assert (await send(http, "remote-model")).status_code == 403
    assert "remote-model" not in [
        m["id"] for m in (await http.get("/v1/models")).json()["data"]
    ]
    raw = json.dumps(app.state.store.events())
    assert "private prompt marker" not in raw and "demo-provider-key" not in raw


@pytest.mark.asyncio
async def test_route_model_and_engine_permissions_apply_to_all_paths(setup):
    app, http, fleet, _local, cloud, _client = setup
    config = app.state.store.config()
    config.routes.append(
        Route(name="purpose", primary=Selector(kind="cloud", model_patterns=["*"]))
    )
    config.clients[0].engine_ids = [cloud.id]
    config.clients[0].model_patterns = ["remote-*"]
    app.state.store.save(config)
    assert (await send(http, "purpose")).status_code == 403
    assert (await send(http, "first-model")).status_code == 403
    assert (await send(http)).json()["model"] == "remote-model"
    assert all(h == "cloud.test" for h, _, _ in fleet.calls)
    config = app.state.store.config()
    config.clients[0].enabled = False
    app.state.store.save(config)
    assert (await send(http)).status_code == 401


@pytest.mark.asyncio
async def test_invalid_or_revoked_key_keeps_network_policy_unclaimed(setup):
    app, http, fleet, *_ = setup
    config = app.state.store.config()
    config.clients[0].source_networks = ["127.0.0.1/32"]
    config.clients[0].allow_network_auth = True
    config.security.client_auth_enabled = False
    app.state.store.save(config)
    invalid = await http.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer invalid"},
        json={"model": "auto"},
    )
    assert invalid.status_code == 200
    assert "authorization" not in fleet.calls[-1][2]
    assert all(
        caller["policy_id"] != config.clients[0].id
        for caller in app.state.store.callers()
    )
    valid = http.headers.pop("Authorization")
    assert (await send(http)).status_code == 200
    http.headers["Authorization"] = valid
    app.state.store.revoke_keys(config.clients[0].id)
    assert (await send(http)).status_code == 200


@pytest.mark.asyncio
async def test_catalog_failure_and_expiry_remove_advertised_models(setup):
    app, http, fleet, _local, _cloud, *_ = setup
    fleet.catalog_failure = {"local.test", "cloud.test"}
    await app.state.discovery.refresh()
    assert (await http.get("/v1/models")).json()["data"] == []
    assert (await send(http)).status_code == 503
    fleet.catalog_failure.clear()
    await app.state.discovery.refresh()
    for obs in app.state.discovery.observations.values():
        obs.observed_at = time.time() - 1000
    assert (await http.get("/v1/models")).json()["data"] == []


@pytest.mark.asyncio
async def test_config_conflict_persistence_defaults_and_secret_isolation(
    setup, tmp_path
):
    app, http, fleet, _local, _cloud, client = setup
    config = app.state.store.config().model_dump()
    config["routes"][0]["defaults"] = {
        "temperature": 0.3,
        "chat_template_kwargs": {"thinking": False},
    }
    result = await http.put("/api/config", json=config)
    assert result.status_code == 200
    assert (await http.put("/api/config", json=config)).status_code == 409
    await send(http, temperature=0.7)
    assert fleet.calls[-1][1]["temperature"] == 0.7
    assert fleet.calls[-1][1]["chat_template_kwargs"]["thinking"] is False
    state = (await http.get("/api/state")).json()
    assert "demo-provider-key" not in json.dumps(state)
    restored = Store(str(tmp_path))
    assert restored.config().routes[0].defaults["temperature"] == 0.3
    assert restored.has_key(client.id)


@pytest.mark.asyncio
async def test_admission_cap_and_draining(setup):
    app, http, fleet, local, _cloud, *_ = setup
    config = app.state.store.config()
    config.engines[0].max_inflight = 1
    config.clients[0].allow_cloud = False
    app.state.store.save(config)
    app.state.discovery.observation(local.id).inflight = 1
    assert (await send(http)).status_code == 503
    app.state.discovery.observation(local.id).inflight = 0
    config = app.state.store.config()
    config.engines[0].draining = True
    app.state.store.save(config)
    assert (await send(http)).status_code == 503
    assert not fleet.calls


@pytest.mark.asyncio
async def test_registration_verifies_live_catalog_and_obeys_exclusions(setup):
    app, http, fleet, *_ = setup
    config = app.state.store.config()
    config.discovery.targets = ["127.0.0.1"]
    config.discovery.auto_register = True
    app.state.store.save(config)
    fleet.models["127.0.0.1"] = ["discovered-live-model"]
    registered = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.1:9991/v1"}
    )
    assert registered.status_code == 200 and registered.json()["engine_id"]
    assert any(
        e["models"][0]["id"] == "discovered-live-model"
        for e in app.state.discovery.views()
    )
    assert (
        await http.post(
            "/v1/gateway/register", json={"base_url": "http://outside.test/v1"}
        )
    ).status_code == 403
    config = app.state.store.config()
    config.discovery.ignored_urls = ["http://127.0.0.1:9992/v1"]
    app.state.store.save(config)
    assert await app.state.discovery.discover_url("http://127.0.0.1:9992/v1") is None
    fleet.models["127.0.0.1"] = []
    assert await app.state.discovery.discover_url("http://127.0.0.1:9993/v1") is None


@pytest.mark.asyncio
async def test_management_auth_and_origin_boundary(setup):
    app, http, *_ = setup
    config = app.state.store.config().model_dump()
    assert (
        await http.put(
            "/api/config", json=config, headers={"Origin": "https://unrelated.example"}
        )
    ).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 5555)),
        base_url="http://router.test",
    ) as remote:
        assert (await remote.get("/api/state")).status_code == 401
        assert (
            await remote.get("/api/state", headers={"X-Forwarded-For": "127.0.0.1"})
        ).status_code == 401
        assert (
            await remote.post(
                "/api/login",
                json={
                    "token": (app.state.store.directory / "operator-bootstrap.key")
                    .read_text()
                    .strip()
                },
                headers={"Origin": "http://router.test"},
            )
        ).status_code == 200
        assert (await remote.get("/api/state")).status_code == 200


@pytest.mark.asyncio
async def test_capability_selection(setup):
    app, http, _fleet, local, _cloud, *_ = setup
    config = app.state.store.config()
    config.engines[0].capabilities = ["text", "streaming"]
    app.state.store.save(config)
    # Observed capabilities come from the live catalog, with no model-family inference.
    assert (
        await send(
            http,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "ping", "parameters": {"type": "object"}},
                }
            ],
        )
    ).status_code == 200
    app.state.discovery.observation(local.id).models[0]["capabilities"] = ["text"]
    config = app.state.store.config()
    config.clients[0].allow_cloud = False
    app.state.store.save(config)
    failed = await send(
        http, tools=[{"type": "function", "function": {"name": "ping"}}]
    )
    assert failed.status_code == 400
    assert failed.json()["error"]["code"] == "unsupported_capability"
    assert "tools" in failed.json()["error"]["message"]
    event = app.state.store.events()[0]
    assert event["http_status"] == 400 and event["status"] == "failed"
    assert event["attempts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["denied", "offline", "busy", "available"])
async def test_capability_error_preserves_temporary_failures_and_fallback(
    setup, fallback
):
    app, http, fleet, local, cloud, *_ = setup
    app.state.discovery.observation(local.id).models[0]["capabilities"] = [
        "text",
        "streaming",
    ]
    config = app.state.store.config()
    config.clients[0].allow_cloud = fallback != "denied"
    app.state.store.save(config)
    if fallback == "offline":
        app.state.discovery.observation(cloud.id).status = "offline"
        app.state.discovery.observation(cloud.id).models = []
    elif fallback == "busy":
        app.state.discovery.observation(cloud.id).inflight = cloud.max_inflight
    tools = [
        {
            "type": "function",
            "function": {"name": "echo", "parameters": {"type": "object"}},
        }
    ]
    result = await send(http, tools=tools)
    if fallback == "available":
        assert result.status_code == 200
        assert fleet.calls[-1][1]["tools"] == tools
    else:
        assert not fleet.calls
        assert result.status_code == (400 if fallback == "denied" else 503)
        if fallback == "denied":
            assert result.json()["error"]["type"] == "invalid_request_error"
            assert result.json()["error"]["code"] == "unsupported_capability"
        for private_value in (
            local.id,
            cloud.id,
            local.name,
            cloud.name,
            "first-model",
            "remote-model",
        ):
            assert private_value not in result.text


@pytest.mark.asyncio
async def test_plain_catalog_respects_saved_capabilities_for_agent_requests(tmp_path):
    calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    async def endpoint(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "current-model"}]})
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            stream=Stream(),
            headers={"Content-Type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(endpoint)
    )
    engine = Engine(
        name="Configured API",
        base_url="http://provider.test/v1",
        capabilities=["text", "streaming", "vision"],
    )
    app.state.store.save(Configuration(engines=[engine], routes=[Route(name="work")]))
    await app.state.discovery.refresh()
    payload = {
        "model": "work",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "tools": [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ],
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": "Bearer placeholder"},
        ) as http,
    ):
        denied = await http.post("/v1/chat/completions", json=payload)
        assert denied.status_code == 400
        assert not calls
        config = app.state.store.config()
        config.engines[0].capabilities.append("tools")
        app.state.store.save(config)
        await app.state.discovery.refresh()
        accepted = await http.post("/v1/chat/completions", json=payload)
        assert accepted.status_code == 200
        assert "[DONE]" in accepted.text
    assert len(calls) == 1
    assert calls[0] == {**payload, "model": "current-model"}


@pytest.mark.asyncio
async def test_replacement_during_request_refreshes_and_routes_without_client_edit(
    setup,
):
    app, http, fleet, *_ = setup
    fleet.models["local.test"] = ["replacement-before-periodic-refresh"]
    response = await send(http)
    assert response.status_code == 200
    assert response.json()["model"] == "replacement-before-periodic-refresh"
    assert [a.get("http_status") for a in app.state.store.events()[0]["attempts"]] == [
        404,
        200,
    ]


@pytest.mark.asyncio
async def test_disabled_model_visible_to_operator_but_not_routable(setup):
    app, http, _fleet, _local, *_ = setup
    from gateway.schema import ModelSettings

    config = app.state.store.config()
    config.engines[0].model_settings = {"first-model": ModelSettings(enabled=False)}
    config.clients[0].allow_cloud = False
    app.state.store.save(config)
    await app.state.discovery.refresh()
    assert app.state.discovery.views()[0]["models"][0]["enabled"] is False
    assert "first-model" not in [
        m["id"] for m in (await http.get("/v1/models")).json()["data"]
    ]
    assert (await send(http, "first-model")).status_code != 200


@pytest.mark.asyncio
async def test_deleted_client_and_engine_secrets_are_removed(setup):
    app, _http, _fleet, local, cloud, client = setup
    config = app.state.store.config()
    config.engines = [local]
    config.clients = []
    app.state.store.save(config)
    assert not app.state.store.secret(cloud.id)
    assert not app.state.store.has_key(client.id)


@pytest.mark.asyncio
async def test_route_only_client_cannot_bypass_policy_with_raw_model(setup):
    app, http, _fleet, *_ = setup
    config = app.state.store.config()
    config.clients[0].allow_direct_models = False
    app.state.store.save(config)
    assert (await send(http, "first-model")).status_code == 403
    assert (await send(http)).status_code == 200
    assert {m["id"] for m in (await http.get("/v1/models")).json()["data"]} == {"auto"}


@pytest.mark.asyncio
async def test_cloud_catalog_requires_explicit_model_selection(setup):
    app, http, _fleet, _local, _cloud, *_ = setup
    config = app.state.store.config()
    config.engines[1].model_patterns = []
    app.state.store.save(config)
    await app.state.discovery.refresh()
    assert app.state.discovery.views()[1]["status"] == "unconfigured"
    assert (await send(http, "remote-model")).status_code != 200
    config = app.state.store.config()
    config.engines[1].model_patterns = ["remote-*"]
    app.state.store.save(config)
    await app.state.discovery.refresh()
    assert (await send(http, "remote-model")).status_code == 200


@pytest.mark.asyncio
async def test_optional_defaults_and_explicit_client_options_are_distinct(setup):
    app, http, fleet, _local, _cloud, *_ = setup
    config = app.state.store.config()
    config.routes[0].defaults = {"chat_template_kwargs": {"thinking": False}}
    config.engines[1].unsupported_parameters = ["chat_template_kwargs"]
    app.state.store.save(config)
    fleet.failure["local.test"] = 503
    assert (await send(http)).status_code == 200
    assert "chat_template_kwargs" not in fleet.calls[-1][1]
    event = app.state.store.events()[0]
    assert event["attempts"][-1]["skipped_defaults"] == ["chat_template_kwargs"]
    # A client requirement is never silently dropped to make a backup work.
    count = len(fleet.calls)
    assert (
        await send(http, chat_template_kwargs={"thinking": True})
    ).status_code == 503
    assert len(fleet.calls) == count


@pytest.mark.asyncio
async def test_busy_model_remains_listed_while_admission_rejects_new_work(setup):
    app, http, _fleet, local, _cloud, *_ = setup
    config = app.state.store.config()
    config.routes[0].fallback = None
    app.state.store.save(config)
    app.state.discovery.observation(local.id).inflight = local.max_inflight
    assert "auto" in [m["id"] for m in (await http.get("/v1/models")).json()["data"]]
    assert (await send(http)).status_code == 503


@pytest.mark.asyncio
async def test_old_failed_probe_cannot_erase_new_observation(setup):
    app, _http, _fleet, local, *_ = setup
    discovery = app.state.discovery
    original = discovery.probe
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed(engine):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            raise ValueError("old failed observation")
        return [
            {
                "id": "new-observation",
                "capabilities": ["text"],
                "context_length": None,
                "enabled": True,
            }
        ]

    discovery.probe = delayed
    old = asyncio.create_task(discovery.refresh_engine(local))
    await started.wait()
    await discovery.refresh_engine(local)
    release.set()
    await old
    assert discovery.observation(local.id).models[0]["id"] == "new-observation"
    discovery.probe = original


@pytest.mark.asyncio
async def test_manual_discovery_keeps_verified_candidates_and_registration_is_idempotent(
    setup,
):
    app, http, fleet, *_ = setup
    config = app.state.store.config()
    config.discovery.auto_register = False
    config.discovery.targets = ["127.0.0.0/8"]
    app.state.store.save(config)
    fleet.models["127.0.0.2"] = ["newly-found-model"]
    response = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.2:8888/v1"}
    )
    assert response.status_code == 202
    pending = (await http.get("/api/state")).json()["discovery"]["pending"]
    assert pending[0]["models"][0]["id"] == "newly-found-model"
    assert len(app.state.store.config().engines) == 2
    # A different, unverified endpoint cannot inherit another endpoint's pending status.
    failed = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.3:8888/v1"}
    )
    assert failed.status_code == 202
    config = app.state.store.config()
    added = Engine(name="Connected manually", base_url=pending[0]["base_url"])
    config.engines.append(added)
    app.state.store.save(config)
    registered = await http.post(
        "/v1/gateway/register", json={"base_url": added.base_url}
    )
    assert registered.json()["engine_id"] == added.id
    # The unrelated catalog-only endpoint remains a separate manual-review
    # record. It cannot inherit the connected endpoint's engine identity.
    assert [item["base_url"] for item in app.state.discovery.pending_views()] == [
        "http://127.0.0.3:8888/v1"
    ]
    invalid = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.2/v1?key=invalid"}
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "requested", ["not-configured", "first-*", "*", "first-model?"]
)
async def test_unknown_exact_name_is_failed_404_without_model_attempts(
    setup, direct, requested
):
    app, http, fleet, *_ = setup
    config = app.state.store.config()
    config.clients[0].allow_direct_models = direct
    saved = app.state.store.save(config)
    response = await send(http, requested)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "model_not_found"
    assert repr(requested) in error["message"] and "/v1/models" in error["message"]
    assert not fleet.calls
    event = app.state.store.events()[0]
    assert event["status"] == "failed" and event["http_status"] == 404
    assert event["attempts"] == []
    assert event["requested"] == requested
    assert app.state.store.config() == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("eligible_catalog", [False, True])
async def test_missing_name_preserves_uncertainty_only_for_permitted_direct_catalogs(
    setup, direct, eligible_catalog
):
    app, http, fleet, _local, cloud, *_ = setup
    config = app.state.store.config()
    config.clients[0].allow_direct_models = direct
    if not eligible_catalog:
        config.clients[0].engine_ids = [cloud.id]
    app.state.store.save(config)
    fleet.catalog_failure.add("local.test")
    await app.state.discovery.refresh()
    assert app.state.discovery.views()[0]["models"] == []
    response = await send(http, "not-configured")
    expected = 503 if direct and eligible_catalog else 404
    assert response.status_code == expected
    assert not fleet.calls
    event = app.state.store.events()[0]
    assert event["status"] == ("unavailable" if expected == 503 else "failed")
    assert event["attempts"] == []
    if not direct:
        # An unavailable catalog cannot support a claim about model existence.
        # This caller can use configured routes, so identify that missing route.
        message = response.json()["error"]["message"]
        assert "No configured route named" in message
        assert "advertised model" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_known_model_in_temporary_cooldown_is_not_reported_missing(setup, direct):
    app, http, fleet, local, *_ = setup
    config = app.state.store.config()
    config.clients[0].allow_direct_models = direct
    app.state.store.save(config)
    app.state.discovery.observation(local.id).circuit_until = time.time() + 60
    response = await send(http, "first-model")
    assert response.status_code == (503 if direct else 403)
    assert response.json()["error"].get("code") != "model_not_found"
    assert not fleet.calls
    event = app.state.store.events()[0]
    assert event["status"] == ("unavailable" if direct else "denied")
    assert event["attempts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["model[1]", "model?", "*"])
async def test_literal_glob_characters_in_known_model_id_are_exact_names(setup, model):
    app, http, fleet, *_ = setup
    fleet.models["local.test"] = [model, "another-model"]
    await app.state.discovery.refresh()
    response = await send(http, model)
    assert response.status_code == 200
    assert response.json()["model"] == model
    assert len(fleet.calls) == 1 and fleet.calls[0][1]["model"] == model


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["key", "allowlist", "disabled"])
async def test_configured_route_precedes_same_named_model_and_retains_its_gate(
    setup, blocker
):
    app, http, fleet, *_ = setup
    config = app.state.store.config()
    config.routes.append(
        Route(
            name="first-model",
            require_caller_key=blocker == "key",
            enabled=blocker != "disabled",
        )
    )
    if blocker != "allowlist":
        config.clients[0].route_names.append("first-model")
    config.security.anonymous_client_id = config.clients[0].id
    app.state.store.save(config)
    if blocker == "key":
        http.headers.pop("Authorization")
    response = await send(http, "first-model")
    expected = {"key": 401, "allowlist": 403, "disabled": 503}[blocker]
    assert response.status_code == expected
    assert not fleet.calls
    event = app.state.store.events()[0]
    assert event["status"] == ("unavailable" if expected == 503 else "denied")
    assert event["attempts"] == []
