"""Map observed connections and saved permission previews as separate edges."""

import ipaddress

from .contracts import EngineView
from .request_policy import resolve_unkeyed_policy
from .routing import decide, matches, selector_reason
from .schema import Client, Configuration, Route


def _observed_policy(
    config: Configuration, caller: dict
) -> tuple[Client | None, bool, str]:
    """Resolve one observation without borrowing another connection's key."""
    basis = caller.get("identity_basis")
    keyed = basis in {"api_key", "operator_test"}
    if keyed:
        policy = next(
            (
                client
                for client in config.clients
                if client.id == caller.get("policy_id")
            ),
            None,
        )
    else:
        policy, _ = resolve_unkeyed_policy(config, caller.get("source_address"))

    rejection = caller.get("authentication_error")
    if rejection:
        return policy, keyed, "Last request rejected: " + rejection["detail"]
    if policy is None:
        return None, keyed, "Observed permission policy is no longer configured"
    # Source limits constrain presented keys too. A console test deliberately
    # evaluates its selected policy under the existing operator-test contract.
    if basis == "api_key" and policy.source_networks:
        try:
            source = ipaddress.ip_address(caller.get("source_address"))
            allowed = any(
                source in ipaddress.ip_network(network, strict=False)
                for network in policy.source_networks
            )
        except (ValueError, TypeError):
            allowed = False
        if not allowed:
            return policy, keyed, "Client key is not allowed from this source address"
    return policy, keyed, ""


def _route_access(
    config: Configuration,
    engines: list[EngineView],
    policy: Client | None,
    route: Route,
    *,
    keyed: bool,
    rejection: str = "",
) -> dict:
    if rejection:
        return {"ready_engines": [], "ready_paths": [], "reason": rejection}
    assert policy is not None
    decision = decide(
        config,
        engines,
        policy,
        {"model": route.name},
        consider_capacity=False,
        caller_key_present=keyed,
    )
    ready = list(dict.fromkeys(c["engine_id"] for c in decision["candidates"]))
    return {
        "ready_engines": ready,
        "ready_paths": [
            {"engine_id": engine_id, "tier": tier}
            for engine_id, tier in dict.fromkeys(
                (candidate["engine_id"], candidate["tier"])
                for candidate in decision["candidates"]
            )
        ],
        "reason": decision.get("error")
        or ("Eligible text path" if ready else "No eligible text destination"),
    }


def route_map(
    config: Configuration, engines: list[EngineView], callers: list[dict]
) -> dict:
    """Keep observation IDs on live paths and policy IDs on key previews.

    Each observed path uses its own authentication evidence and current policy.
    Saved policies can be previewed before traffic exists without inventing a
    connected caller. Source grouping belongs to presentation, never permission.
    """
    observed_by_policy: dict[str, list[dict]] = {
        client.id: [] for client in config.clients
    }
    caller_routes = []
    for caller in callers:
        policy, keyed, rejection = _observed_policy(config, caller)
        policy_id = policy.id if policy and policy.id in observed_by_policy else None
        if policy_id is not None:
            observed_by_policy[policy_id].append(caller)
        for route in config.routes:
            caller_routes.append(
                {
                    "caller_id": caller["id"],
                    "policy_id": policy_id,
                    "route_id": route.id,
                    **_route_access(
                        config, engines, policy, route, keyed=keyed, rejection=rejection
                    ),
                }
            )

    policy_routes = [
        {
            "policy_id": policy.id,
            "route_id": route.id,
            **_route_access(config, engines, policy, route, keyed=True),
        }
        for policy in config.clients
        for route in config.routes
        if matches(route.name, policy.route_names)
    ]
    route_engines = []
    for route in config.routes:
        for tier, selector in [
            ("primary", route.primary),
            ("fallback", route.fallback),
        ]:
            if selector is None:
                continue
            for engine in engines:
                if selector.kind != "any" and engine["kind"] != selector.kind:
                    continue
                if selector.engine_ids and engine["id"] not in selector.engine_ids:
                    continue
                if not set(selector.tags).issubset(engine["tags"]):
                    continue
                models = [
                    model["id"]
                    for model in engine["models"]
                    if "text" in model["capabilities"]
                    and not selector_reason(selector, engine, model)
                    and model.get("enabled", True)
                ]
                # An explicitly pinned engine remains visible while its catalog
                # is empty or its model filter excludes the current catalog.
                if (
                    not models
                    and engine["models"]
                    and engine["id"] not in selector.engine_ids
                ):
                    continue
                route_engines.append(
                    {
                        "route_id": route.id,
                        "engine_id": engine["id"],
                        "tier": tier,
                        "dynamic": not selector.engine_ids,
                        "models": models,
                        "ready": bool(models)
                        and route.enabled
                        and engine["status"] == "available",
                    }
                )
    return {
        "policies": [
            {
                "policy_id": client.id,
                "observed_callers": observed_by_policy[client.id],
            }
            for client in config.clients
        ],
        "caller_routes": caller_routes,
        "policy_routes": policy_routes,
        "route_engines": route_engines,
    }
