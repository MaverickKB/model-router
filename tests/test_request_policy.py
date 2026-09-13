import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request

from gateway.app import create_app
from gateway.identity import Identity
from gateway.request_policy import resolve_unkeyed_policy
from gateway.routing import decide
from gateway.schema import Client, Configuration, Engine, Route, Security, Selector
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


def test_observation_edges_do_not_borrow_keys_from_same_policy_or_source():
    policy = Client(name="One permission policy", route_names=["open", "gated"])
    config = Configuration(
        clients=[policy],
        security=Security(anonymous_client_id=policy.id),
        routes=[Route(name="open"), Route(name="gated", require_caller_key=True)],
    )
    callers = [
        observation(policy.id, "api_key", id="keyed-sdk"),
        observation(policy.id, "shared_access", id="unkeyed-sdk"),
        observation(
            policy.id, "api_key", id="another-source", source_address="192.0.2.11"
        ),
    ]
    engine = available_engine()
    topology = route_map(config, [engine], callers)
    edges = {
        (edge["caller_id"], edge["route_id"]): edge
        for edge in topology["caller_routes"]
    }
    assert len(edges) == len(callers) * len(config.routes)
    assert {edge["caller_id"] for edge in edges.values()} == {c["id"] for c in callers}
    assert all(edge["policy_id"] == policy.id for edge in edges.values())
    for caller in callers:
        assert edges[caller["id"], config.routes[0].id]["ready_engines"] == [
            engine["id"]
        ]
        gated = edges[caller["id"], config.routes[1].id]
        assert bool(gated["ready_engines"]) == (caller["identity_basis"] == "api_key")
    assert "key is required" in edges["unkeyed-sdk", config.routes[1].id]["reason"]
    assert all(
        edge["ready_engines"] == [engine["id"]] for edge in topology["policy_routes"]
    )


def test_preconfigured_policy_is_only_a_preview_until_a_connection_exists():
    policy = Client(name="Future caller", route_names=["private"])
    route = Route(name="private", require_caller_key=True)
    config = Configuration(clients=[policy], routes=[route])
    engine = available_engine()
    topology = route_map(config, [engine], [])
    assert topology["caller_routes"] == []
    assert topology["policies"] == [{"policy_id": policy.id, "observed_callers": []}]
    assert topology["policy_routes"] == [
        {
            "policy_id": policy.id,
            "route_id": route.id,
            "ready_engines": [engine["id"]],
            "ready_paths": [{"engine_id": engine["id"], "tier": "primary"}],
            "reason": "Eligible text path",
        }
    ]
    assert "unassigned_callers" not in topology


@pytest.mark.parametrize("basis", ["unassigned", "shared_access", "source_network"])
@pytest.mark.parametrize("source_override", [False, True])
def test_unkeyed_map_uses_current_source_policy_not_observed_assignment(
    basis, source_override
):
    old_policy = Client(name="Old default", route_names=["old"])
    current = Client(
        name="Current policy",
        route_names=["current"],
        source_networks=["192.0.2.10/32"] if source_override else [],
        allow_network_auth=source_override,
    )
    config = Configuration(
        clients=[old_policy, current],
        security=Security(
            anonymous_client_id=old_policy.id if source_override else current.id
        ),
        routes=[Route(name="old"), Route(name="current")],
    )
    caller = observation(old_policy.id, basis)
    engine = available_engine()
    topology = route_map(config, [engine], [caller])
    edges = {edge["route_id"]: edge for edge in topology["caller_routes"]}
    assert all(edge["policy_id"] == current.id for edge in edges.values())
    assert edges[config.routes[0].id]["ready_engines"] == []
    assert "allowlist" in edges[config.routes[0].id]["reason"]
    assert edges[config.routes[1].id]["ready_engines"] == [engine["id"]]
    assert topology["policies"] == [
        {"policy_id": old_policy.id, "observed_callers": []},
        {"policy_id": current.id, "observed_callers": [caller]},
    ]


@pytest.mark.parametrize("change", ["removed", "disabled", "source", "rejected"])
def test_key_observation_cannot_fall_back_to_open_access_when_policy_rejects_it(change):
    policy = Client(name="Saved key policy", route_names=["open", "gated"])
    caller = observation(policy.id, "api_key")
    if change == "disabled":
        policy.enabled = False
    if change == "source":
        policy.source_networks = ["198.51.100.0/24"]
    if change == "rejected":
        caller["authentication_error"] = {"status": 401, "detail": "Key revoked"}
    config = Configuration(
        clients=[] if change == "removed" else [policy],
        routes=[Route(name="open"), Route(name="gated", require_caller_key=True)],
    )
    topology = route_map(config, [available_engine()], [caller])
    assert len(topology["caller_routes"]) == 2
    for edge in topology["caller_routes"]:
        assert edge["caller_id"] == caller["id"]
        assert edge["policy_id"] == (None if change == "removed" else policy.id)
        assert edge["ready_engines"] == []
        assert edge["ready_paths"] == []
        assert {
            "removed": "no longer configured",
            "disabled": "disabled",
            "source": "source address",
            "rejected": "Last request rejected: Key revoked",
        }[change] in edge["reason"]


def test_source_allowlist_is_evaluated_for_each_observation_using_one_key():
    policy = Client(
        name="Scoped key", route_names=["private"], source_networks=["192.0.2.0/24"]
    )
    config = Configuration(
        clients=[policy], routes=[Route(name="private", require_caller_key=True)]
    )
    callers = [
        observation(policy.id, "api_key", id="allowed-source"),
        observation(
            policy.id, "api_key", id="blocked-source", source_address="198.51.100.10"
        ),
    ]
    edges = route_map(config, [available_engine()], callers)["caller_routes"]
    assert edges[0]["ready_engines"]
    assert edges[1]["ready_engines"] == []
    assert "source address" in edges[1]["reason"]


def test_unkeyed_transient_access_has_no_invented_policy_id():
    route = Route(name="open")
    caller = observation("removed-policy", "shared_access")
    topology = route_map(Configuration(routes=[route]), [available_engine()], [caller])
    edge = topology["caller_routes"][0]
    assert edge["caller_id"] == caller["id"]
    assert edge["policy_id"] is None
    assert edge["ready_engines"]
    assert topology["policies"] == []
    assert topology["policy_routes"] == []


@pytest.mark.parametrize("allowed_models", [["primary-*"], ["fallback-model"], ["*"]])
def test_ready_paths_keep_model_permissions_separate_for_each_engine_tier(
    allowed_models,
):
    engine = available_engine()
    engine["models"] = [
        {"id": name, "capabilities": ["text"], "enabled": True}
        for name in ["primary-one", "primary-two", "fallback-model"]
    ]
    policy = Client(name="Model-scoped key", model_patterns=allowed_models)
    route = Route(
        name="auto",
        primary=Selector(engine_ids=[engine["id"]], model_patterns=["primary-*"]),
        fallback=Selector(engine_ids=[engine["id"]], model_patterns=["fallback-*"]),
    )
    config = Configuration(clients=[policy], routes=[route])
    topology = route_map(config, [engine], [observation(policy.id, "api_key")])
    expected_tiers = (
        ["primary"]
        if allowed_models == ["primary-*"]
        else ["fallback"]
        if allowed_models == ["fallback-model"]
        else ["primary", "fallback"]
    )
    for edge in topology["caller_routes"] + topology["policy_routes"]:
        assert edge["ready_engines"] == [engine["id"]]
        assert edge["ready_paths"] == [
            {"engine_id": engine["id"], "tier": tier} for tier in expected_tiers
        ]
    # Both model selections exist on the engine. The observation's allowed
    # primary models must not make its forbidden fallback path eligible.
    assert {edge["tier"] for edge in topology["route_engines"]} == {
        "primary",
        "fallback",
    }
