from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def _port(parts) -> int:
    return parts.port or (443 if parts.scheme == "https" else 80)


def _endpoint_shapes(engine: dict) -> set[tuple[str, int]]:
    shapes = set()
    for url in [engine.get("base_url"), *(engine.get("aliases") or [])]:
        if not url:
            continue
        parts = urlsplit(url)
        shapes.add((parts.path.rstrip("/") or "/", _port(parts)))
    return shapes


def _hostnames(engine: dict) -> set[str]:
    hosts = set()
    urls = [engine.get("base_url"), *(engine.get("aliases") or [])]
    for url in urls:
        if not url:
            continue
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not host or host == "localhost":
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            hosts.add(host)
    if engine.get("name_source") == "discovered":
        name = str(engine.get("name") or "").lower().rstrip(".")
        if name and name != "localhost":
            try:
                ipaddress.ip_address(name)
            except ValueError:
                hosts.add(name)
    return hosts


def _catalog_fingerprint(engine: dict) -> tuple | None:
    models = engine.get("models") or []
    if not models:
        return None
    return tuple(
        sorted(
            (
                str(model.get("id")),
                tuple(sorted(str(cap) for cap in model.get("capabilities", []))),
                model.get("context_length"),
                model.get("enabled", True),
            )
            for model in models
        )
    )


def suggestions(engines: list[dict]) -> list[dict]:
    """Return reviewable duplicate evidence without changing engine identity."""
    result = []
    for index, left in enumerate(engines):
        for right in engines[index + 1 :]:
            shared_shapes = _endpoint_shapes(left) & _endpoint_shapes(right)
            if not shared_shapes:
                continue
            same_catalog = _catalog_fingerprint(
                left
            ) is not None and _catalog_fingerprint(left) == _catalog_fingerprint(right)
            shared_hosts = _hostnames(left) & _hostnames(right)
            if not same_catalog and not shared_hosts:
                continue
            reasons = ["same API path and port"]
            if same_catalog:
                reasons.append("same discovered catalog")
            if shared_hosts:
                reasons.append("same discovered host name")
            result.append(
                {
                    "source_id": left["id"],
                    "target_id": right["id"],
                    "source_name": left["name"],
                    "target_name": right["name"],
                    "reasons": reasons,
                }
            )
    return result
