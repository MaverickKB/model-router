"""Who a request's tokens belong to.

Ken, 2026-10-19: "I need token counts, i want a small lab version of hosting
capabilities here. we map via origin point or API key. I can't block all free
paths now without having to retool dr Moore, but still need to track by
account/origin. We know his, he's on my tailscale and is exempt. I'm exempt."

Counting is always on. Enforcement is unchanged: it still lives in account
levels. This module answers only "whose tokens were these", in a stable order,
so a report can exist before any limit does.

Identity order, strongest first:

  account:<id>   an account key was presented and verified
  key:<id>       a client key was presented and verified
  origin:<name>  the TCP peer matched a configured origin (tailnet login or tag)
  addr:<address> nothing else resolved; the address is the identity

A label is never identity. An address is identity of last resort, and it is
still better than a self-reported name.
"""

from __future__ import annotations

PRINCIPAL_ORDER = ("account", "key", "origin", "address")


def resolve(request) -> dict:
    """Return the principal for this request, with the evidence that produced it."""
    account_id = getattr(request.state, "account_id", None)
    key_id = getattr(request.state, "key_id", None)
    origin = getattr(request.state, "origin", None)
    source = request.client.host if request.client else "Unknown source"
    caller = getattr(request.state, "caller", None) or {}
    # A client key proves a policy, not a key row: identity.py records the
    # client, not a key id, for that branch. The client is the identity then.
    basis = getattr(request.state, "identity_basis", "")
    client_id = caller.get("policy_id") or ""
    if not key_id and basis == "api_key" and client_id:
        key_id = f"client:{client_id}"

    if account_id:
        return {
            "principal": f"account:{account_id}",
            "kind": "account",
            "account_id": account_id,
            "key_id": key_id,
            "origin_name": origin.name if origin is not None else "",
            "source_address": source,
            "login": caller.get("source_login", ""),
            "label": caller.get("name", "") or "account",
        }
    if key_id:
        return {
            "principal": f"key:{key_id}",
            "kind": "key",
            "account_id": "",
            "key_id": key_id,
            "origin_name": origin.name if origin is not None else "",
            "source_address": source,
            "login": caller.get("source_login", ""),
            "label": caller.get("name", "") or "key",
        }
    if origin is not None:
        return {
            "principal": f"origin:{origin.name}",
            "kind": "origin",
            "account_id": "",
            "key_id": "",
            "origin_name": origin.name,
            "source_address": source,
            "login": caller.get("source_login", ""),
            "label": origin.name,
        }
    return {
        "principal": f"addr:{source}",
        "kind": "address",
        "account_id": "",
        "key_id": "",
        "origin_name": "",
        "source_address": source,
        "login": caller.get("source_login", ""),
        "label": source,
    }


def row(principal: dict, window_start: float, window_seconds: int, usage) -> tuple:
    """The tuple store.record_principal_usage() expects."""
    return (
        principal["principal"],
        principal["kind"],
        principal["label"],
        principal["login"],
        window_start,
        window_seconds,
        *usage.row(),
    )
