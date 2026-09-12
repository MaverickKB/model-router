import json

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Security
from gateway.store import Store
from gateway.topology import route_map


@pytest.mark.asyncio
async def test_observed_sources_are_distinct_from_shared_permission_policy(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Default access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    assert app.state.store.callers() == []
    empty_map = route_map(app.state.store.config(), [], [])
    assert empty_map["caller_routes"][0]["caller_id"] == policy.id
    assert empty_map["policies"][0]["observed_callers"] == []
    for address in ["192.0.2.10", "192.0.2.11"]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(address, 9000)),
            base_url="http://router.test",
        ) as http:
            response = await http.get(
                "/v1/models",
                headers={
                    "User-Agent": "AgentSDK/1",
                    "X-Forwarded-For": "spoofed",
                    "X-Router-Caller": "Unverified label",
                },
            )
            assert response.status_code == 200
    callers = Store(str(tmp_path)).callers()
    assert len(callers) == 2
    assert {c["source_address"] for c in callers} == {"192.0.2.10", "192.0.2.11"}
    assert all(
        c["name"] == "Unverified label"
        and c["identity_basis"] == "shared_access"
        and c["policy_id"] == policy.id
        for c in callers
    )
    assert "spoofed" not in json.dumps(callers)
    assert all(
        e["caller_id"] == policy.id
        for e in route_map(app.state.store.config(), [], callers)["caller_routes"]
    )


@pytest.mark.asyncio
async def test_key_identity_uses_assigned_name_without_changing_optional_auth(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Operator-named test agent", kind="agent")
    shared = Client(name="Default permission policy")
    cfg = app.state.store.save(
        Configuration(
            clients=[policy, shared],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=shared.id,
            ),
        )
    )
    key = app.state.store.issue_key(policy.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.12", 1234)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "SDK/2",
                    "X-Router-Caller": "someone else",
                },
            )
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["name"] == policy.name and caller["identity_basis"] == "api_key"
    assert caller["reported_name"] == "someone else"
    assert key not in json.dumps(caller)
    assert app.state.store.config().security == cfg.security


@pytest.mark.asyncio
async def test_console_preview_is_not_attributed_to_agent(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="An agent")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(operator_auth_enabled=False, client_auth_enabled=False),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    ) as http:
        response = await http.post(
            "/api/v1/try-route",
            json={
                "client_id": policy.id,
                "route": "auto",
                "message": "Do not retain this text",
            },
        )
        assert response.status_code == 503
    caller = app.state.store.callers()[0]
    event = app.state.store.events()[0]
    assert (
        caller["name"] == "Console route test"
        and caller["identity_basis"] == "operator_test"
    )
    assert event["caller"]["id"] == caller["id"]
    assert "Do not retain this text" not in json.dumps([caller, event])
