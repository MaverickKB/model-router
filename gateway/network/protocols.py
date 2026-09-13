"""Identify serving protocols by their responses, regardless of address or port."""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlsplit

import httpx

from ..adapters.http import json_document
from ..adapters.ollama import ollama_models


def task_capabilities(paths: dict) -> list[str]:
    capabilities = []
    for suffix, names in [
        ("/chat/completions", ["text", "streaming"]),
        ("/completions", ["text", "streaming"]),
        ("/embeddings", ["embeddings"]),
        ("/audio/speech", ["speech"]),
        ("/audio/transcriptions", ["transcription"]),
        ("/images/generations", ["image_generation"]),
    ]:
        if any(path.endswith(suffix) for path in paths):
            capabilities.extend(names)
    return list(dict.fromkeys(capabilities))


def is_gateway_catalog(catalog: dict) -> bool:
    """Use an explicit protocol extension, never an owner or model name."""
    declaration = catalog.get("model_serving")
    return (
        isinstance(declaration, dict)
        and declaration.get("version") == 1
        and declaration.get("kind") == "router"
    )


def unavailable_reason(row: dict) -> str:
    provenance = row.get("provenance", {})
    health = provenance.get("health", {}) if isinstance(provenance, dict) else {}
    if isinstance(health, dict) and health.get("status") in {
        "unhealthy",
        "unavailable",
        "offline",
        "failed",
    }:
        return str(health.get("code") or "Backend reported unavailable")
    if row.get("available") is False:
        return str(row.get("availability_reason") or "Backend reported unavailable")
    return ""


def advertised_models(rows: list, capabilities: list[str]) -> list[dict]:
    models = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        reason = unavailable_reason(row)
        stated = row.get("capabilities")
        declared = (
            [name for name, enabled in stated.items() if enabled is True]
            if isinstance(stated, dict)
            else stated
            if isinstance(stated, list)
            else capabilities
        )
        models.append(
            {
                "id": row["id"],
                "capabilities": declared,
                "available": not reason,
                **({"availability_reason": reason} if reason else {}),
            }
        )
    return models


def server_prefixes(origin: str, spec: dict) -> list[str]:
    """Return literal OpenAPI server paths that resolve to this service."""
    origin_url = urlsplit(origin)
    prefixes = []
    servers = spec.get("servers")
    if not isinstance(servers, list):
        return [""]
    for server in servers:
        value = server.get("url") if isinstance(server, dict) else None
        if not isinstance(value, str) or "{" in value or "}" in value:
            continue
        resolved = urlsplit(urljoin(origin.rstrip("/") + "/", value))
        if (
            resolved.scheme != origin_url.scheme
            or resolved.netloc != origin_url.netloc
            or resolved.username
            or resolved.password
            or resolved.query
            or resolved.fragment
        ):
            continue
        prefixes.append(resolved.path.rstrip("/"))
    return list(dict.fromkeys([*prefixes, ""]))


async def inspect_service(http: httpx.AsyncClient, origin: str) -> dict:
    result = {
        "origin": origin,
        "status": "unidentified",
        "protocol": "",
        "base_url": "",
        "models": [],
        "capabilities": [],
        "detail": "",
    }
    documents = await asyncio.gather(
        *(
            json_document(http, origin + path)
            for path in ("/v1/models", "/api/tags", "/openapi.json")
        )
    )
    catalog, ollama, spec = documents
    spec = spec if isinstance(spec, dict) else {}
    paths = spec.get("paths", {})
    paths = paths if isinstance(paths, dict) else {}
    capabilities = task_capabilities(paths)
    info = spec.get("info", {})
    title = str(info.get("title", "")) if isinstance(info, dict) else ""
    catalog_base = "/v1" if catalog and isinstance(catalog.get("data"), list) else ""
    initial_catalog_auth = (
        "/v1" if catalog and catalog.get("authentication_required") else None
    )
    catalog_auth_base = None
    # API-declared catalog paths cover reverse proxies with nonstandard prefixes.
    if not catalog or not isinstance(catalog.get("data"), list):
        catalog_paths = [
            path
            for path in paths
            if isinstance(path, str) and path.endswith("/models")
        ] + ["/models"]
        attempted_paths = {"/v1/models"}
        for prefix in server_prefixes(origin, spec):
            for path in catalog_paths:
                request_path = prefix + path
                if request_path in attempted_paths:
                    continue
                attempted_paths.add(request_path)
                doc = await json_document(http, origin + request_path)
                if doc and isinstance(doc.get("data"), list):
                    catalog = doc
                    catalog_base = request_path.removesuffix("/models")
                    break
                if doc and doc.get("authentication_required") and catalog_auth_base is None:
                    catalog_auth_base = request_path.removesuffix("/models")
            if catalog and isinstance(catalog.get("data"), list):
                break
        if catalog_auth_base is None:
            catalog_auth_base = initial_catalog_auth
    if catalog and isinstance(catalog.get("data"), list):
        relay = is_gateway_catalog(catalog)
        catalog_capabilities = capabilities or ([] if relay else ["text", "streaming"])
        result.update(
            status="model_service",
            protocol="openai",
            base_url=origin + catalog_base,
            capabilities=catalog_capabilities,
        )
        result["models"] = advertised_models(catalog["data"], catalog_capabilities)
        # Gateway catalogs are visible network surfaces, but not new backing engines.
        if relay:
            result.update(
                status="gateway", detail="Catalog is published by a routing service"
            )
    elif catalog_auth_base is not None or any(
        doc and doc.get("authentication_required") for doc in documents
    ):
        result.update(
            status="authentication_required",
            base_url=(origin + catalog_auth_base) if catalog_auth_base is not None else "",
            detail="HTTP access requires credentials; model catalog unverified",
        )
    elif capabilities:
        result.update(
            status="model_surface",
            protocol="openapi",
            capabilities=capabilities,
            detail="Serving operations found; no model catalog was published",
        )
        # Some serving APIs publish model identity in health metadata instead of
        # a /models catalog. Keep that identity visible without inventing an
        # OpenAI-compatible routing endpoint for a different wire protocol.
        for path in ("/health", "/info"):
            if not isinstance(paths.get(path), dict) or "get" not in paths[path]:
                continue
            metadata = await json_document(http, origin + path) or {}
            model = metadata.get("model_id") or metadata.get("model")
            if isinstance(model, str) and model.strip():
                result.update(
                    models=[{"id": model, "capabilities": capabilities}],
                    detail=f"Model identity published by {path}; serving operations declared by OpenAPI",
                )
                break
    elif paths:
        result.update(
            status="http_service",
            detail=title or "HTTP API with no declared model-serving operations",
        )
    elif any(doc is not None for doc in documents):
        result.update(
            status="http_service",
            protocol="http",
            detail="HTTP endpoint found; no model catalog or serving operations identified",
        )
    if ollama and isinstance(ollama.get("models"), list):
        native = await ollama_models(http, origin, ollama["models"])
        if result["status"] == "model_service":
            by_id = {m["id"]: m for m in native}
            for model in result["models"]:
                extra = by_id.get(model["id"], {})
                model.update({k: v for k, v in extra.items() if k != "id"})
            if result["models"] and all(m["id"] in by_id for m in result["models"]):
                result["catalog_adapter"] = "ollama"
            elif result["models"]:
                result.update(
                    catalog_conflict=True,
                    detail="OpenAI and native catalogs disagree; automatic registration is withheld",
                )
        elif result["status"] not in {"gateway", "authentication_required"}:
            result.update(
                status="model_service",
                protocol="ollama",
                base_url=origin + "/v1",
                models=[
                    {**m, "capabilities": m.get("capabilities", [])} for m in native
                ],
            )
        result["capabilities"] = sorted(
            {c for m in result["models"] for c in m["capabilities"]}
        )
    result["name"] = title or urlsplit(origin).hostname
    return result
