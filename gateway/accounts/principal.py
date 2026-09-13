"""Project an account and its level into the Client shape that routing.decide evaluates."""

from __future__ import annotations

from fastapi import Request

from ..schema import AccountLevel, Client, Configuration
from ..store import Store

PERMISSION_FIELDS = (
    "route_names",
    "engine_ids",
    "model_patterns",
    "allow_cloud",
    "allow_direct_models",
)


def level_for(config: Configuration, level_id: str) -> AccountLevel | None:
    return next(
        (level for level in config.account_levels if level.id == level_id), None
    )


def derive_principal(account: dict, level: AccountLevel) -> Client:
    # The derived client is transient: it never enters config.clients and can
    # never carry more than its level grants.
    return Client(
        id=account["id"],
        name=account["name"],
        kind="person",
        enabled=account["status"] == "active",
        allow_network_auth=False,
        source_networks=[],
        **level.model_dump(include=set(PERMISSION_FIELDS)),
    )


def current_principal(
    store: Store, config: Configuration, request: Request
) -> Client | None:
    """Re-resolve an account principal from current state. None means access was removed."""
    if not config.accounts.enabled:
        return None
    account = store.account_snapshot(request.state.account_id)
    if account is None or account["status"] != "active":
        return None
    device_id = getattr(request.state, "device_id", None)
    if device_id is not None:
        device = store.device_snapshot(device_id)
        if (
            not config.accounts.device_registration_enabled
            or device is None
            or not device["enabled"]
        ):
            return None
    level = level_for(config, account["level_id"])
    return None if level is None else derive_principal(account, level)
