"""Observe actual connections without treating labels or headers as authority."""

import asyncio
import hashlib
import json
import time

from fastapi import Request

from .schema import Client
from .store import Store


def readable(value: str, limit: int = 180) -> str:
    return " ".join(value.split())[:limit]


async def observe(store: Store, request: Request, policy: Client | None) -> dict:
    source = request.client.host if request.client else "Unknown source"
    basis = getattr(request.state, "identity_basis", "operator_test")
    software = readable(request.headers.get("User-Agent", ""))
    reported_name = readable(request.headers.get("X-Router-Caller", ""), 100)
    if basis == "operator_test":
        name = "Console route test"
    elif basis == "api_key" and policy is not None:
        name = policy.name
    else:
        name = reported_name or software.split(" ")[0] or "Unidentified application"
    # Never use a supplied label, User-Agent or forwarding header for access.
    # Separate machines using the same shared key still have distinct sources.
    identifier = hashlib.sha256(
        json.dumps([policy.id if policy else None, source, basis, reported_name, software]).encode()
    ).hexdigest()[:24]
    caller = {
        "id": identifier,
        "policy_id": policy.id if policy else None,
        "name": name,
        "source_address": source,
        "software": software,
        "reported_name": reported_name,
        "identity_basis": basis,
        "last_seen": time.time(),
        "last_path": request.url.path,
    }
    await asyncio.to_thread(store.observe_caller, caller)
    request.state.caller = caller
    return caller
