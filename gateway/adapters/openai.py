"""The standard model-list contract used by OpenAI-compatible servers."""

from .http import json_document


async def read_catalog(http, base_url: str, headers: dict) -> dict:
    document = await json_document(http, base_url + "/models", headers=headers)
    if not document or not isinstance(document.get("data"), list):
        raise ValueError("Endpoint did not return a readable model catalog")

    # OpenAI's catalog has no residency field. Some compatible serving endpoints
    # expose their active model from a root /health document. Require the paired
    # model_ids field as well, so a generic health response's configured "model"
    # is not mistaken for proof of residency.
    origin = base_url.removesuffix("/v1")
    health = await json_document(http, origin + "/health", headers=headers)
    resident = health.get("model") if health else None
    model_ids = health.get("model_ids") if health else None
    if (
        isinstance(resident, str)
        and resident
        and isinstance(model_ids, list)
        and resident in model_ids
    ):
        advertised_ids = {
            row.get("id")
            for row in document["data"]
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        if resident in advertised_ids:
            document = dict(document)
            document["data"] = [
                {**row, "loaded": row["id"] == resident}
                if isinstance(row, dict) and isinstance(row.get("id"), str)
                else row
                for row in document["data"]
            ]
    return document
