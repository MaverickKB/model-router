"""Catalog adapters describe metadata independently of the chat transport."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .ollama import read_catalog as read_ollama
from .openai import read_catalog as read_openai
from .jevos_native import read_catalog as read_jevos_native
from .systemone import read_catalog as read_systemone
from .xai_tts import read_catalog as read_xai_tts


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
        CatalogAdapter("jevos_native", "Native JevOS decision catalog", read_jevos_native),
        CatalogAdapter("systemone", "System One health catalog", read_systemone),
        CatalogAdapter("xai_tts", "Router-declared xAI speech service", read_xai_tts),
    )
}
