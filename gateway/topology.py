"""Build the policy-first picture used by the route map."""

from .contracts import EngineView
from .routing import decide, matches, route_requires_caller_key, selector_reason
from .schema import Client, Configuration


def route_map(
    config: Configuration, engines: list[EngineView], callers: list[dict]
) -> dict:
    """Return policy edges even before a caller has produced traffic.

    Observations describe connections nested under a permission policy. They do
    not create graph nodes and are never used as the policy source of truth.
    """
    observed_by_policy: dict[str, list[dict]] = {
        client.id: [] for client in config.clients
    }
    unassigned = []
    for caller in callers:
        policy_id = caller.get("policy_id")
        if policy_id in observed_by_policy:
            observed_by_policy[policy_id].append(caller)
        else:
            unassigned.append(caller)

    caller_routes, route_engines = [], []
    for client in config.clients:
        for route in config.routes:
            if not matches(route.name, client.route_names):
                continue
            decision = decide(
                config,
                engines,
                client,
                {"model": route.name},
                consider_capacity=False,
            )
            ready_engines = (
                list(dict.fromkeys(c["engine_id"] for c in decision["candidates"]))
                if route.enabled and client.enabled
                else []
            )
            caller_routes.append(
                {
                    "caller_id": client.id,
                    "policy_id": client.id,
                    "route_id": route.id,
                    "ready_engines": ready_engines,
                    "reason": (
                        "Permission policy disabled"
                        if not client.enabled
                        else "Route disabled"
                        if not route.enabled
                        else decision.get("error")
                        or (
                            "Eligible text path"
                            if ready_engines
                            else "No eligible text destination for this policy"
                        )
                    ),
                }
            )
    # An observed connection without a permission policy is still a real
    # caller. Show the route gate it would encounter without inventing a
    # persistent policy or granting it direct model access.
    unkeyed = Client(
        name="Observed unkeyed connection",
        kind="shared",
        route_names=[route.name for route in config.routes],
        allow_cloud=True,
    )
    for caller in unassigned:
        for route in config.routes:
            decision = decide(
                config,
                engines,
                unkeyed,
                {"model": route.name},
                consider_capacity=False,
                caller_key_present=False,
            )
            ready_engines = list(
                dict.fromkeys(c["engine_id"] for c in decision["candidates"])
            )
            caller_routes.append(
                {
                    "caller_id": caller["id"],
                    "policy_id": None,
                    "route_id": route.id,
                    "ready_engines": ready_engines,
                    "reason": (
                        "A caller key is required for this route"
                        if route_requires_caller_key(config, route)
                        else decision.get("error")
                        or (
                            "Eligible text path"
                            if ready_engines
                            else "No eligible text destination"
                        )
                    ),
                }
            )
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
        "unassigned_callers": unassigned,
        "caller_routes": caller_routes,
        "route_engines": route_engines,
    }
