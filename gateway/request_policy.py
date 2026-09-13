"""Resolve unkeyed routing permissions from saved policy and direct source evidence.

HTTP authentication and topology share this pure selection function. A selected
permission policy does not establish a caller's identity or prove a key exists.
"""

import ipaddress

from .schema import Client, Configuration


def resolve_unkeyed_policy(
    config: Configuration, source: str | None
) -> tuple[Client, str]:
    """Prefer the most specific source override, then the selected default."""
    try:
        address = ipaddress.ip_address(source)
    except (ValueError, TypeError):
        address = None
    network_clients = []
    for client in config.clients:
        if not client.enabled or not client.allow_network_auth:
            continue
        for value in client.source_networks:
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
            if address is not None and address in network:
                network_clients.append((network.prefixlen, client))
    if network_clients:
        network_clients.sort(key=lambda item: item[0], reverse=True)
        return network_clients[0][1], "source_network"

    default = next(
        (
            client
            for client in config.clients
            if client.id == config.security.anonymous_client_id and client.enabled
        ),
        None,
    )
    if (
        default
        and address is not None
        and (
            not default.source_networks
            or any(
                address in ipaddress.ip_network(value, strict=False)
                for value in default.source_networks
            )
        )
    ):
        return default, "shared_access"
    return (
        Client(
            name="Unkeyed connection",
            kind="shared",
            route_names=[route.name for route in config.routes],
            allow_cloud=True,
        ),
        "shared_access",
    )
