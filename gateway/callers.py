"""Observe actual connections without treating labels or headers as authority."""

import asyncio
import hashlib
import json
import re
import time

from fastapi import Request

from .schema import Client
from .store import Store


def readable(value: str, limit: int = 180) -> str:
    return " ".join(value.split())[:limit]


_IDENTITY_HEADERS = {
    "x-router-caller": "router_caller",
    "x-client-name": "client_name",
    "x-client-version": "client_version",
    "x-openai-client-user-agent": "openai_client_user_agent",
    "x-stainless-lang": "stainless_lang",
    "x-stainless-package-version": "stainless_package_version",
    "x-stainless-runtime": "stainless_runtime",
    "x-stainless-runtime-version": "stainless_runtime_version",
    "x-stainless-os": "stainless_os",
    "x-stainless-arch": "stainless_arch",
}
_CLIENT_TOKEN = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9._-]*)/(?P<version>[A-Za-z0-9._+-]+)"
)
_CLIENT_NAMES = {
    "python": "Python",
    "python-requests": "Python requests",
    "python-urllib": "Python urllib",
    "openai-python": "OpenAI Python",
    "node-fetch": "Node fetch",
    "go-http-client": "Go HTTP client",
    "httpx": "HTTPX",
    "curl": "curl",
    "axios": "Axios",
}


def identity_hints(request: Request) -> dict[str, str]:
    """Keep only bounded, non-secret client metadata useful for identification."""
    return {
        key: readable(request.headers.get(header, ""))
        for header, key in _IDENTITY_HEADERS.items()
        if readable(request.headers.get(header, ""))
    }


def client_profile(software: str, hints: dict[str, str]) -> tuple[str, str, str]:
    """Return a human-readable library, version, and runtime without trusting it."""
    values = [
        hints.get("openai_client_user_agent", ""),
        software,
    ]
    for value in values:
        match = _CLIENT_TOKEN.search(value)
        if not match:
            continue
        raw_name = match.group("name")
        raw_version = match.group("version")
        if raw_name.lower() == "openai" and raw_version.lower() == "python":
            return "OpenAI Python", "", "Python"
        family = _CLIENT_NAMES.get(raw_name.lower(), raw_name)
        runtime = (
            "Python"
            if family.startswith("Python ") or family == "OpenAI Python"
            else ""
        )
        return family, raw_version, runtime
    return "", "", ""


def identity_quality(basis: str, reported_name: str, hints: dict[str, str]) -> str:
    if basis == "api_key":
        return "policy_key"
    if reported_name:
        return "self_reported"
    if any(key.startswith(("client_", "openai_", "stainless_")) for key in hints):
        return "runtime_hints"
    return "transport_only"


async def observe(store: Store, request: Request, policy: Client | None) -> dict:
    source = request.client.host if request.client else "Unknown source"
    source_port = request.client.port if request.client else None
    basis = getattr(request.state, "identity_basis", "operator_test")
    software = readable(request.headers.get("User-Agent", ""))
    hints = identity_hints(request)
    reported_name = readable(
        hints.get("router_caller") or hints.get("client_name", ""), 100
    )
    reported_name_source = (
        "X-Router-Caller"
        if hints.get("router_caller")
        else "X-Client-Name"
        if hints.get("client_name")
        else ""
    )
    client_family, client_version, derived_runtime = client_profile(software, hints)
    if not client_version:
        client_version = hints.get("client_version") or hints.get(
            "stainless_package_version", ""
        )
    client_runtime = hints.get("stainless_runtime") or derived_runtime
    observed_at = time.time()
    if basis == "operator_test":
        name = "Console route test"
    elif basis == "api_key" and policy is not None:
        name = policy.name
    else:
        name = reported_name or "Unidentified caller"
    # Never use a supplied label, User-Agent or forwarding header for access.
    # Separate machines using the same shared key still have distinct sources.
    identifier = hashlib.sha256(
        json.dumps(
            [policy.id if policy else None, source, basis, reported_name, software]
        ).encode()
    ).hexdigest()[:24]
    caller = {
        "id": identifier,
        "policy_id": policy.id if policy else None,
        "name": name,
        "source_address": source,
        "source_port": source_port,
        "software": software,
        "reported_name": reported_name,
        "reported_name_source": reported_name_source,
        "identity_quality": identity_quality(basis, reported_name, hints),
        "client_family": client_family,
        "client_version": client_version,
        "client_runtime": client_runtime,
        "client_os": hints.get("stainless_os", ""),
        "client_arch": hints.get("stainless_arch", ""),
        "identity_hints": hints,
        "identity_basis": basis,
        "first_seen": observed_at,
        "request_count": 1,
        "last_seen": observed_at,
        "last_method": request.method,
        "last_path": request.url.path,
    }
    await asyncio.to_thread(store.observe_caller, caller)
    request.state.caller = caller
    return caller
