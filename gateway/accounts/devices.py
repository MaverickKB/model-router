"""Pre-registered device addresses (dev mode): validation, overlap with source policies, warnings."""

from __future__ import annotations

import ipaddress

from ..schema import Configuration

ADDRESS_RULE = "Enter one IPv4 or IPv6 address"


def canonical_address(value: str) -> str:
    """One host literal. CIDRs and addresses that cannot name a peer are refused."""
    try:
        parsed = ipaddress.ip_address(str(value).strip())
    except ValueError:
        raise ValueError(ADDRESS_RULE)
    # A zoned literal (fe80::1%eth0) can never equal a reported peer address and
    # would let one host be registered under several distinct strings.
    if getattr(parsed, "scope_id", None) is not None:
        raise ValueError(ADDRESS_RULE)
    # An IPv4-mapped IPv6 literal names the same host as its IPv4 form.
    parsed = getattr(parsed, "ipv4_mapped", None) or parsed
    # Loopback is a real peer (every process on the router host); it is checked
    # first because ::1 also falls inside the IPv6 reserved block.
    if not parsed.is_loopback and (
        parsed.is_unspecified
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_link_local
    ):
        raise ValueError(ADDRESS_RULE)
    return str(parsed)


def shadowing(config: Configuration, address: str) -> list[dict]:
    """Enabled source policies whose networks contain this address; the device outranks them."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return []
    overlaps = []
    for client in config.clients:
        if not client.enabled or not client.allow_network_auth:
            continue
        for value in client.source_networks:
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
            if parsed in network:
                overlaps.append(
                    {"client_id": client.id, "client_name": client.name, "network": value}
                )
    return overlaps


def warnings(config: Configuration, devices: list[dict], accounts: list[dict]) -> list[str]:
    """One line per device that overrides a source policy, only while the path is live."""
    settings = config.accounts
    if not (settings.enabled and settings.device_registration_enabled):
        return []
    names = {
        account["id"]: account["name"]
        for account in accounts
        if account["status"] == "active"
    }
    lines = []
    for device in devices:
        if not device["enabled"] or device["account_id"] not in names:
            continue
        for overlap in shadowing(config, device["address"]):
            lines.append(
                f"Registered device {device['name']!r} ({names[device['account_id']]}) "
                f"takes precedence over source policy {overlap['client_name']!r} "
                f"({overlap['network']}) for {device['address']}"
            )
    return lines
