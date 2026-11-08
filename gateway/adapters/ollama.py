"""Optional native catalog enrichment for an OpenAI-compatible Ollama endpoint."""

import asyncio

from .http import json_document

SHOW_CONCURRENCY = 4
SHOW_DEADLINE_SECONDS = 4


async def show_details(http, origin, models, headers=None):
    """Ask /api/show for every model at once, four at a time, inside one deadline.

    Returns a model -> document map. A model whose document did not arrive in
    time is simply absent, and the caller reports empty capabilities for it
    instead of inventing engine defaults.
    """
    slots = asyncio.Semaphore(SHOW_CONCURRENCY)
    answers = {}

    async def ask(model):
        async with slots:
            details = await json_document(
                http, origin + "/api/show", {"model": model}, headers=headers
            )
        if isinstance(details, dict):
            answers[model] = details

    tasks = [asyncio.create_task(ask(model)) for model in models]
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), SHOW_DEADLINE_SECONDS
        )
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return answers


def row_capabilities(details):
    """Capabilities the provider declared; an unreadable answer declares none."""
    features = details.get("capabilities")
    if not isinstance(features, list):
        return []
    capabilities = []
    for feature in features:
        if not isinstance(feature, str):
            continue
        capabilities.extend(
            {"completion": ["text", "streaming"], "embedding": ["embeddings"]}.get(
                feature, [feature]
            )
        )
    return capabilities


async def ollama_models(http, origin, rows, headers=None):
    loaded = await json_document(http, origin + "/api/ps", headers=headers) or {}
    residents = loaded.get("models", [])
    resident = {
        row.get("model") or row.get("name")
        for row in (residents if isinstance(residents, list) else [])
        if isinstance(row, dict)
    }
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model") or row.get("name")
        if not isinstance(model, str) or not model:
            continue
        names.append(model)
    details_by_model = await show_details(http, origin, names, headers=headers)
    models = []
    for model in dict.fromkeys(names):
        details = details_by_model.get(model) or {}
        models.append(
            {
                "id": model,
                "loaded": model in resident,
                "capabilities": row_capabilities(details),
            }
        )
    return models


async def read_catalog(http, base_url: str, headers: dict) -> dict:
    origin = base_url.removesuffix("/v1")
    document = await json_document(http, origin + "/api/tags", headers=headers)
    if not document or not isinstance(document.get("models"), list):
        raise ValueError("Endpoint did not return a readable native catalog")
    return {
        "data": await ollama_models(http, origin, document["models"], headers=headers)
    }
