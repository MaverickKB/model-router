"""Optional tag resolvers for origin_policy. None is enabled by default.

Enable one in the policy file, e.g. "tag_resolver": "tailscale", or point at your
own: "tag_resolver": "my_package.my_module:resolve" (callable(address) -> list[str]).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

_CACHE: dict[str, tuple[float, list]] = {}
_LOGIN_CACHE: dict[str, tuple[float, str]] = {}
TTL = float(os.environ.get("MODEL_ROUTER_TAG_RESOLVER_TTL", "300"))
TAILSCALE_BIN = os.environ.get("MODEL_ROUTER_TAILSCALE_BIN", "tailscale")


def _whois(address: str) -> dict:
    """One `tailscale whois --json` call, cached. {} when the CLI is absent."""
    if not shutil.which(TAILSCALE_BIN):
        return {}
    try:
        out = subprocess.run([TAILSCALE_BIN, "whois", "--json", address],
                             capture_output=True, timeout=2, check=True).stdout
        return json.loads(out)
    except Exception:
        return {}


def tailscale_login(address: str) -> str:
    """The tailnet account (email) that owns *address*, or "".

    This is the identity Ken asked to share by: the login, not the machine name
    and not the tag. Tagged devices have no user login, so a tagged node
    returns "" and the caller falls back to its tag or address.
    """
    hit = _LOGIN_CACHE.get(address)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    profile = (_whois(address).get("UserProfile") or {})
    login = str(profile.get("LoginName") or "")
    _LOGIN_CACHE[address] = (time.monotonic(), login)
    return login


def tailscale_tags(address: str) -> list:
    """Tags of the tailnet node owning *address* via `tailscale whois --json`.
    Returns [] when the CLI is absent or the lookup fails."""
    hit = _CACHE.get(address)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    tags = list((_whois(address).get("Node") or {}).get("Tags") or [])
    _CACHE[address] = (time.monotonic(), tags)
    return tags


class TagResolver:
    """Interface: resolve(address) -> list of tag strings. Any callable works."""

    def __call__(self, address: str) -> list:  # pragma: no cover - interface
        raise NotImplementedError


class StaticCIDRResolver(TagResolver):
    """Default resolver: tags from a static mapping {tag: [cidr, ...]} in the policy
    file ("static_tags"). No external tools, works on any OS."""

    def __init__(self, mapping: dict):
        import ipaddress
        self._nets = [(tag, ipaddress.ip_network(c, strict=False))
                      for tag, cidrs in (mapping or {}).items() for c in cidrs]

    def __call__(self, address: str) -> list:
        import ipaddress
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return []
        return sorted({tag for tag, net in self._nets if ip in net})
