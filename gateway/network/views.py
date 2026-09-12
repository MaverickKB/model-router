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
            engine = by_url.get(service.get("base_url"))
            if service.get("status") != "gateway" and engine:
                service.update(
                    engine_id=engine["id"],
                    engine_name=engine["name"],
                    models=engine["models"],
                    checked_at=engine["checked_at"],
                    catalog_status="unavailable" if engine["error"] else "available",
                    detail=engine["error"],
                    status=service.get("status", "model_service")
                    if engine["error"]
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
            len(s.get("models", [])) for s in services if s.get("status") != "gateway"
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
                ]
            ).lower()
        )
        and (
            category == "All addresses"
            or category == "Responding"
            and h.get("status") == "up"
            or category == "Model services"
            and any(
                s.get("status") in {"model_service", "model_surface", "gateway"}
                for s in h.get("services", [])
            )
        )
    ]
    report["total"] = len(matched)
    report["offset"] = offset
    report["hosts"] = (
        matched[offset : offset + limit] if limit is not None else matched[offset:]
    )
    return report
