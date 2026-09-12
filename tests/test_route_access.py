import json

import httpx
import pytest

from gateway.app import create_app
from gateway.migration import configuration as migrate_configuration
from gateway.schema import Client, Configuration, Engine, Route, Security, Selector
from gateway.store import Store


class Endpoint:
    async def __call__(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": payload["model"], "choices": [{"message": {"content": "ok"}}]},
        )


@pytest.mark.asyncio
async def test_unkeyed_callers_are_observed_and_filtered_by_route_gate(tmp_path):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(Endpoint())
    )
    engine = Engine(name="Local endpoint", base_url="http://model.test/v1")
    cloud = Engine(
        name="Cloud endpoint",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    app.state.store.save(
        Configuration(
            engines=[engine, cloud],
            routes=[
                Route(
                    name="free",
                    primary=Selector(engine_ids=[engine.id]),
                    require_caller_key=False,
                ),
                Route(
                    name="cloud-free",
                    primary=Selector(kind="cloud", engine_ids=[cloud.id]),
                    require_caller_key=False,
                ),
                Route(
                    name="private",
                    primary=Selector(engine_ids=[engine.id]),
                    require_caller_key=True,
                ),
            ],
            security=Security(operator_auth_enabled=False),
        )
    )
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.20", 1)),
            base_url="http://router.test",
            headers={"User-Agent": "EdgeHarness/1"},
        ) as client,
    ):
        catalog = await client.get("/v1/models")
        assert catalog.status_code == 200
        assert {item["id"] for item in catalog.json()["data"]} == {
            "free",
            "cloud-free",
        }
        allowed = await client.post(
            "/v1/chat/completions",
            json={"model": "free", "messages": [{"role": "user", "content": "hi"}]},
        )
        cloud_allowed = await client.post(
            "/v1/chat/completions",
            json={
                "model": "cloud-free",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        denied = await client.post(
            "/v1/chat/completions",
            json={"model": "private", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert allowed.status_code == 200
    assert cloud_allowed.status_code == 200
    assert denied.status_code == 401
    callers = Store(str(tmp_path)).callers()
    assert len(callers) == 1
    assert callers[0]["policy_id"] is None
    assert callers[0]["source_address"] == "192.0.2.20"
    assert callers[0]["identity_basis"] == "shared_access"


@pytest.mark.asyncio
async def test_one_key_policy_can_use_multiple_routes(tmp_path):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(Endpoint())
    )
    engine = Engine(name="Local endpoint", base_url="http://model.test/v1")
    cloud = Engine(
        name="Cloud endpoint",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    caller = Client(
        name="Harness key",
        route_names=["free", "cloud-private", "private"],
        allow_cloud=True,
    )
    app.state.store.save(
        Configuration(
            engines=[engine, cloud],
            routes=[
                Route(name="free", primary=Selector(engine_ids=[engine.id])),
                Route(
                    name="cloud-private",
                    primary=Selector(kind="cloud", engine_ids=[cloud.id]),
                    require_caller_key=True,
                ),
                Route(
                    name="private",
                    primary=Selector(engine_ids=[engine.id]),
                    require_caller_key=True,
                ),
            ],
            clients=[caller],
            security=Security(operator_auth_enabled=False),
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.21", 1)),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        ) as client,
    ):
        catalog = await client.get("/v1/models")
        private = await client.post(
            "/v1/chat/completions",
            json={"model": "private", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert {item["id"] for item in catalog.json()["data"]} == {
        "free",
        "cloud-private",
        "private",
    }
    assert private.status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.23", 1)),
        base_url="http://router.test",
        headers={"Authorization": f"Bearer {key}"},
    ) as second_source:
        assert (await second_source.get("/v1/models")).status_code == 200
    callers = Store(str(tmp_path)).callers()
    assert len(callers) == 2
    assert {item["policy_id"] for item in callers} == {caller.id}


@pytest.mark.asyncio
async def test_invalid_key_uses_open_route_without_becoming_a_policy(tmp_path):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(
        Configuration(
            routes=[Route(name="free", require_caller_key=False)],
            security=Security(operator_auth_enabled=False),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.22", 1)),
            base_url="http://router.test",
            headers={"Authorization": "Bearer invalid", "User-Agent": "BadKey/1"},
        ) as client,
    ):
        response = await client.get("/v1/models")
    assert response.status_code == 200
    caller = Store(str(tmp_path)).callers()[0]
    assert caller["policy_id"] is None
    assert caller["identity_basis"] == "unassigned"


@pytest.mark.asyncio
async def test_invalid_key_still_fails_a_gated_route(tmp_path):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(
        Configuration(
            routes=[Route(name="private", require_caller_key=True)],
            security=Security(operator_auth_enabled=False),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.23", 1)),
            base_url="http://router.test",
            headers={"Authorization": "Bearer invalid", "User-Agent": "BadKey/1"},
        ) as client,
    ):
        response = await client.get("/v1/models")
    assert response.status_code == 401
    caller = Store(str(tmp_path)).callers()[0]
    assert caller["policy_id"] is None
    assert caller["identity_basis"] == "unassigned"


@pytest.mark.asyncio
async def test_placeholder_header_matches_default_policy_without_claiming_it(tmp_path):
    app = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(Endpoint()),
    )
    engine = Engine(name="Local endpoint", base_url="http://model.test/v1")
    default = Client(name="Shared policy", route_names=["free"], allow_cloud=False)
    app.state.store.save(
        Configuration(
            engines=[engine],
            routes=[
                Route(name="free", primary=Selector(engine_ids=[engine.id])),
                Route(name="other", primary=Selector(engine_ids=[engine.id])),
            ],
            clients=[default],
            security=Security(
                operator_auth_enabled=False,
                anonymous_client_id=default.id,
            ),
        )
    )
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.24", 1)),
            base_url="http://router.test",
        ) as client,
    ):
        unkeyed = await client.get("/v1/models")
        placeholder = await client.get(
            "/v1/models", headers={"Authorization": "Bearer placeholder"}
        )
    assert {item["id"] for item in unkeyed.json()["data"]} == {"free"}
    assert {item["id"] for item in placeholder.json()["data"]} == {"free"}
    callers = Store(str(tmp_path)).callers()
    assert {caller["identity_basis"] for caller in callers} == {
        "shared_access",
        "unassigned",
    }
    assert all(
        caller["policy_id"] in {None, default.id}
        for caller in callers
    )
    assert any(
        caller["identity_basis"] == "unassigned" and caller["policy_id"] is None
        for caller in callers
    )


@pytest.mark.asyncio
async def test_valid_key_keeps_source_restriction_on_an_open_route(tmp_path):
    app = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(Endpoint()),
    )
    engine = Engine(name="Local endpoint", base_url="http://model.test/v1")
    caller = Client(
        name="Restricted key",
        route_names=["free"],
        source_networks=["192.0.2.24/32"],
    )
    app.state.store.save(
        Configuration(
            engines=[engine],
            routes=[Route(name="free", primary=Selector(engine_ids=[engine.id]))],
            clients=[caller],
            security=Security(operator_auth_enabled=False),
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.25", 1)),
        base_url="http://router.test",
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive_for_valid_keys(tmp_path):
    app = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(Endpoint()),
    )
    engine = Engine(name="Local endpoint", base_url="http://model.test/v1")
    caller = Client(name="Key", route_names=["private"])
    app.state.store.save(
        Configuration(
            engines=[engine],
            routes=[
                Route(name="free", primary=Selector(engine_ids=[engine.id])),
                Route(
                    name="private",
                    primary=Selector(engine_ids=[engine.id]),
                    require_caller_key=True,
                ),
            ],
            clients=[caller],
            security=Security(operator_auth_enabled=False),
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.26", 1)),
        base_url="http://router.test",
        headers={"Authorization": f"bearer {key}"},
    ) as client:
        response = await client.get("/v1/models")
    assert {item["id"] for item in response.json()["data"]} == {"private"}


def test_pre_route_gate_config_materializes_legacy_global_policy():
    route = Route(name="auto", require_caller_key=True)
    raw = Configuration(routes=[route], security=Security(client_auth_enabled=True)).model_dump()
    raw["schema_version"] = 3
    raw["routes"][0].pop("require_caller_key")
    migrated = migrate_configuration(raw)
    assert migrated["schema_version"] == 4
    assert migrated["routes"][0]["require_caller_key"] is True

    raw["security"]["client_auth_enabled"] = False
    migrated = migrate_configuration(raw)
    assert migrated["routes"][0]["require_caller_key"] is False
