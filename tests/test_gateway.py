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
async def test_built_favicons_are_served_from_the_application_root(tmp_path, monkeypatch):
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
    assert (await send(http, "first-model")).status_code == 503
    assert (await send(http, "*")).status_code == 503


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
    assert (
        await send(http, tools=[{"type": "function", "function": {"name": "ping"}}])
    ).status_code == 503


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
        "/v1/gateway/register", json={"base_url": "http://127.0.0.2:8888"}
    )
    assert response.status_code == 202
    pending = (await http.get("/api/state")).json()["discovery"]["pending"]
    assert pending[0]["models"][0]["id"] == "newly-found-model"
    assert len(app.state.store.config().engines) == 2
    # A different, unverified endpoint cannot inherit another endpoint's pending status.
    failed = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.3:8888"}
    )
    assert failed.status_code == 503
    config = app.state.store.config()
    added = Engine(name="Connected manually", base_url=pending[0]["base_url"])
    config.engines.append(added)
    app.state.store.save(config)
    registered = await http.post(
        "/v1/gateway/register", json={"base_url": added.base_url}
    )
    assert registered.json()["engine_id"] == added.id
    assert app.state.discovery.pending_views() == []
    invalid = await http.post(
        "/v1/gateway/register", json={"base_url": "http://127.0.0.2/v1?key=invalid"}
    )
    assert invalid.status_code == 422
