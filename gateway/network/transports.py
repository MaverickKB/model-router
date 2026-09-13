"""Bounded HTTP and HTTPS origin classification shared by discovery sources."""

import httpx

from .protocols import inspect_services


async def inspect_transports(
    http: httpx.AsyncClient, authority: str, port: int
) -> list[dict]:
    """Return every independently proven surface on both web transports.

    Requests stay sequential so adding HTTPS does not multiply concurrent
    metadata probes for one candidate.
    """
    surfaces: dict[str, dict] = {}
    for scheme in ("http", "https"):
        origin = f"{scheme}://{authority}:{port}"
        for service in await inspect_services(http, origin):
            surface_id = service.get("surface_id")
            if not isinstance(surface_id, str) or not surface_id:
                # The classifier supplies this key. Keep a stable fallback for
                # historical reports and callers still on its old contract.
                surface_id = str(
                    service.get("base_url")
                    or service.get("compatible_base_url")
                    or service.get("origin")
                    or origin
                )
                service = {**service, "surface_id": surface_id}
            surfaces[surface_id] = service
    return [surfaces[key] for key in sorted(surfaces)]
