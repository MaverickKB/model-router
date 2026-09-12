"""Bounded HTTP metadata documents, shared by catalog adapters."""

import asyncio
import json

import httpx


async def json_document(http: httpx.AsyncClient, url: str, payload=None, headers=None):
    try:
        async with (
            asyncio.timeout(4),
            http.stream(
                "POST" if payload is not None else "GET",
                url,
                json=payload,
                headers=headers,
                timeout=2,
            ) as response,
        ):
            if response.status_code in {401, 403}:
                return {"authentication_required": True}
            if response.status_code != 200:
                return {"http_response": True, "status": response.status_code}
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    return {"http_response": True}
            try:
                value = json.loads(raw)
            except ValueError:
                return {"http_response": True}
            return value if isinstance(value, dict) else {"http_response": True}
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        return None
