from __future__ import annotations

from fnmatch import fnmatchcase

from .contracts import Candidate, Decision, EngineView, ModelView, Rejection
from .schema import Client, Configuration, Route, Selector


def matches(value: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(value, pattern) for pattern in patterns)


def route_requires_caller_key(config: Configuration, route: Route) -> bool:
    """Resolve a route gate without reopening pre-route-gate installations.

    ``model_fields_set`` lets an in-memory legacy Route continue to honor the
    old global setting until it is saved.  Persisted pre-v4 state is
    materialized by migration, while every route edited by the current UI
    carries an explicit value.
    """
    if "require_caller_key" in route.model_fields_set:
        return route.require_caller_key
    return config.security.client_auth_enabled


def requirements(payload: dict) -> set[str]:
    required = {"text"}
    if payload.get("stream"):
        required.add("streaming")
    if payload.get("tools") and payload.get("tool_choice") != "none":
        required.add("tools")
    for message in payload.get("messages", []):
        if (
            isinstance(message, dict)
            and isinstance(message.get("content"), list)
            and any(
                isinstance(part, dict)
                and part.get("type") in {"image_url", "input_image"}
                for part in message["content"]
            )
        ):
            required.add("vision")
    return required


def client_reason(client: Client, engine: EngineView, model: ModelView) -> str:
    if not client.enabled:
        return "Client is disabled"
    if engine["kind"] == "cloud" and not client.allow_cloud:
        return "Cloud access is not allowed for this client"
    if client.engine_ids and engine["id"] not in client.engine_ids:
        return "Engine is outside the client's allowlist"
    if not matches(model["id"], client.model_patterns):
        return "Model is outside the client's allowlist"
    return ""


def selector_reason(selector: Selector, engine: EngineView, model: ModelView) -> str:
    if selector.kind != "any" and engine["kind"] != selector.kind:
        return f"This policy selects {selector.kind} engines"
    if selector.engine_ids and engine["id"] not in selector.engine_ids:
        return "Engine is outside this route's selection"
    if not matches(model["id"], selector.model_patterns):
        return "Model does not match this route"
    if selector.tags and not set(selector.tags).issubset(engine["tags"]):
        return "Engine does not have the required tags"
    return ""


def decide(
    config: Configuration,
    engines: list[EngineView],
    client: Client,
    payload: dict,
    *,
    consider_capacity: bool = True,
    caller_key_present: bool = True,
) -> Decision:
    requested = str(payload.get("model", ""))
    route = next((r for r in config.routes if r.name == requested), None)
    rejected: list[Rejection] = []
    candidates: list[Candidate] = []
    required = requirements(payload)
    permission_blocked = False
    if not client.enabled:
        return {
            "candidates": [],
            "rejections": [],
            "error": "Client is disabled",
            "status": 403,
        }
    if route:
        if not route.enabled:
            return {
                "candidates": [],
                "rejections": [],
                "error": "Route is disabled",
                "status": 503,
            }
        if not matches(route.name, client.route_names):
            return {
                "candidates": [],
                "rejections": [],
                "error": "Route is outside the client's allowlist",
                "status": 403,
            }
        if route_requires_caller_key(config, route) and not caller_key_present:
            return {
                "candidates": [],
                "rejections": [],
                "error": "A caller key is required for this route",
                "status": 401,
            }
        tiers = [("primary", route.primary)] + (
            [("fallback", route.fallback)] if route.fallback else []
        )
    else:
        if not client.allow_direct_models:
            return {
                "candidates": [],
                "rejections": [],
                "error": "This client may use its allowed routes only",
                "status": 403,
            }
        tiers = [("primary", Selector(kind="any", model_patterns=[requested]))]
    for tier, selector in tiers:
        for engine in engines:
            if engine["status"] != "available":
                rejected.append(
                    {
                        "engine_id": engine["id"],
                        "engine": engine["name"],
                        "model": None,
                        "tier": tier,
                        "reason": f"Engine is {engine['status']}",
                    }
                )
                continue
            for model in engine["models"]:
                permission = client_reason(client, engine, model)
                selection = selector_reason(selector, engine, model)
                if permission and not selection and (route or model["id"] == requested):
                    permission_blocked = True
                reason = permission or selection
                if not model.get("enabled", True):
                    reason = "Model is disabled by the operator"
                if not route and model["id"] != requested:
                    reason = "Direct model requests require an exact model ID"
                if not reason and not required.issubset(model["capabilities"]):
                    reason = "Missing capability: " + ", ".join(
                        sorted(required - set(model["capabilities"]))
                    )
                unsupported = set(payload) & set(engine["unsupported_parameters"])
                if not reason and unsupported:
                    reason = (
                        "Engine does not support the client's explicit option: "
                        + ", ".join(sorted(unsupported))
                    )
                if (
                    not reason
                    and consider_capacity
                    and engine["inflight"] >= engine["max_inflight"]
                ):
                    reason = "Engine concurrency limit reached"
                if reason:
                    rejected.append(
                        {
                            "engine_id": engine["id"],
                            "engine": engine["name"],
                            "model": model["id"],
                            "tier": tier,
                            "reason": reason,
                        }
                    )
                else:
                    order = (
                        selector.engine_ids.index(engine["id"])
                        if engine["id"] in selector.engine_ids
                        else 0
                    )
                    candidates.append(
                        {
                            "engine_id": engine["id"],
                            "engine": engine["name"],
                            "model": model["id"],
                            "kind": engine["kind"],
                            "tier": tier,
                            "order": order,
                            "load": engine["inflight"] / engine["max_inflight"],
                            "skipped_defaults": sorted(
                                set(route.defaults if route else {})
                                & set(engine["unsupported_parameters"])
                            ),
                            "reason": f"{tier.capitalize()} policy matched; client permissions and required capabilities passed",
                        }
                    )
    strategy = route.strategy if route else "least_busy"
    candidates.sort(
        key=lambda c: (
            c["tier"] == "fallback",
            c["order"] if strategy == "ordered" else c["load"],
            c["order"],
            c["engine_id"],
            c["model"],
        )
    )
    unique = []
    seen = set()
    for candidate in candidates:
        key = candidate["engine_id"], candidate["model"]
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return {
        "route": route.name if route else requested,
        "client": client.name,
        "required_capabilities": sorted(required),
        "candidates": unique,
        "rejections": rejected,
        "defaults": route.defaults if route else {},
        "revision": config.revision,
        **(
            {"status": 403, "error": "Client permissions exclude the matching models"}
            if not route and not unique and permission_blocked
            else {}
        ),
    }


def apply_defaults(payload: dict, defaults: dict) -> dict:
    result = dict(payload)
    for key, value in defaults.items():
        if (
            key == "chat_template_kwargs"
            and isinstance(value, dict)
            and isinstance(result.get(key, {}), dict)
        ):
            result[key] = {**value, **result.get(key, {})}
        else:
            result.setdefault(key, value)
    return result
