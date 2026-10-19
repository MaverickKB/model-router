"""Reviewable duplicate-engine evidence. The address is identity, the name is a claim.

Ken's rule, stated 2026-10-19:

    "Names are unique and IP's are unique. they don't overlap."
    "if the IPs are LAN and Tailscale - then they should be linked by Alias… but if
     the IPs are same subnet, IP wins over Alias and we have a name error"
    "when an alias is used, like 'Spark' but we have 2 of them with unique IPs on the
     same subnet, that's a conflict. 'Spark1' and 'Spark2' does not conflict in Alias.
     So, Alias can't win there. But, when Spark1 + LAN IP and Spark1 + Tailscale IP
     are the same, that's not a name conflict in alias, that's a merge."
    "a Spark1 + Wired LAN and a Spark1 + WiFi are also a merge, not a name conflict.
     Hostname wins."
    "be sure to give a manual override for that, such as a case when 1 machine might
     have multiple IP addresses."

The failure this replaces: the previous version built its key as (API path, port) and
threw the host away. One machine written ``…:8888/v1`` and ``…:8888`` never matched,
so a network scan appended a second record for the same box, and the detector
reported nothing. Two different machines sharing a port and path would have matched.
Both directions were wrong, because the key was a string that happened to be
convenient rather than the identity.

Precedence, strongest evidence first. The first rule that fires decides:

1. Operator pin. A human declaration that these addresses are one machine. This is
   the override for a box with several addresses and no resolving name.
2. Same address and port on both base_urls. One machine listed twice.
3. Same hostname. One box with several interfaces (wired, WiFi, LAN, tailnet).
   Hostname outranks the same-subnet rule for exactly this case.
4. Shared alias across subnets: one box on two transports. Merge.
5. Shared alias, different hostnames, same subnet: two machines cannot share a name.
   The address wins and the alias is wrong.
6. Anything else: clean.

An alias is a claim. A hostname is identity. A name becomes an address only with
resolver evidence; guessing is the bug this replaces.

A display label is neither. ``name`` is what an operator typed, and it may be a
sentence ("Spark 1 (currently 2 spark host)"). Only a field documented as a hostname,
or an alias URL whose host is a name rather than an address, counts as hostname
evidence. Two records sharing a label prove nothing and must not merge.

``resolve`` maps hostname -> address from discovery evidence. ``pins`` maps an address
or a hostname -> machine key; both forms are accepted, because an operator who knows
two addresses are one box may know it by name.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _port(parts) -> int | None:
    """The endpoint's port, or None when the stored URL carries an unusable one."""
    try:
        return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def _is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _resolve(host: str, resolve: dict[str, str]) -> str:
    if not host:
        return ""
    if _is_address(host):
        return str(ipaddress.ip_address(host))
    return resolve.get(host, "")


def _endpoint(url: str, resolve: dict[str, str]) -> tuple[str, int] | None:
    """The machine this URL names, as (address, port), or None if it cannot be known."""
    if not url:
        return None
    try:
        host = _resolve(_url_host(url), resolve)
        port = _port(urlsplit(url))
    except ValueError:
        return None
    if not host or port is None:
        return None
    return (host, port)


def _own(engine: dict, resolve: dict[str, str]) -> tuple[str, int] | None:
    return _endpoint(engine.get("base_url") or "", resolve)


def _alias_endpoints(engine: dict, resolve: dict[str, str]) -> list[tuple[str, int]]:
    out = []
    for entry in engine.get("aliases") or []:
        endpoint = _endpoint(entry or "", resolve)
        if endpoint and endpoint not in out:
            out.append(endpoint)
    return out


def _alias_names(engine: dict) -> set[str]:
    """Alias entries that are bare names, not URLs and not addresses."""
    names = set()
    for entry in engine.get("aliases") or []:
        text = str(entry or "").strip()
        if not text or "://" in text or _is_address(text):
            continue
        names.add(text.casefold())
    return names


def _hostname(engine: dict) -> str:
    """The machine's own name, from evidence that names a machine.

    Three sources, in order, and never the display label:

    1. An explicit ``hostname`` / ``source_hostname`` field.
    2. An alias URL whose host is a name rather than an address. Discovery writes
       exactly this shape when a scan reports a host name for an endpoint.
    3. Nothing. No evidence, no merge on the strength of a name.
    """
    for key in ("hostname", "source_hostname"):
        value = str(engine.get(key) or "").strip().casefold().rstrip(".")
        if value and not _is_address(value):
            return value
    for entry in engine.get("aliases") or []:
        text = str(entry or "").strip()
        if "://" not in text:
            continue
        host = _url_host(text)
        if host and host != "localhost" and not _is_address(host):
            return host.casefold().rstrip(".")
    return ""


def _same_subnet(left: str, right: str) -> bool:
    try:
        a, b = ipaddress.ip_address(left), ipaddress.ip_address(right)
    except ValueError:
        return False
    if a.version != b.version:
        return False
    bits = 24 if a.version == 4 else 64
    return ipaddress.ip_network(f"{a}/{bits}", strict=False) == ipaddress.ip_network(
        f"{b}/{bits}", strict=False
    )


def _space(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "other"
    if ip.version == 4 and ip in TAILNET:
        return "tailnet"
    if ip.version == 4 and ip.is_private:
        return "lan"
    return "other"


def _paths(engine: dict) -> set[str]:
    urls = [u for u in [engine.get("base_url"), *(engine.get("aliases") or [])] if u]
    paths = set()
    for url in urls:
        try:
            paths.add(urlsplit(url).path.rstrip("/") or "/")
        except ValueError:
            continue
    return paths


def _catalog_fingerprint(engine: dict) -> tuple | None:
    models = engine.get("models") or []
    if not models:
        return None
    return tuple(
        sorted(
            (
                str(model.get("id")),
                tuple(sorted(str(cap) for cap in (model.get("capabilities") or []))),
                model.get("context_length"),
                model.get("enabled", True),
            )
            for model in models
        )
    )


def _finding(kind, left, right, reasons, endpoints, action) -> dict:
    return {
        "kind": kind,
        "source_id": left["id"],
        "target_id": right["id"],
        "source_name": left["name"],
        "target_name": right["name"],
        "endpoints": [f"{host}:{port}" for host, port in sorted(set(endpoints))],
        "reasons": reasons,
        "action": action,
    }


def suggestions(
    engines: list[dict],
    resolve: dict[str, str] | None = None,
    pins: dict[str, str] | None = None,
) -> list[dict]:
    """Return reviewable duplicate evidence without changing engine identity."""
    resolve = resolve or {}
    pins = {_resolve(str(key), resolve) or str(key): value
            for key, value in (pins or {}).items()}
    result = []
    for index, left in enumerate(engines):
        for right in engines[index + 1 :]:
            lo, ro = _own(left, resolve), _own(right, resolve)
            if lo is None or ro is None:
                continue
            same_catalog = (
                _catalog_fingerprint(left) is not None
                and _catalog_fingerprint(left) == _catalog_fingerprint(right)
            )
            shared_paths = _paths(left) & _paths(right)
            catalog_note = ["same discovered catalog"] if same_catalog else []
            path_note = (
                ["same API path: " + ", ".join(sorted(shared_paths))]
                if shared_paths
                else []
            )

            if pins.get(lo[0]) and pins.get(lo[0]) == pins.get(ro[0]):
                result.append(
                    _finding(
                        "merge",
                        left,
                        right,
                        [f"operator pinned both addresses to {pins[lo[0]]}"]
                        + catalog_note,
                        [lo, ro],
                        "merge; operator declared one machine",
                    )
                )
                continue

            if lo == ro:
                result.append(
                    _finding(
                        "duplicate",
                        left,
                        right,
                        [f"same host and port: {lo[0]}:{lo[1]}"]
                        + path_note
                        + catalog_note,
                        [lo],
                        "one machine listed twice; merge",
                    )
                )
                continue

            lh, rh = _hostname(left), _hostname(right)
            if lh and lh == rh:
                result.append(
                    _finding(
                        "merge",
                        left,
                        right,
                        [
                            f"both records carry the hostname {lh!r}",
                            f"addresses {lo[0]} and {ro[0]} are interfaces on one machine",
                        ]
                        + path_note
                        + catalog_note,
                        [lo, ro],
                        "merge; one machine, multiple addresses",
                    )
                )
                continue

            shared_alias_names = _alias_names(left) & _alias_names(right)
            cross_alias = (
                [f"{left['name']} aliases {ro[0]}:{ro[1]}"]
                if ro in _alias_endpoints(left, resolve)
                else []
            ) + (
                [f"{right['name']} aliases {lo[0]}:{lo[1]}"]
                if lo in _alias_endpoints(right, resolve)
                else []
            )
            claims = [
                f"both carry the alias {name!r}" for name in sorted(shared_alias_names)
            ] + cross_alias
            if not claims:
                continue

            left_space, right_space = _space(lo[0]), _space(ro[0])

            if {left_space, right_space} == {"lan", "tailnet"}:
                lan = lo[0] if left_space == "lan" else ro[0]
                tail = ro[0] if left_space == "lan" else lo[0]
                result.append(
                    _finding(
                        "merge",
                        left,
                        right,
                        [
                            f"one address is LAN ({lan}), the other is tailnet ({tail})",
                            "different subnets, so this is one machine on two transports",
                        ]
                        + claims
                        + catalog_note,
                        [lo, ro],
                        "merge; one machine, two transports",
                    )
                )
                continue

            if _same_subnet(lo[0], ro[0]):
                result.append(
                    _finding(
                        "name_error",
                        left,
                        right,
                        [
                            f"both addresses sit in the same subnet: {lo[0]}, {ro[0]}",
                            f"the hostnames differ ({lh or 'none'} vs {rh or 'none'}), "
                            "so the alias cannot win",
                        ]
                        + claims
                        + catalog_note,
                        [lo, ro],
                        "rename one, or pin them if they really are one machine",
                    )
                )
                continue
    return result
