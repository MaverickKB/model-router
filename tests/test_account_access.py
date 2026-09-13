"""Account keys authenticate as level-derived principals; nothing else changes."""

import json

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import (
    AccountLevel,
    AccountsSettings,
    Client,
    Configuration,
    Engine,
    Route,
    Security,
    Selector,
)
from gateway.security.credentials import digest
from gateway.topology import route_map


class Fleet:
    """Two local engines so a failed attempt has a retry target on another engine."""

    def __init__(self):
        self.models = {
            "local-a.test": ["local-model"],
            "local-b.test": ["local-model"],
            "cloud.test": ["remote"],
        }
        self.calls = []
        self.statuses = []
        self.on_call = None

    async def handle(self, request):
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": m} for m in self.models.get(host, [])]}
            )
        body = json.loads(request.content)
        self.calls.append((host, body["model"]))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )


class Setup:
    def __init__(self, app, fleet, engines, level, account, key, key_record):
        self.app, self.fleet, self.engines = app, fleet, engines
        self.level, self.account, self.key, self.key_record = (
            level,
            account,
            key,
            key_record,
        )
        self.store = app.state.store

    def caller(self, address="192.0.2.50", key=None):
        headers = {"User-Agent": "python-requests/2.33.0"}
        if key is not False:
            headers["Authorization"] = f"Bearer {key or self.key}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url="http://router.test",
            headers=headers,
        )

    def operator(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        )


async def build(tmp_path, *, enabled=True, clients=(), anonymous=None) -> Setup:
    fleet = Fleet()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet.handle)
    )
    store = app.state.store
    local_a = Engine(name="Local A", base_url="http://local-a.test/v1")
    local_b = Engine(name="Local B", base_url="http://local-b.test/v1")
    ordered = Selector(engine_ids=[local_a.id, local_b.id])
    cloud = Engine(
        name="Cloud endpoint",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    level = AccountLevel(
        name="Standard", route_names=["free", "private"], allow_cloud=False
    )
    store.save(
        Configuration(
            engines=[local_a, local_b, cloud],
            routes=[
                Route(name="free", primary=ordered, strategy="ordered"),
                Route(
                    name="private",
                    primary=ordered,
                    strategy="ordered",
                    require_caller_key=True,
                ),
                Route(
                    name="cloud-only",
                    primary=Selector(kind="cloud", engine_ids=[cloud.id]),
                    require_caller_key=True,
                ),
            ],
            clients=list(clients),
            security=Security(
                operator_auth_enabled=False, anonymous_client_id=anonymous
            ),
            accounts=AccountsSettings(enabled=enabled),
            account_levels=[level],
        )
    )
    store.set_secret(cloud.id, "demo-provider-key")
    account = store.create_account("alice", "Alice", level.id)
    token, _ = store.issue_activation(account["id"], "activate")
    assert (
        store.activate_account(token, digest("correct horse battery")) == account["id"]
    )
    key, record = store.create_account_key(account["id"], "laptop")
    await app.state.discovery.refresh()
    return Setup(
        app,
        fleet,
        [local_a, local_b],
        level,
        store.account_snapshot(account["id"]),
        key,
        record,
    )


async def complete(http, model):
    return await http.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


@pytest.mark.asyncio
async def test_account_key_grants_exactly_the_level(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        catalog = await http.get("/v1/models")
        assert catalog.status_code == 200
        assert {item["id"] for item in catalog.json()["data"]} == {"free", "private"}
        private = await complete(http, "private")
        cloud = await complete(http, "cloud-only")
        direct = await complete(http, "local-model")
    assert private.status_code == 200
    assert private.headers["x-router-engine"] == setup.engines[0].id
    assert cloud.status_code == 403
    assert cloud.json()["error"]["message"] == "Route is outside the client's allowlist"
    assert direct.status_code == 403
    assert setup.fleet.calls == [("local-a.test", "local-model")]


@pytest.mark.asyncio
async def test_account_key_is_refused_while_accounts_disabled(tmp_path):
    setup = await build(tmp_path, enabled=False)
    async with setup.caller() as http:
        response = await complete(http, "private")
    assert response.status_code == 401
    assert response.json()["detail"] == "User accounts are not enabled on this router"
    assert setup.fleet.calls == []
    callers = setup.store.callers()
    assert len(callers) == 1
    assert callers[0]["identity_basis"] == "unassigned"
    assert callers[0]["account_id"] is None


@pytest.mark.asyncio
async def test_invalid_account_key_never_falls_through_to_shared_access(tmp_path):
    everyone = Client(name="Everyone", route_names=["free"])
    setup = await build(tmp_path, clients=[everyone], anonymous=everyone.id)
    async with setup.caller(key="mru_invalid") as http:
        account_style = await complete(http, "free")
    async with setup.caller(key="mr_invalid") as http:
        client_style = await complete(http, "free")
    assert account_style.status_code == 401
    assert account_style.json()["detail"] == "Account key is invalid or revoked"
    # A placeholder client key still follows the default policy on an open route.
    assert client_style.status_code == 200
    assert setup.fleet.calls == [("local-a.test", "local-model")]


@pytest.mark.asyncio
async def test_suspended_account_key_is_refused(tmp_path):
    setup = await build(tmp_path)
    setup.store.update_account(setup.account["id"], status="suspended")
    async with setup.caller() as http:
        response = await complete(http, "private")
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended"
    assert setup.fleet.calls == []


@pytest.mark.asyncio
async def test_suspension_and_level_change_apply_before_next_attempt(tmp_path):
    setup = await build(tmp_path)
    setup.fleet.statuses = [503]

    def suspend(call_number):
        if call_number == 1:
            setup.store.update_account(setup.account["id"], status="suspended")

    setup.fleet.on_call = suspend
    async with setup.app.router.lifespan_context(setup.app), setup.caller() as http:
        response = await complete(http, "private")
    # The proxy re-identifies the key before every attempt, so the suspension
    # is reported exactly as it would be on a fresh request.
    assert response.status_code == 403
    assert response.json()["detail"] == "Account is suspended"
    assert len(setup.fleet.calls) == 1
    event = setup.store.events()[0]
    assert event["status"] == "denied" and event["http_status"] == 403

    narrowed = await build(tmp_path / "narrowed")
    narrowed.fleet.statuses = [503]

    def narrow_level(call_number):
        if call_number == 1:
            config = narrowed.store.config()
            config.account_levels[0].route_names = ["free"]
            narrowed.store.save(config)

    narrowed.fleet.on_call = narrow_level
    async with (
        narrowed.app.router.lifespan_context(narrowed.app),
        narrowed.caller() as http,
    ):
        response = await complete(http, "private")
    # The fresh decision excludes the route, so the second engine is never tried.
    assert response.status_code == 503
    assert len(narrowed.fleet.calls) == 1

    control = await build(tmp_path / "control")
    control.fleet.statuses = [503]
    async with (
        control.app.router.lifespan_context(control.app),
        control.caller() as http,
    ):
        response = await complete(http, "private")
    assert response.status_code == 200
    assert [host for host, _ in control.fleet.calls] == ["local-a.test", "local-b.test"]


@pytest.mark.asyncio
async def test_source_network_client_is_still_refreshed_between_attempts(tmp_path):
    lan = Client(
        name="LAN policy",
        route_names=["free"],
        allow_network_auth=True,
        source_networks=["192.0.2.0/24"],
    )
    setup = await build(tmp_path, clients=[lan])
    setup.fleet.statuses = [503]

    def disable(call_number):
        if call_number == 1:
            config = setup.store.config()
            config.clients[0].enabled = False
            setup.store.save(config)

    setup.fleet.on_call = disable
    async with (
        setup.app.router.lifespan_context(setup.app),
        setup.caller(key=False) as http,
    ):
        response = await complete(http, "free")
    assert response.status_code == 503
    assert len(setup.fleet.calls) == 1
    caller = setup.store.callers()[0]
    assert caller["identity_basis"] == "source_network"
    assert caller["policy_id"] == lan.id and caller["account_id"] is None


@pytest.mark.asyncio
async def test_account_caller_is_observed_with_ids_not_material(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        assert (await complete(http, "private")).status_code == 200
    callers = setup.store.callers()
    assert len(callers) == 1
    caller = callers[0]
    assert caller["name"] == "Alice · laptop"
    assert caller["identity_basis"] == "account_key"
    assert caller["identity_quality"] == "policy_key"
    assert caller["policy_id"] == setup.account["id"]
    assert caller["account_id"] == setup.account["id"]
    assert caller["key_id"] == setup.key_record["id"]
    assert caller["device_id"] is None
    serialized = json.dumps([callers, setup.store.events()])
    assert setup.key not in serialized and setup.key[4:] not in serialized


@pytest.mark.asyncio
async def test_route_map_groups_account_callers_and_keeps_unassigned_unchanged(
    tmp_path,
):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        assert (await complete(http, "private")).status_code == 200
    async with setup.caller(address="192.0.2.51", key=False) as http:
        assert (await complete(http, "free")).status_code == 200
    store, config = setup.store, setup.store.config()
    views = setup.app.state.discovery.views()
    topology = route_map(config, views, store.callers(), store.accounts())
    routes = {route.name: route.id for route in config.routes}

    [group] = topology["account_callers"]
    assert group["account_id"] == setup.account["id"]
    assert group["name"] == "Alice" and group["level_id"] == setup.level.id
    [observed] = group["observed_callers"]
    assert observed["identity_basis"] == "account_key"
    [unassigned] = [c for c in store.callers() if c["identity_basis"] == "shared_access"]
    assert topology["policies"] == []
    edges = {
        edge["route_id"]: edge
        for edge in topology["caller_routes"]
        if edge["caller_id"] == observed["id"]
    }
    assert edges[routes["private"]]["ready_engines"] == [e.id for e in setup.engines]
    assert edges[routes["private"]]["policy_id"] == setup.account["id"]
    assert edges[routes["cloud-only"]]["ready_engines"] == []
    assert (
        edges[routes["cloud-only"]]["reason"]
        == "Route is outside the client's allowlist"
    )
    unkeyed_edges = [
        edge
        for edge in topology["caller_routes"]
        if edge["caller_id"] == unassigned["id"]
    ]
    assert len(unkeyed_edges) == len(config.routes)
    assert all(edge["policy_id"] is None for edge in unkeyed_edges)

    disabled = config.model_copy(
        update={"accounts": AccountsSettings(enabled=False)}, deep=True
    )
    inert = route_map(disabled, views, store.callers(), store.accounts())
    assert [g["account_id"] for g in inert["account_callers"]] == [setup.account["id"]]
    assert all(
        edge["reason"] == "User accounts are disabled" and edge["ready_engines"] == []
        for edge in inert["caller_routes"]
        if edge["caller_id"] == observed["id"]
    )

    async with setup.operator() as http:
        state = await http.get("/api/v1/state")
    assert state.status_code == 200
    assert [g["account_id"] for g in state.json()["route_map"]["account_callers"]] == [
        setup.account["id"]
    ]


@pytest.mark.asyncio
async def test_explain_accepts_account_id(tmp_path):
    setup = await build(tmp_path)
    async with setup.operator() as http:
        allowed = await http.post(
            "/api/v1/explain",
            json={"account_id": setup.account["id"], "payload": {"model": "private"}},
        )
        outside = await http.post(
            "/api/v1/explain",
            json={
                "account_id": setup.account["id"],
                "payload": {"model": "cloud-only"},
            },
        )
        unknown = await http.post(
            "/api/v1/explain", json={"account_id": "nope", "payload": {"model": "free"}}
        )
        neither = await http.post(
            "/api/v1/explain", json={"payload": {"model": "free"}}
        )
    assert allowed.status_code == 200
    assert {c["engine_id"] for c in allowed.json()["candidates"]} == {
        e.id for e in setup.engines
    }
    assert outside.status_code == 200
    assert outside.json()["error"] == "Route is outside the client's allowlist"
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "Choose a configured client or account"
    assert neither.status_code == 422


@pytest.mark.asyncio
async def test_try_route_never_carries_account_state(tmp_path):
    harness = Client(name="Harness", route_names=["free"])
    setup = await build(tmp_path, clients=[harness])
    async with setup.app.router.lifespan_context(setup.app), setup.operator() as http:
        response = await http.post(
            "/api/v1/try-route", json={"client_id": harness.id, "route": "free"}
        )
    assert response.status_code == 200
    event = setup.store.events()[0]
    assert "account_id" not in event and event["client_id"] == harness.id
    caller = setup.store.callers()[0]
    assert caller["identity_basis"] == "operator_test"
    assert caller["account_id"] is None and caller["key_id"] is None
