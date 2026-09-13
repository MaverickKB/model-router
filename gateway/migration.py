"""Preserve installed access choices when reading a pre-settings configuration."""

from copy import deepcopy


def configuration(raw: dict) -> dict:
    value = deepcopy(raw)
    version = int(value.get("schema_version", 0) or 0)
    if version < 3:
        value["upgraded_from_schema"] = version
        value["schema_version"] = 3
        value.setdefault(
            "security", {"operator_auth_enabled": False, "client_auth_enabled": False}
        )
        for client in value.get("clients", []):
            client.setdefault("allow_network_auth", bool(client.get("source_networks")))
        discovery = value.get("discovery", {})
        discovery.pop("ports", None)
        discovery.setdefault("scanner", "nmap")
        discovery.setdefault("inspect_all_open_ports", True)
        discovery.setdefault("include_loopback", True)
        version = 3
    if version < 4:
        # Before route-level gates existed, client_auth_enabled applied to
        # every advertised route.  Materialize that old behavior on each
        # route so an upgrade cannot silently open a protected installation.
        security = value.get("security") or {}
        legacy_required = bool(security.get("client_auth_enabled", True))
        for route in value.get("routes", []):
            route.setdefault("require_caller_key", legacy_required)
        value["upgraded_from_schema"] = value.get("upgraded_from_schema", version)
        value["schema_version"] = 4
    if version < 5:
        # User accounts arrive disabled with no levels, so an upgraded
        # installation behaves exactly as before until an operator opts in.
        value.setdefault(
            "accounts",
            {
                "enabled": False,
                "device_registration_enabled": False,
                "session_hours": 168,
            },
        )
        value.setdefault("account_levels", [])
        value["upgraded_from_schema"] = value.get("upgraded_from_schema", version)
        value["schema_version"] = 5
    value.pop("compatibility", None)
    return value
