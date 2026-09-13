"""Observe actual connections without treating labels or headers as authority."""

import asyncio
import time

from fastapi import Request

from .caller_records import caller_id, client_profile
from .schema import Client
from .store import Store


def readable(value: str, limit: int = 180) -> str:
    return " ".join(value.split())[:limit]


_IDENTITY_HEADERS = {
    "x-router-caller": "router_caller",
    "x-router-device-id": "router_device_id",
    "x-router-hostname": "router_hostname",
    "x-client-device-id": "client_device_id",
    "x-client-name": "client_name",
    "x-client-hostname": "client_hostname",
    "x-client-version": "client_version",
    "x-openai-client-user-agent": "openai_client_user_agent",
    "x-stainless-lang": "stainless_lang",
    "x-stainless-package-version": "stainless_package_version",
    "x-stainless-runtime": "stainless_runtime",
    "x-stainless-runtime-version": "stainless_runtime_version",
    "x-stainless-os": "stainless_os",
    "x-stainless-arch": "stainless_arch",
}


def identity_hints(request: Request) -> dict[str, str]:
    """Keep only bounded, non-secret client metadata useful for identification."""
    return {
        key: readable(request.headers.get(header, ""))
        for header, key in _IDENTITY_HEADERS.items()
        if readable(request.headers.get(header, ""))
    }


def identity_quality(basis: str, reported_name: str, hints: dict[str, str]) -> str:
    if basis in {"api_key", "account_key"}:
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
    source_evidence = store.source_identity(source, hints)
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
    account_id = getattr(request.state, "account_id", None)
    key_id = getattr(request.state, "key_id", None)
    device_id = getattr(request.state, "device_id", None)
    principal_label = getattr(request.state, "principal_label", None)
    if basis == "operator_test":
        name = "Console route test"
    elif basis == "api_key" and policy is not None:
        name = policy.name
    elif basis in {"account_key", "registered_device"} and principal_label:
        name = principal_label
    else:
        name = reported_name or "Unidentified caller"
    caller = {
        "policy_id": policy.id if policy else None,
        "account_id": account_id,
        "key_id": key_id,
        "device_id": device_id,
        "name": name,
        "source_address": source,
        "source_port": source_port,
        **source_evidence,
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
    authentication_error = getattr(request.state, "authentication_error", None)
    if authentication_error:
        caller["authentication_error"] = {
            "status": authentication_error["status"],
            "detail": readable(authentication_error["detail"], 300),
        }
    caller["id"] = caller_id(caller)
    await asyncio.to_thread(store.observe_caller, caller)
    request.state.caller = caller
    return caller
