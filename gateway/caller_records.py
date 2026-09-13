"""Stable connection records, independent of mutable authentication and policy.

An observation groups a direct source, reported application and software family.
It is connection evidence, never proof of a person or a grant of access.
"""

import hashlib
import ipaddress
import json
import re

from .caller_sources import ensure_source_identity

_CLIENT_TOKEN = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9._-]*)/(?P<version>[A-Za-z0-9._+-]+)"
)
_CLIENT_NAMES = {
    "python-requests": "Python requests",
    "python-urllib": "Python urllib",
    "openai-python": "OpenAI Python",
    "node-fetch": "Node fetch",
    "go-http-client": "Go HTTP client",
    "httpx": "HTTPX",
    "curl": "curl",
    "axios": "Axios",
}


def client_profile(software: str, hints: dict[str, str]) -> tuple[str, str, str]:
    """Return a human-readable library, version, and runtime without trusting it."""
    values = [
        hints.get("openai_client_user_agent", ""),
        software,
    ]
    for value in values:
        for match in _CLIENT_TOKEN.finditer(value):
            raw_name = match.group("name")
            raw_version = match.group("version")
            if (
                raw_name.lower() in {"openai", "asyncopenai"}
                and raw_version.lower() == "python"
            ):
                return "OpenAI Python", "", "Python"
            family = _CLIENT_NAMES.get(raw_name.lower())
            if family is None:
                continue
            runtime = "Python" if family.startswith("Python ") else ""
            return family, raw_version, runtime
    return "", "", ""


def caller_id(record: dict) -> str:
    """Keep connection identity stable across ports, key changes and SDK upgrades."""
    source = record.get("source_key")
    if not source:
        source = record.get("source_address", "Unknown source")
        try:
            source = str(ipaddress.ip_address(source))
        except ValueError:
            pass
    software = record.get("software", "")
    # Retain the complete product string so two applications mentioning the same
    # SDK do not merge. Optional runtime headers never split an existing source.
    software = re.sub(
        r"\b(?:AsyncOpenAI|OpenAI)/Python(?:\s+[0-9][A-Za-z0-9._+-]*)?",
        "OpenAI/Python",
        software,
        flags=re.IGNORECASE,
    )
    software_identity = _CLIENT_TOKEN.sub(
        lambda match: (
            match.group("name")
            if match.group("version")[0].isdigit()
            else match.group(0)
        ),
        software,
    )
    identity = [source, record.get("reported_name", ""), software_identity.casefold()]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def merge_observations(older: dict, newer: dict) -> dict:
    """Sum history while the latest request alone supplies identity and auth facts."""
    older = ensure_source_identity(older)
    newer = ensure_source_identity(newer)
    ordered = sorted((older, newer), key=lambda row: row.get("last_seen", 0))
    latest = dict(ordered[-1])
    latest["first_seen"] = min(
        row.get("first_seen", row.get("last_seen", 0)) for row in ordered
    )
    count = 0
    ports = []
    addresses = []
    for row in ordered:
        try:
            count += max(int(row.get("request_count", 1)), 1)
        except (TypeError, ValueError):
            count += 1
        recent = row.get("recent_source_ports", [])
        recent = list(recent) if isinstance(recent, list) else []
        recent.append(row.get("source_port"))
        for port in recent:
            if isinstance(port, int) and 0 < port <= 65535:
                ports = [previous for previous in ports if previous != port]
                ports.append(port)
        recent_addresses = row.get("recent_source_addresses", [])
        recent_addresses = (
            list(recent_addresses) if isinstance(recent_addresses, list) else []
        )
        recent_addresses.append(row.get("source_address"))
        for address in recent_addresses:
            if isinstance(address, str) and address:
                addresses = [previous for previous in addresses if previous != address]
                addresses.append(address)
    latest["request_count"] = count
    latest["recent_source_ports"] = ports[-8:]
    latest["recent_source_addresses"] = addresses[-8:]
    return latest
