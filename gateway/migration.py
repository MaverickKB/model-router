"""Preserve installed access choices when reading a pre-settings configuration."""

from copy import deepcopy


def configuration(raw: dict) -> dict:
    value = deepcopy(raw)
    if value.get("schema_version", 0) < 3:
        value["upgraded_from_schema"] = value.get("schema_version", 0)
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
    value.pop("compatibility", None)
    return value
