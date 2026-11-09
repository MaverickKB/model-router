"""Per-origin request policy: origin classification, privacy marking, metadata
stripping, route allowlist and a maintenance gate.

Neutral by default: with no policy file there are no origins and nothing changes.
Everything site-specific lives in the policy file (JSON) or environment:

  MODEL_ROUTER_ORIGIN_POLICY     policy file path (default: <state>/origin_policy.json)
  MODEL_ROUTER_MAINTENANCE_FLAG  global gate flag file (default: <state>/maintenance.flag);
                                 its presence gates every configured origin.

See docs/ORIGIN_POLICY.md and examples/origin_policy.example.json.
The file is re-read when its mtime changes; no restart is needed.
"""
from __future__ import annotations

import importlib
import ipaddress
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PRIVATE_HEADER = "X-Router-Origin-Private"
DEFAULT_ORIGIN_HEADER = "X-Router-Origin"
DEFAULT_MESSAGE = "This service is down for maintenance. Please try again later."
# Backward-compatible alias; prefer OriginPolicy.private_header.
PRIVATE_HEADER = DEFAULT_PRIVATE_HEADER


@dataclass
class Origin:
    name: str
    networks: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    private: bool = False
    allowed_routes: list | None = None      # None = no restriction
    allowed_paths: list | None = None       # None = no restriction
    strip_prefixes: list = field(default_factory=list)
    strip_keys: list = field(default_factory=list)
    maintenance: bool = False
    maintenance_flag: str | None = None
    maintenance_message: str = DEFAULT_MESSAGE

    FIELDS = ("private", "allowed_routes", "allowed_paths", "strip_prefixes",
              "strip_keys", "maintenance", "maintenance_flag", "maintenance_message")

    @classmethod
    def parse(cls, raw: dict) -> Origin:
        o = cls(name=str(raw["name"]),
                networks=[ipaddress.ip_network(n, strict=False)
                          for n in raw.get("source_networks", [])],
                tags=list(raw.get("source_tags", [])))
        for k in cls.FIELDS:
            if k in raw:
                setattr(o, k, raw[k])
        return o

    def public(self) -> dict:
        return {"name": self.name, "private": bool(self.private)}

    def route_allowed(self, model: str) -> bool:
        return self.allowed_routes is None or model in self.allowed_routes

    def path_allowed(self, path: str) -> bool:
        if self.allowed_paths is None:
            return True
        return path.rstrip("/") in {p.rstrip("/") for p in self.allowed_paths}

    def _strip_dict(self, d: dict) -> int:
        removed = 0
        for key in list(d):
            low = str(key).lower()
            if any(low.startswith(p) for p in self.strip_prefixes) or any(
                    low == k or low.startswith(k + "_") or low.endswith("_" + k)
                    for k in self.strip_keys):
                d.pop(key)
                removed += 1
        return removed

    def sanitize(self, payload: dict) -> int:
        """Remove configured metadata keys in place; never inspects content."""
        removed = self._strip_dict(payload)
        for container in ("metadata", "extra_body"):
            inner = payload.get(container)
            if isinstance(inner, dict):
                removed += self._strip_dict(inner)
                if container == "extra_body" and isinstance(inner.get("metadata"), dict):
                    removed += self._strip_dict(inner["metadata"])
        return removed


def load_resolver(spec, static_tags=None):
    """Tag resolver: None (off unless "static_tags" is given, then "static"),
    "static", "tailscale" (optional plugin) or "module:callable".
    A resolver is callable(address) -> list[str] of tags."""
    if not spec and static_tags:
        spec = "static"
    if not spec:
        return None
    if spec == "static":
        from .origin_resolvers import StaticCIDRResolver
        return StaticCIDRResolver(static_tags or {})
    if spec == "tailscale":
        from .origin_resolvers import tailscale_tags
        return tailscale_tags
    module, _, attr = str(spec).partition(":")
    return getattr(importlib.import_module(module), attr)


class OriginPolicy:
    def __init__(self, state_dir: str, tag_lookup=None):
        self.path = Path(os.environ.get(
            "MODEL_ROUTER_ORIGIN_POLICY", Path(state_dir) / "origin_policy.json"))
        self.global_flag = Path(os.environ.get(
            "MODEL_ROUTER_MAINTENANCE_FLAG", Path(state_dir) / "maintenance.flag"))
        self._lock = threading.Lock()
        self._mtime = None
        self._injected = tag_lookup
        self.origins: list[Origin] = []
        self.private_header = DEFAULT_PRIVATE_HEADER
        self.origin_header: str | None = None   # off by default
        self.resolver = tag_lookup
        self.error: str | None = None
        self.reload()

    def reload(self):
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self.origins, self._mtime = [], None
                return
            if mtime == self._mtime:
                return
            try:
                raw = json.loads(self.path.read_text())
                self.origins = [Origin.parse(o) for o in raw.get("origins", [])]
                self.private_header = raw.get("private_header", DEFAULT_PRIVATE_HEADER)
                oh = raw.get("origin_name_header", None)
                self.origin_header = DEFAULT_ORIGIN_HEADER if oh is True else (oh or None)
                spec = raw.get("tag_resolver")
                # Legacy env switch, kept for compatibility with older deployments.
                if spec is None and os.environ.get("MODEL_ROUTER_TAILSCALE_TAGS") == "1":
                    spec = "tailscale"
                self.resolver = self._injected or load_resolver(spec, raw.get("static_tags"))
                self.error = None
            except Exception as exc:  # noqa: BLE001 - optional resolver plugins may raise arbitrary errors.
                self.error = f"{type(exc).__name__}: {exc}"
            self._mtime = mtime
            self._cache = {}

    def _tags_for(self, address: str) -> set:
        cache = getattr(self, "_cache", {})
        if address not in cache:
            try:
                cache[address] = set(self.resolver(address) or [])
            except Exception:  # noqa: BLE001 - an optional resolver failure supplies no tags.
                cache[address] = set()
            self._cache = cache
        return cache[address]

    def classify(self, address: str | None) -> Origin | None:
        self.reload()
        if not address or not self.origins:
            return None
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        for o in self.origins:
            if any(ip in n for n in o.networks):
                return o
        if self.resolver is not None and any(o.tags for o in self.origins):
            tags = self._tags_for(address)
            for o in self.origins:
                if tags & set(o.tags):
                    return o
        return None

    def in_maintenance(self, origin: Origin) -> bool:
        if origin.maintenance or self.global_flag.exists():
            return True
        return bool(origin.maintenance_flag and Path(origin.maintenance_flag).exists())

    def upstream_headers(self, origin: Origin | None) -> dict:
        if origin is None or not origin.private:
            return {}
        h = {self.private_header: "1"}
        if self.origin_header:
            h[self.origin_header] = origin.name
        return h

    def is_private_event(self, event: dict) -> bool:
        tag = event.get("origin")
        if isinstance(tag, dict) and tag.get("private"):
            return True
        caller = event.get("caller")
        return isinstance(caller, dict) and self.is_private_caller(caller)

    def is_private_caller(self, caller: dict) -> bool:
        o = self.classify(caller.get("source_address"))
        return bool(o and o.private)
