"""Resolve a source address to a human name from local, credential-free evidence.

The router sits on a host that already knows its own fleet. Three local sources
answer "who is this address", and none of them asks the caller to volunteer
anything:

 1. the tailnet node table   -- ``tailscale status --json``, one call for all peers
 2. reverse DNS              -- ``getent hosts``, one call per address
 3. mDNS                     -- ``avahi-resolve-address``, one call per address
 4. the router's engine table -- an address the operator already named here

Precedence is machine identity first: a hostname from the tailnet or mDNS names
the machine, while an engine name names a role and could mislead on a caller row
that is not the engine itself. An operator-set source label still outranks all
three, and a caller's own reported hostname outranks a resolved one; those
rules already live in ``store._apply_caller_source_name``.

Reverse DNS is asked before mDNS because the router's own hostname lives there
and nowhere else: ``192.0.2.75`` resolves to ``router-host`` through NSS while
avahi answers nothing for it, which left the router unable to name itself. DNS
is only consulted for private and CGNAT addresses, because a public address
reverse-resolves to an ISP's generic pointer, and that is worse than a blank.

A resolved name is presentation evidence only. It never grants access and never
becomes a source key. Resolution runs on a background refresh and answers from a
cache, so the request path never waits on a lookup.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .caller_sources import normalized_address, normalized_hostname

TTL = float(os.environ.get("MODEL_ROUTER_SOURCE_NAME_TTL", "120"))
TAILSCALE_BIN = os.environ.get("MODEL_ROUTER_TAILSCALE_BIN", "tailscale")
AVAHI_BIN = os.environ.get("MODEL_ROUTER_AVAHI_BIN", "avahi-resolve-address")
GETENT_BIN = os.environ.get("MODEL_ROUTER_GETENT_BIN", "getent")
TIMEOUT = 2.0
LOOPBACK = {"127.0.0.1", "::1"}


def run(argv, timeout: float = TIMEOUT) -> bytes:
    """Run one local lookup. Any failure is an empty answer, never an exception."""
    try:
        return subprocess.run(
            argv, capture_output=True, timeout=timeout, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def tailnet_names(runner=run) -> dict[str, str]:
    """IPv4 address -> node name for every peer on this tailnet, in one call.

    The node's DNS name is preferred because it is a hostname by construction and
    survives validation; a display name like "Kenneth's MacBook Pro" is not one.
    """
    output = runner([TAILSCALE_BIN, "status", "--json"])
    if not output:
        return {}
    try:
        document = json.loads(output)
    except ValueError:
        return {}
    names: dict[str, str] = {}

    def add(node):
        if not isinstance(node, dict):
            return
        dns = str(node.get("DNSName") or "").rstrip(".")
        label = normalized_hostname(dns.split(".")[0] if dns else "")
        if not label:
            label = normalized_hostname(str(node.get("HostName") or ""))
        if not label:
            return
        for address in node.get("TailscaleIPs") or []:
            if ":" in str(address):
                continue
            names[normalized_address(str(address))] = label

    add(document.get("Self") or {})
    for peer in (document.get("Peer") or {}).values():
        add(peer)
    return names


def dns_name(address: str, runner=run) -> str:
    """Address -> hostname via reverse DNS (NSS). '' when nothing authoritative answers."""
    try:
        output = runner([GETENT_BIN, "hosts", address]).decode("utf-8", "replace")
    except Exception:
        return ""
    for line in output.splitlines():
        parts = line.split()
        # getent prints "<address> <name> [aliases...]"
        if len(parts) >= 2 and parts[0] == address:
            name = normalized_hostname(parts[1])
            # A pointer back at the address itself carries no name.
            if name and name != address:
                return name
    return ""


def mdns_name(address: str, runner=run) -> str:
    """Address -> mDNS hostname via avahi. Answers only for hosts that announce."""
    output = runner([AVAHI_BIN, address])
    if not output:
        return ""
    fields = output.decode("utf-8", "replace").split()
    if len(fields) < 2:
        return ""
    return normalized_hostname(fields[1].rstrip("."))


def engine_names(engines) -> dict[str, str]:
    """IPv4 address -> engine name, from the operator's own engine table."""
    names: dict[str, str] = {}
    for engine in engines or []:
        try:
            host = urlsplit(str(engine.get("base_url") or "")).hostname
        except ValueError:
            continue
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue  # A DNS name needs no resolution; the engine already holds it.
        label = str(engine.get("name") or "").strip()
        if label:
            names[normalized_address(host)] = label
    return names


def private_v4(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


class SourceNames:
    """Cached address -> name map. Reads never perform a lookup."""

    def __init__(self, ttl: float = TTL, runner=run):
        self.ttl = ttl
        self.runner = runner
        self._lock = threading.RLock()
        self._resolved: dict[str, tuple[float, str]] = {}
        self._tailnet: dict[str, str] = {}
        self._tailnet_at = 0.0
        self._engines: dict[str, str] = {}

    def set_engines(self, engines):
        with self._lock:
            self._engines = engine_names(engines)

    def names(self) -> dict[str, str]:
        """Every resolved name still inside its TTL."""
        now = time.monotonic()
        with self._lock:
            return {
                address: name
                for address, (at, name) in self._resolved.items()
                if name and now - at < self.ttl
            }

    def name(self, address: str) -> str:
        """Cached answer only. Unknown addresses resolve to an empty string."""
        address = normalized_address(str(address))
        now = time.monotonic()
        with self._lock:
            hit = self._resolved.get(address)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        return ""

    def _tailnet_map(self, now: float) -> dict[str, str]:
        if self._tailnet and now - self._tailnet_at < self.ttl:
            return self._tailnet
        fresh = tailnet_names(self.runner)
        with self._lock:
            self._tailnet = fresh
            self._tailnet_at = now
        return fresh

    def refresh(self, addresses) -> dict[str, str]:
        """Resolve a bounded set of addresses. Call this off the request path."""
        now = time.monotonic()
        tailnet = self._tailnet_map(now)
        with self._lock:
            engines = dict(self._engines)
        for raw in addresses:
            address = normalized_address(str(raw))
            if not address or address in LOOPBACK:
                continue
            name = tailnet.get(address, "")
            if not name and private_v4(address):
                name = dns_name(address, self.runner)
            if not name and private_v4(address):
                name = mdns_name(address, self.runner)
            if not name:
                name = engines.get(address, "")
            with self._lock:
                self._resolved[address] = (now, name)
        return self.names()
