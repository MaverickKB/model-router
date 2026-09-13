"""Bounded catalog probes and structural model inventory normalization."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

# These are documented protocol dialect metadata endpoints. They are bounded
# paths, never guessed from hostnames, ports, models, or HTTP response text.
DIALECT_CATALOG_PATHS = (
    "/v1/models",
    "/models",
    "/api/v1/models",
    "/api/tags",
)


def model_id(value: object) -> str | None:
    """Return a transport-safe model identifier, never an inferred label."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 1024:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _relative_path(path: str) -> str:
    """Keep probes same-origin and expose only safe, relative evidence."""
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Catalog probes require a relative path")
    normalized = parsed.path
    if normalized == "/":
        return normalized
    segments = normalized[1:].split("/")
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if not segments or any(
        not segment
        or (decoded := unquote(segment)) in {"", ".", ".."}
        or "/" in decoded
        or "\\" in decoded
        for segment in segments
    ):
        raise ValueError("Catalog probes require a canonical relative path")
    return normalized


@dataclass(frozen=True)
class Document:
    """One bounded, credential-free HTTP metadata probe."""

    path: str
    status: int | None
    value: dict | None

    @property
    def authentication_required(self) -> bool:
        return self.status in {401, 403}

    @property
    def responded(self) -> bool:
        return self.status is not None

    def attempt(self) -> dict:
        """Return only operator-safe evidence for the network report."""
        return {
            "path": self.path,
            "status": self.status if self.status is not None else "unreachable",
        }


async def fetch_document(http: httpx.AsyncClient, origin: str, path: str) -> Document:
    """Read a small JSON object without following redirects or leaking errors."""
    attempted_path = path if isinstance(path, str) else ""
    try:
        path = _relative_path(path)
        async with (
            asyncio.timeout(4),
            http.stream("GET", origin.rstrip("/") + path, timeout=2) as response,
        ):
            status = response.status_code
            if status != 200:
                return Document(path, status, None)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_DOCUMENT_BYTES:
                    return Document(path, status, None)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
        return Document(attempted_path, None, None)
    try:
        value = json.loads(raw)
    except ValueError:
        return Document(path, status, None)
    return Document(path, status, value if isinstance(value, dict) else None)


def catalog_base_path(path: str) -> str | None:
    """Derive the exact API base that a declared catalog path proves."""
    try:
        normalized = _relative_path(path).rstrip("/") or "/"
    except ValueError:
        return None
    if not normalized.endswith("/models"):
        return None
    base = normalized.removesuffix("/models").rstrip("/")
    if base in {"", "/"}:
        return ""
    if "{" in base or "}" in base:
        return None
    return base


def compatible_url(origin: str, base_path: str | None) -> str:
    """Join an origin to only a proven, non-root API base path."""
    if not base_path:
        return ""
    return origin.rstrip("/") + base_path


def is_openai_singleton(document: dict | None) -> bool:
    """Recognize the narrow one-model catalog shape used by some servers."""
    return bool(
        isinstance(document, dict)
        and document.get("object") == "model"
        and model_id(document.get("id"))
    )


def openai_catalog_rows(document: dict | None) -> list[dict] | None:
    """Normalize standard lists and strict singleton catalog documents only."""
    if not isinstance(document, dict):
        return None
    rows = document.get("data")
    if isinstance(rows, list):
        return rows
    if is_openai_singleton(document):
        return [document]
    return None


def normalized_openai_catalog(document: dict | None) -> dict | None:
    """Adapt one strict singleton to the adapter's normal OpenAI list contract."""
    rows = openai_catalog_rows(document)
    return {"data": rows} if rows is not None else None


def native_inventory_rows(document: dict | None) -> list[dict] | None:
    """Read explicit native model IDs without treating free-form fields as IDs."""
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        return None
    rows = []
    for item in document["models"]:
        if isinstance(item, str):
            identifier = model_id(item)
            if identifier:
                rows.append({"id": identifier})
            continue
        if not isinstance(item, dict):
            continue
        identifier = model_id(item.get("id")) or model_id(item.get("key"))
        if identifier:
            rows.append({**item, "id": identifier})
    return rows or None
