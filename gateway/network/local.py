"""Local socket reachability complements the network sweep without hardware assumptions."""

import asyncio
import ipaddress
import socket

try:
    import psutil
except ImportError:
    psutil = None


def is_local_address(value: str) -> bool:
    """Select a route without sending a datagram; compare its source to the target."""
    try:
        address = ipaddress.ip_address(value)
        if address.is_loopback:
            return True
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as route:
            route.connect((value, 9))
            return ipaddress.ip_address(route.getsockname()[0]) == address
    except (ValueError, OSError):
        return False


async def local_listeners() -> dict[str, list[int]]:
    """Read local TCP listeners through the optional portable inventory adapter."""
    if psutil is None:
        raise RuntimeError(
            "Local listener inventory requires the optional discovery package"
        )
    try:
        connections = await asyncio.to_thread(psutil.net_connections, kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        raise RuntimeError(
            "The operating system denied local listener inventory; grant the collector inventory access or connect a known endpoint"
        ) from exc
    listeners: dict[str, set[int]] = {}
    for connection in connections:
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and ipaddress.ip_address(connection.laddr.ip).is_loopback
        ):
            listeners.setdefault(connection.laddr.ip, set()).add(connection.laddr.port)
    return {host: sorted(ports) for host, ports in listeners.items()}
