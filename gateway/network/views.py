"""Bounded operator views; live authenticated catalogs own registered model state."""

from pathlib import Path

from .report import read_report


def network_view(
    root: Path,
    engines: list[dict],
    *,
    offset=0,
    limit=None,
    query="",
    category="All addresses",
) -> dict:
    report = read_report(root)
    by_url = {
        url: engine
        for engine in engines
        for url in [engine["base_url"], *engine.get("aliases", [])]
    }
    hosts = report.get("hosts", [])
    for host in hosts:
        for service in host.get("services", []):
            engine = by_url.get(service.get("base_url")) or by_url.get(
                service.get("compatible_base_url")
            )
            if service.get("status") != "gateway" and engine:
                service.update(
                    engine_id=engine["id"],
                    engine_name=engine["name"],
                )
                # A registered engine can preserve a previously authenticated
                # catalog, but it cannot turn a catalog-less completion surface
                # into a verified discovery result.
                if service.get("status", "model_service") in {
                    "model_service",
                    "authentication_required",
                }:
                    error = engine.get("error", "")
                    service.update(
                        models=engine.get("models", []),
                        checked_at=engine.get("checked_at", service.get("checked_at", 0)),
                        catalog_status="unavailable" if error else "available",
                        detail=error,
                        status=service.get("status", "model_service")
                        if error
                        else "model_service",
                    )
    services = [s for h in hosts for s in h.get("services", [])]
    report["counts"] = {
        "addresses": len(hosts),
        "responding": sum(
            h.get("status") == "up" and h.get("scope") == "network" for h in hosts
        ),
        "covered": sum(
            h.get("status") == "up"
            and h.get("scope") == "network"
            and h.get("scan_complete", False)
            for h in hosts
        ),
        "services": len(services),
        "models": sum(
            len(s.get("models", []))
            for s in services
            if s.get("status") == "model_service"
        ),
        "verified_catalogs": sum(
            s.get("status") in {"model_service", "gateway"} for s in services
        ),
        "completion_endpoints": sum(
            s.get("status") == "model_surface"
            or (
                s.get("status") == "authentication_required"
                and bool(s.get("compatible_base_url"))
            )
            for s in services
        ),
    }
    query = query.lower()
    matched = [
        h
        for h in hosts
        if (
            not query
            or query
            in " ".join(
                [
                    h.get("name", ""),
                    h.get("address", ""),
                    *[
                        m["id"]
                        for s in h.get("services", [])
                        for m in s.get("models", [])
                    ],
                    *[
                        str(service.get(field, ""))
                        for service in h.get("services", [])
                        for field in (
                            "origin",
                            "surface_id",
                            "observed_base_url",
                            "base_url",
                            "compatible_base_url",
                        )
                    ],
                ]
            ).lower()
        )
        and _matches_category(h, category)
    ]
    report["total"] = len(matched)
    report["offset"] = offset
    report["hosts"] = (
        matched[offset : offset + limit] if limit is not None else matched[offset:]
    )
    return report


def _matches_category(host: dict, category: str) -> bool:
    """Filter service groups without changing the legacy API category."""
    if category == "All addresses":
        return True
    if category == "Responding":
        return host.get("status") == "up"
    statuses = {service.get("status") for service in host.get("services", [])}
    if category == "Verified catalogs":
        return bool(statuses & {"model_service", "gateway"})
    if category == "Completion endpoints":
        return any(
            service.get("status") == "model_surface"
            or (
                service.get("status") == "authentication_required"
                and bool(service.get("compatible_base_url"))
            )
            for service in host.get("services", [])
        )
    if category == "Model services":
        return bool(statuses & {"model_service", "model_surface", "gateway"})
    return False
