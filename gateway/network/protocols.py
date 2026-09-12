"""Identify serving protocols by their responses, regardless of address or port."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

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
    spec = spec or {}
    paths = spec.get("paths", {})
    paths = paths if isinstance(paths, dict) else {}
    capabilities = task_capabilities(paths)
    info = spec.get("info", {})
    title = str(info.get("title", "")) if isinstance(info, dict) else ""
    # API-declared catalog paths cover reverse proxies with nonstandard prefixes.
    if not catalog or not isinstance(catalog.get("data"), list):
        for path in ["/models"] + [
            p for p in paths if p.endswith("/models") and p != "/v1/models"
        ]:
            doc = await json_document(http, origin + path)
            if doc and isinstance(doc.get("data"), list):
                catalog = doc
                result["base_url"] = origin + path.removesuffix("/models")
                break
    if catalog and isinstance(catalog.get("data"), list):
        relay = is_gateway_catalog(catalog)
        catalog_capabilities = capabilities or ([] if relay else ["text", "streaming"])
        result.update(
            status="model_service",
            protocol="openai",
            base_url=result["base_url"] or origin + "/v1",
            capabilities=catalog_capabilities,
        )
        result["models"] = advertised_models(catalog["data"], catalog_capabilities)
        # Gateway catalogs are visible network surfaces, but not new backing engines.
        if relay:
            result.update(
                status="gateway", detail="Catalog is published by a routing service"
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
    elif any(doc and doc.get("authentication_required") for doc in documents):
        result.update(
            status="authentication_required",
            detail="HTTP access requires credentials; model catalog unverified",
        )
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
        elif result["status"] != "gateway":
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
