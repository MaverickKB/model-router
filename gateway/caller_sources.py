"""Caller source identity evidence and operator labels.

HTTP can prove the peer address and bounded request headers. A saved network
discovery snapshot can also associate a directly reachable address with a
hardware address. That evidence is stronger than a caller-reported device
identifier, but neither is an access grant. Hostnames are presentation evidence
only and never affect source identity or access decisions.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re

_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,251}[A-Za-z0-9]$")
_HARDWARE_ADDRESS = re.compile(
    r"^(?:(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4})$"
)


def normalized_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.strip() or "Unknown source"


def normalized_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".").casefold()
    if not hostname or len(hostname) > 253 or not _HOSTNAME.match(hostname):
        return ""
    return hostname


def normalized_hardware_address(value: str) -> str:
    """Normalize a MAC address from recorded network discovery evidence."""
    candidate = value.strip()
    if not _HARDWARE_ADDRESS.fullmatch(candidate):
        return ""
    compact = re.sub(r"[:-]|\.", "", candidate)
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).upper()


def source_identity(
    source_address: str, hints: dict[str, str], *, hardware_address: str = ""
) -> dict[str, str]:
    address = normalized_address(source_address)
    hardware_address = normalized_hardware_address(hardware_address)
    device_id = (
        hints.get("router_device_id") or hints.get("client_device_id") or ""
    ).strip()
    hostname = normalized_hostname(
        hints.get("router_hostname") or hints.get("client_hostname") or ""
    )
    if hardware_address:
        digest = hashlib.sha256(hardware_address.encode()).hexdigest()[:24]
        return {
            "source_key": f"hardware:{digest}",
            "source_label": hostname or address,
            "source_label_source": "reported_hostname" if hostname else "address",
            "source_hostname": hostname,
            "source_identity_quality": "network_hardware",
        }
    if device_id:
        digest = hashlib.sha256(device_id.encode()).hexdigest()[:24]
        return {
            "source_key": f"device:{digest}",
            "source_label": hostname or address,
            "source_label_source": "reported_hostname" if hostname else "address",
            "source_hostname": hostname,
            "source_identity_quality": "reported_device",
        }
    if hostname:
        return {
            "source_key": f"addr:{address}",
            "source_label": hostname,
            "source_label_source": "reported_hostname",
            "source_hostname": hostname,
            "source_identity_quality": "address",
        }
    return {
        "source_key": f"addr:{address}",
        "source_label": address,
        "source_label_source": "address",
        "source_hostname": "",
        "source_identity_quality": "address",
    }


def ensure_source_identity(record: dict) -> dict:
    if record.get("source_key"):
        return record
    identity = source_identity(
        str(record.get("source_address", "Unknown source")),
        record.get("identity_hints", {})
        if isinstance(record.get("identity_hints"), dict)
        else {},
    )
    return {**record, **identity}
