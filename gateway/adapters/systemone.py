"""Health catalog for typed decision engines (System One)."""

from .http import json_document


async def read_catalog(http, base_url: str, headers: dict) -> dict:
    document = await json_document(http, base_url + "/health", headers=headers)
    if not document:
        raise ValueError("Endpoint did not return a readable health document")
    if document.get("authentication_required"):
        raise ValueError("Endpoint requires authentication")
    if document.get("http_response"):
        raise ValueError("Endpoint did not return a readable health document")
    status = document.get("status")
    if status not in {"ready", "ok"}:
        raise ValueError("System One endpoint is not ready")
    model_id = document.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = "systemone"
    else:
        model_id = model_id.strip()
    context_length = document.get("max_path_tokens")
    if type(context_length) is not int or context_length <= 0:
        context_length = None
    return {
        "data": [
            {
                "id": model_id,
                "capabilities": ["decision"],
                "context_length": context_length,
            }
        ]
    }
