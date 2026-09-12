"""Catalog adapters describe metadata independently of the chat transport."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .ollama import read_catalog as read_ollama
from .openai import read_catalog as read_openai


@dataclass(frozen=True)
class CatalogAdapter:
    id: str
    label: str
    read: Callable[..., Awaitable[dict]]


CATALOG_ADAPTERS = {
    adapter.id: adapter
    for adapter in (
        CatalogAdapter("openai", "OpenAI-compatible catalog", read_openai),
        CatalogAdapter("ollama", "Ollama native catalog", read_ollama),
    )
}
