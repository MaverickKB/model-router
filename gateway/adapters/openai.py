"""The standard model-list contract used by OpenAI-compatible servers."""

from ..network.catalogs import normalized_openai_catalog
from .http import json_document


async def read_catalog(http, base_url: str, headers: dict) -> dict:
    document = await json_document(http, base_url + "/models", headers=headers)
    catalog = normalized_openai_catalog(document)
    if catalog is None:
        raise ValueError("Endpoint did not return a readable model catalog")
    return catalog
