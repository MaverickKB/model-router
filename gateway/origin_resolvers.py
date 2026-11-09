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
TTL = float(os.environ.get("MODEL_ROUTER_TAG_RESOLVER_TTL", "300"))
TAILSCALE_BIN = os.environ.get("MODEL_ROUTER_TAILSCALE_BIN", "tailscale")


def tailscale_tags(address: str) -> list:
    """Tags of the tailnet node owning *address* via `tailscale whois --json`.
    Returns [] when the CLI is absent or the lookup fails."""
    hit = _CACHE.get(address)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    tags: list = []
    if shutil.which(TAILSCALE_BIN):
        try:
            out = subprocess.run([TAILSCALE_BIN, "whois", "--json", address],
                                 capture_output=True, timeout=2, check=True).stdout
            tags = list((json.loads(out).get("Node") or {}).get("Tags") or [])
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            tags = []
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
