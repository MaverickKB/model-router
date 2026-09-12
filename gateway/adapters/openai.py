"""The standard model-list contract used by OpenAI-compatible servers."""

from .http import json_document


async def read_catalog(http, base_url: str, headers: dict) -> dict:
    document = await json_document(http, base_url + "/models", headers=headers)
    if not document or not isinstance(document.get("data"), list):
        raise ValueError("Endpoint did not return a readable model catalog")
    return document
