"""Optional native catalog enrichment for an OpenAI-compatible Ollama endpoint."""

from .http import json_document


async def ollama_models(http, origin, rows, headers=None):
    loaded = await json_document(http, origin + "/api/ps", headers=headers) or {}
    residents = loaded.get("models", [])
    resident = {
        row.get("model") or row.get("name")
        for row in (residents if isinstance(residents, list) else [])
        if isinstance(row, dict)
    }
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model") or row.get("name")
        if not isinstance(model, str) or not model:
            continue
        details = (
            await json_document(
                http, origin + "/api/show", {"model": model}, headers=headers
            )
            or {}
        )
        capabilities = []
        features = details.get("capabilities", [])
        for feature in features if isinstance(features, list) else []:
            if not isinstance(feature, str):
                continue
            capabilities.extend(
                {"completion": ["text", "streaming"], "embedding": ["embeddings"]}.get(
                    feature, [feature]
                )
            )
        models.append(
            {
                "id": model,
                "loaded": model in resident,
                **(
                    {"capabilities": capabilities}
                    if isinstance(details.get("capabilities"), list)
                    else {}
                ),
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
