import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request

from gateway.app import create_app
from gateway.identity import Identity
from gateway.request_policy import resolve_unkeyed_policy
from gateway.routing import decide
from gateway.schema import Client, Configuration, Engine, Route, Security
from gateway.topology import route_map


def available_engine():
    return {
        **Engine(name="Endpoint", base_url="http://provider.test/v1").model_dump(),
        "status": "available",
        "inflight": 0,
        "models": [{"id": "example-model", "capabilities": ["text"], "enabled": True}],
    }


def observation(policy_id=None, basis="unassigned", **extra):
    return {
        "id": "observation",
        "policy_id": policy_id,
        "identity_basis": basis,
        "source_address": "192.0.2.10",
        **extra,
    }


def test_source_override_requires_opt_in_and_precedes_default_by_specificity():
    default = Client(name="Default", route_names=["default"])
    wide = Client(
        name="Network", allow_network_auth=True, source_networks=["192.0.2.0/24"]
    )
    narrow = Client(
        name="Device", allow_network_auth=True, source_networks=["192.0.2.10/32"]
    )
    ignored = Client(name="Key only", source_networks=["192.0.2.10/32"])
    config = Configuration(
        clients=[ignored, default, wide, narrow],
        security=Security(anonymous_client_id=default.id),
    )
    assert resolve_unkeyed_policy(config, "192.0.2.10") == (narrow, "source_network")
    assert resolve_unkeyed_policy(config, "192.0.2.11") == (wide, "source_network")
    assert resolve_unkeyed_policy(config, "198.51.100.10") == (default, "shared_access")


@pytest.mark.asyncio
@pytest.mark.parametrize("source_override", [False, True])
async def test_placeholder_request_and_map_use_same_saved_permission_policy(
    source_override,
):
    policy = Client(
        name="Selected policy",
        route_names=["allowed"],
        source_networks=["192.0.2.10/32"] if source_override else [],
        allow_network_auth=source_override,
    )
    config = Configuration(
        clients=[policy],
        security=Security(anonymous_client_id=None if source_override else policy.id),
        routes=[Route(name="allowed"), Route(name="blocked")],
    )
    identity = object.__new__(Identity)
    identity.verification_slots = asyncio.Semaphore(1)
    identity.store = SimpleNamespace(config=lambda: config, key_client=lambda _: None)
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer placeholder")],
            "client": ("192.0.2.10", 50001),
        }
    )
    selected = await identity.identify(request)
    assert selected == policy
    assert request.state.identity_basis == "unassigned"
    engine = available_engine()
    topology = route_map(config, [engine], [observation()])
    for route in config.routes:
        actual = decide(
            config, [engine], selected, {"model": route.name}, caller_key_present=False
        )
        edge = next(
            edge
            for edge in topology["caller_routes"]
            if edge["caller_id"] == "observation" and edge["route_id"] == route.id
        )
        assert edge["ready_engines"] == [
            candidate["engine_id"] for candidate in actual["candidates"]
        ]
        if route.name == "blocked":
            assert "allowlist" in edge["reason"]


@pytest.mark.parametrize("basis", ["source_network", "shared_access", "api_key"])
def test_observed_policy_path_requires_actual_key_evidence_for_gated_route(basis):
    policy = Client(name="Policy", route_names=["private"])
    config = Configuration(
        clients=[policy], routes=[Route(name="private", require_caller_key=True)]
    )
    topology = route_map(config, [available_engine()], [observation(policy.id, basis)])
    edge = topology["caller_routes"][0]
    assert bool(edge["ready_engines"]) == (basis == "api_key")
    if basis != "api_key":
        assert "key is required" in edge["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "source"])
async def test_rejected_credentials_are_visible_without_a_false_available_path(
    tmp_path, failure
):
    app = create_app(str(tmp_path), background=False)
    policy = Client(
        name="Restricted caller",
        enabled=failure != "disabled",
        source_networks=["198.51.100.10/32"] if failure == "source" else [],
    )
    app.state.store.save(Configuration(clients=[policy]))
    key = app.state.store.issue_key(policy.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 50001)),
        base_url="http://router.test",
    ) as http:
        response = await http.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == (401 if failure == "disabled" else 403)
        caller = app.state.store.callers()[0]
        assert caller["authentication_error"]["status"] == response.status_code
        assert key not in str(caller)
        topology = route_map(app.state.store.config(), [available_engine()], [caller])
        edge = next(
            e for e in topology["caller_routes"] if e["caller_id"] == caller["id"]
        )
        assert edge["ready_engines"] == []
        assert edge["reason"].startswith("Last request rejected:")

        # The next successful request refreshes the same source observation and
        # removes the previous rejection instead of indefinitely marking it denied.
        assert (await http.get("/v1/models")).status_code == 200
        callers = app.state.store.callers()
        assert len(callers) == 1
        assert "authentication_error" not in callers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["disabled", "source", "removed"])
async def test_key_resolution_uses_policy_saved_during_key_verification(changed):
    policy = Client(name="Key policy")
    current = Configuration(clients=[policy])
    replacement = policy.model_copy(deep=True)
    if changed == "disabled":
        replacement.enabled = False
    if changed == "source":
        replacement.source_networks = ["198.51.100.10/32"]

    def verify_key(_):
        nonlocal current
        current = Configuration(clients=[] if changed == "removed" else [replacement])
        return policy.id

    identity = object.__new__(Identity)
    identity.store = SimpleNamespace(config=lambda: current, key_client=verify_key)
    identity.verification_slots = asyncio.Semaphore(1)
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-key")],
            "client": ("192.0.2.10", 50001),
        }
    )
    if changed == "removed":
        selected = await identity.identify(request)
        assert selected.id != policy.id
        assert request.state.caller_key_present is False
    else:
        with pytest.raises(HTTPException) as error:
            await identity.identify(request)
        assert error.value.status_code == (401 if changed == "disabled" else 403)
