"""Identify serving protocols from bounded, same-origin protocol evidence."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from ..adapters.ollama import ollama_models
from .catalogs import (
    DIALECT_CATALOG_PATHS,
    Document,
    catalog_base_path,
    fetch_document,
    model_id,
    native_inventory_rows,
    openai_catalog_rows,
)
from .openapi import (
    ApiBaseEvidence,
    OpenAPIEvidence,
    inspect_openapi,
    task_capabilities,
)

MAX_OPENAPI_CATALOG_PATHS = 8

_SURFACE_EVIDENCE = {
    "model_service": 60,
    "gateway": 50,
    "model_surface": 40,
    "native_inventory": 30,
    "authentication_required": 20,
    "http_service": 10,
    "unidentified": 0,
}


def _unique(values: list[str]) -> list[str]:
    """Preserve probe order while removing repeated exact protocol facts."""
    return list(dict.fromkeys(values))

__all__ = [
    "advertised_models",
    "inspect_service",
    "inspect_services",
    "is_gateway_catalog",
    "task_capabilities",
    "unavailable_reason",
]


def is_gateway_catalog(catalog: dict) -> bool:
    """Use an explicit protocol extension, never an owner or model name."""
    declaration = catalog.get("model_serving")
    return (
        isinstance(declaration, dict)
        and declaration.get("version") == 1
        and declaration.get("kind") == "router"
    )


def unavailable_reason(row: dict) -> str:
    provenance = row.get("provenance", {})
    health = provenance.get("health", {}) if isinstance(provenance, dict) else {}
    if isinstance(health, dict) and health.get("status") in {
        "unhealthy",
        "unavailable",
        "offline",
        "failed",
    }:
        return str(health.get("code") or "Backend reported unavailable")
    if row.get("available") is False:
        return str(row.get("availability_reason") or "Backend reported unavailable")
    return ""


def advertised_models(rows: list, capabilities: list[str]) -> list[dict]:
    """Preserve catalog rows while rejecting malformed model identifiers."""
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = model_id(row.get("id"))
        if not identifier:
            continue
        reason = unavailable_reason(row)
        stated = row.get("capabilities")
        declared = (
            [name for name, enabled in stated.items() if enabled is True]
            if isinstance(stated, dict)
            else stated
            if isinstance(stated, list)
            else capabilities
        )
        models.append(
            {
                "id": identifier,
                "capabilities": [name for name in declared if isinstance(name, str)],
                "available": not reason,
                **({"loaded": row["loaded"]} if isinstance(row.get("loaded"), bool) else {}),
                **({"availability_reason": reason} if reason else {}),
            }
        )
    return models


def _catalog_evidence(documents: list[Document]) -> list[dict]:
    attempts = []
    seen = set()
    for document in documents:
        if document.path not in seen:
            attempts.append(document.attempt())
            seen.add(document.path)
    return attempts


def _base_url(origin: str, base_path: str) -> str:
    """Render a proven API base, where an empty path is the service origin."""
    return origin.rstrip("/") if base_path == "" else origin.rstrip("/") + base_path


def _surface_key(origin: str, base_path: str | None, evidence_path: str) -> str:
    return _base_url(origin, base_path) if base_path is not None else f"{origin}#{evidence_path}"


def _surface(
    origin: str,
    *,
    status: str,
    protocol: str = "",
    base_path: str | None = None,
    models: list[dict] | None = None,
    capabilities: list[str] | None = None,
    completion_paths: list[str] | None = None,
    detail: str = "",
    registration_eligible: bool = False,
    needs_model_identity: bool = False,
    routeable_base: bool = True,
    evidence_path: str = "service",
    catalog_attempts: list[dict],
    catalog_paths_unprobed: int,
) -> dict:
    """Create one report surface with no guessed transport or identity fields."""
    observed_base_url = _base_url(origin, base_path) if base_path is not None else ""
    return {
        "origin": origin,
        "surface_id": _surface_key(origin, base_path, evidence_path),
        "status": status,
        "protocol": protocol,
        "base_url": (
            observed_base_url if status in {"model_service", "gateway"} else ""
        ),
        # This is set only when the same base declares a request contract the
        # router can forward. A catalog path or a completion-shaped URL is not
        # enough to turn a base into a usable OpenAI-compatible endpoint.
        "compatible_base_url": observed_base_url if routeable_base else "",
        "observed_base_url": observed_base_url,
        "models": models or [],
        "capabilities": capabilities or [],
        # Exact route-relative OpenAI operations independently proven for this
        # API base. An absent field is never interpreted as a guessed path by
        # auto-registration.
        "completion_paths": completion_paths or [],
        "registration_eligible": registration_eligible,
        "needs_model_identity": needs_model_identity,
        "catalog_attempts": catalog_attempts,
        "catalog_paths_unprobed": catalog_paths_unprobed,
        "detail": detail,
        "name": urlsplit(origin).hostname,
    }


def _surface_rank(service: dict) -> tuple[int, int, int, int, str]:
    """Pick a backwards-compatible primary view without discarding other surfaces."""
    return (
        _SURFACE_EVIDENCE.get(service.get("status"), 0),
        int(service.get("registration_eligible") is True),
        int(bool(service.get("base_url") or service.get("compatible_base_url"))),
        len(service.get("models", [])),
        str(service.get("surface_id", "")),
    )


def _selected_base_evidence(
    evidence_sets: list[OpenAPIEvidence],
) -> dict[str, ApiBaseEvidence]:
    """Aggregate only independently complete operation proofs for each base.

    Separate OpenAPI documents may each prove a different completion operation
    at the same API base.  Their public and protected operation lists may be
    combined because every list entry already passed both the request and
    response checks within its own document.  The compatibility booleans alone
    are deliberately never used to construct an operation: that would join a
    request proof from one document to a response proof from another.
    """
    collected: dict[str, list[ApiBaseEvidence]] = {}
    for evidence in evidence_sets:
        for candidate in evidence.bases:
            collected.setdefault(candidate.base_path, []).append(candidate)

    selected = {}
    for base_path, candidates in collected.items():
        declared_public_paths = _unique(
            [path for candidate in candidates for path in candidate.completion_paths]
        )
        declared_protected_paths = _unique(
            [
                path
                for candidate in candidates
                for path in candidate.protected_completion_paths
            ]
        )
        # One persisted engine cannot express document-dependent access for
        # the same operation. A public declaration and a protected declaration
        # therefore conflict rather than making the least restrictive reading
        # eligible for automatic registration. Keep the operation visible as a
        # credentialed review target until the documents agree.
        conflicting_paths = set(declared_public_paths) & set(declared_protected_paths)
        public_paths = [
            path for path in declared_public_paths if path not in conflicting_paths
        ]
        protected_paths = _unique(
            [
                *declared_protected_paths,
                *(
                    path
                    for path in declared_public_paths
                    if path in conflicting_paths
                ),
            ]
        )
        selected[base_path] = ApiBaseEvidence(
            base_path=base_path,
            capabilities=_unique(
                [
                    capability
                    for candidate in candidates
                    for capability in candidate.capabilities
                ]
            ),
            model_ids=_unique(
                [
                    model_id
                    for candidate in candidates
                    for model_id in candidate.model_ids
                ]
            ),
            protected_capabilities=_unique(
                [
                    capability
                    for candidate in candidates
                    for capability in candidate.protected_capabilities
                ]
            ),
            protected_model_ids=_unique(
                [
                    model_id
                    for candidate in candidates
                    for model_id in candidate.protected_model_ids
                ]
            ),
            completion_declared=any(
                candidate.completion_declared for candidate in candidates
            ),
            completion_request_compatible=any(
                candidate.completion_request_compatible for candidate in candidates
            ),
            completion_response_compatible=any(
                candidate.completion_response_compatible for candidate in candidates
            ),
            completion_compatible=bool(public_paths or protected_paths),
            completion_paths=public_paths,
            protected_completion_paths=protected_paths,
        )
    return selected


def _openai_catalog_surfaces(
    origin: str,
    documents: list[Document],
    evidence: dict[str, ApiBaseEvidence],
    *,
    catalog_attempts: list[dict],
    catalog_paths_unprobed: int,
) -> tuple[list[dict], set[str]]:
    """Turn each readable catalog into either a verified engine or an inventory."""
    surfaces = []
    matched_bases: set[str] = set()
    seen_paths: set[str] = set()
    seen_bases: set[str] = set()
    for document in documents:
        if document.path in seen_paths:
            continue
        seen_paths.add(document.path)
        rows = openai_catalog_rows(document.value)
        if rows is None:
            continue
        base_path = catalog_base_path(document.path)
        if base_path is None:
            continue
        # `/v1/models` and `/v1/models/` prove one API base. Keep the first
        # readable catalog in probe order rather than emitting duplicate cards
        # for a single endpoint identity.
        if base_path in seen_bases:
            continue
        seen_bases.add(base_path)
        base = evidence.get(base_path)
        if base and base.completion_paths:
            relay = is_gateway_catalog(document.value or {})
            models = advertised_models(rows, base.capabilities)
            surface = _surface(
                origin,
                status="gateway" if relay else "model_service",
                protocol="openai",
                base_path=base_path,
                models=models,
                capabilities=base.capabilities,
                completion_paths=base.completion_paths,
                registration_eligible=not relay and bool(models),
                detail=(
                    "Catalog is published by a routing service"
                    if relay
                    else "Catalog and POST completion operation are declared for this API base"
                ),
                evidence_path=document.path,
                catalog_attempts=catalog_attempts,
                catalog_paths_unprobed=catalog_paths_unprobed,
            )
            surfaces.append(surface)
            matched_bases.add(base_path)
        elif base and base.protected_completion_paths:
            # The catalog is readable, but OpenAPI says every compatible
            # completion operation requires credentials. Keep the exact
            # endpoint proof for a manual credentialed connection and never
            # auto-register a zero-credential engine.
            surfaces.append(
                _surface(
                    origin,
                    status="authentication_required",
                    protocol="openai",
                    base_path=base_path,
                    models=advertised_models(rows, base.protected_capabilities),
                    capabilities=base.protected_capabilities,
                    completion_paths=base.protected_completion_paths,
                    detail=(
                        "Catalog is readable, but the documented completion "
                        "operation requires credentials before this API can be connected"
                    ),
                    routeable_base=True,
                    evidence_path=document.path,
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            matched_bases.add(base_path)
        else:
            # A readable list matters to the operator, but cannot advertise a
            # route until the same API base also declares a completion POST.
            surfaces.append(
                _surface(
                    origin,
                    status="http_service",
                    protocol="openai",
                    base_path=base_path,
                    models=advertised_models(rows, []),
                    detail=(
                        "Model catalog and JSON completion request found, but "
                        "the documented success response does not declare an "
                        "OpenAI choices envelope"
                        if base and base.completion_request_compatible
                        else "Model catalog found, but this API base did not declare "
                        "a compatible JSON completion request contract"
                        if base and base.completion_declared
                        else "Model catalog found, but no same-origin POST completion "
                        "operation was declared for this API base"
                    ),
                    routeable_base=False,
                    evidence_path=document.path,
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            matched_bases.add(base_path)
    return surfaces, matched_bases


def _protected_catalog_surfaces(
    origin: str,
    documents: list[Document],
    evidence: dict[str, ApiBaseEvidence],
    matched_bases: set[str],
    *,
    catalog_attempts: list[dict],
    catalog_paths_unprobed: int,
) -> list[dict]:
    """Expose protected catalog targets without promoting them to engines."""
    surfaces = []
    for document in documents:
        if not document.authentication_required:
            continue
        base_path = catalog_base_path(document.path)
        base = evidence.get(base_path) if base_path is not None else None
        if base_path is None or base_path in matched_bases:
            continue
        if base and (base.completion_paths or base.protected_completion_paths):
            detail = (
                "Credentials are required before this API base can publish its model catalog"
            )
        elif base and base.completion_request_compatible:
            detail = (
                "Protected model catalog found. The documented completion response "
                "is incomplete, so review it with credentials before connecting."
            )
        elif base and base.completion_declared:
            detail = (
                "Protected model catalog found. The completion request contract is "
                "incomplete, so review it with credentials before connecting."
            )
        else:
            detail = (
                "Protected model catalog found. Review it with credentials before "
                "connecting because a completion contract was not documented."
            )
        surfaces.append(
            _surface(
                origin,
                status="authentication_required",
                protocol="openai",
                base_path=base_path,
                capabilities=(
                    [*base.capabilities, *base.protected_capabilities]
                    if base
                    else []
                ),
                completion_paths=(
                    [*base.completion_paths, *base.protected_completion_paths]
                    if base
                    else []
                ),
                detail=detail,
                # The catalog itself makes this a useful operator review target,
                # but only a full same-base contract can be routed automatically.
                routeable_base=bool(base and base.completion_paths),
                evidence_path=document.path,
                catalog_attempts=catalog_attempts,
                catalog_paths_unprobed=catalog_paths_unprobed,
            )
        )
        # This is the strongest honest evidence for this exact base until the
        # operator supplies credentials. Do not add a second catalogless
        # completion card with the same surface identity below.
        matched_bases.add(base_path)
    return surfaces


async def _native_inventory_surfaces(
    http: httpx.AsyncClient,
    origin: str,
    documents: list[Document],
    evidence: dict[str, ApiBaseEvidence],
    matched_bases: set[str],
    *,
    catalog_attempts: list[dict],
    catalog_paths_unprobed: int,
) -> list[dict]:
    """Keep native inventory separate unless a matching base proves its transport."""
    surfaces = []
    seen_paths: set[str] = set()
    seen_bases: set[str] = set()
    for document in documents:
        if document.path in seen_paths:
            continue
        seen_paths.add(document.path)
        if document.path == "/api/tags":
            rows = (
                document.value.get("models")
                if isinstance(document.value, dict)
                and isinstance(document.value.get("models"), list)
                else None
            )
            if not rows:
                continue
            native = await ollama_models(http, origin, rows)
            models = advertised_models(native, [])
            protocol = "ollama"
        else:
            rows = native_inventory_rows(document.value)
            if not rows:
                continue
            models = advertised_models(rows, [])
            protocol = "native"
        # Ollama's `/api/tags` is inventory evidence at a distinct native
        # surface, not an OpenAI `/models` catalog. Keep its own surface
        # identity instead of borrowing the origin base of another API.
        base_path = (
            None if document.path == "/api/tags" else catalog_base_path(document.path)
        )
        if base_path is not None and base_path in seen_bases:
            continue
        if base_path is not None:
            seen_bases.add(base_path)
        base = evidence.get(base_path) if base_path is not None else None
        if base and base.completion_paths and base_path not in matched_bases:
            surfaces.append(
                _surface(
                    origin,
                    status="model_surface",
                    protocol="openai",
                    base_path=base_path,
                    models=[
                        {**model, "capabilities": base.capabilities}
                        for model in models
                    ],
                    capabilities=base.capabilities,
                    completion_paths=base.completion_paths,
                    detail=(
                        "Native model inventory and a matching POST completion "
                        "operation are declared for this API base"
                    ),
                    needs_model_identity=not models,
                    evidence_path=document.path,
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            matched_bases.add(base_path)
        elif (
            base
            and base.protected_completion_paths
            and base_path not in matched_bases
        ):
            surfaces.append(
                _surface(
                    origin,
                    status="authentication_required",
                    protocol="openai",
                    base_path=base_path,
                    models=models,
                    capabilities=base.protected_capabilities,
                    completion_paths=base.protected_completion_paths,
                    detail=(
                        "Native model inventory is visible, but the documented "
                        "completion operation requires credentials before connecting"
                    ),
                    routeable_base=True,
                    evidence_path=document.path,
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            matched_bases.add(base_path)
        else:
            surfaces.append(
                _surface(
                    origin,
                    status="native_inventory",
                    protocol=protocol,
                    base_path=base_path,
                    models=models,
                    detail=(
                        "Native model inventory found; no matching POST completion "
                        "operation was declared"
                    ),
                    routeable_base=False,
                    evidence_path=document.path,
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
    return surfaces


async def inspect_services(http: httpx.AsyncClient, origin: str) -> list[dict]:
    """Return every independently evidenced model surface at one HTTP origin.

    A host can publish more than one API base. Each base remains a separate
    record, so a catalog, completion operation, or model identity is never
    silently paired with a different same-host API.
    """
    origin = origin.rstrip("/")
    openapi, *dialects = await asyncio.gather(
        fetch_document(http, origin, "/openapi.json"),
        *(fetch_document(http, origin, path) for path in DIALECT_CATALOG_PATHS),
    )
    v1_catalog, root_catalog, native_v1_catalog, ollama = dialects
    initial_catalogs = [v1_catalog, root_catalog, native_v1_catalog, ollama]

    # A strict readable OpenAI catalog establishes a canonical non-root base,
    # not a completion transport. It is the only evidence allowed to request a
    # bounded sibling OpenAPI document such as `/v1/openapi.json`.
    prefixed_openapi_paths = []
    for document in initial_catalogs:
        base_path = catalog_base_path(document.path)
        if (
            base_path
            and openai_catalog_rows(document.value) is not None
        ):
            prefixed_openapi_paths.append(f"{base_path}/openapi.json")
    prefixed_openapi_paths = list(
        dict.fromkeys(
            path for path in prefixed_openapi_paths if path != "/openapi.json"
        )
    )[:MAX_OPENAPI_CATALOG_PATHS]
    prefixed_openapi = await asyncio.gather(
        *(fetch_document(http, origin, path) for path in prefixed_openapi_paths)
    )
    openapi_documents = [openapi, *prefixed_openapi]
    evidence_sets = [
        inspect_openapi(document.value, document_path=document.path, origin=origin)
        for document in openapi_documents
    ]

    # Catalog paths declared by any fetched document are still bounded. Paths
    # already probed as standard dialects or obtained above are never replayed.
    known_catalog_paths = {document.path for document in initial_catalogs}
    extra_candidates = list(
        dict.fromkeys(
            path
            for evidence_set in evidence_sets
            for path in evidence_set.catalog_paths
            if path not in known_catalog_paths
        )
    )
    extra_paths = extra_candidates[:MAX_OPENAPI_CATALOG_PATHS]
    extra_catalogs = await asyncio.gather(
        *(fetch_document(http, origin, path) for path in extra_paths)
    )
    documents = [
        *initial_catalogs,
        *extra_catalogs,
    ]
    catalog_attempts = _catalog_evidence(documents)
    catalog_paths_unprobed = len(extra_candidates) - len(extra_paths)
    evidence = _selected_base_evidence(evidence_sets)

    surfaces, matched_bases = _openai_catalog_surfaces(
        origin,
        documents,
        evidence,
        catalog_attempts=catalog_attempts,
        catalog_paths_unprobed=catalog_paths_unprobed,
    )
    surfaces.extend(
        _protected_catalog_surfaces(
            origin,
            documents,
            evidence,
            matched_bases,
            catalog_attempts=catalog_attempts,
            catalog_paths_unprobed=catalog_paths_unprobed,
        )
    )
    surfaces.extend(
        await _native_inventory_surfaces(
            http,
            origin,
            documents,
            evidence,
            matched_bases,
            catalog_attempts=catalog_attempts,
            catalog_paths_unprobed=catalog_paths_unprobed,
        )
    )
    for base_path, base in evidence.items():
        if base_path in matched_bases:
            continue
        if not base.completion_compatible:
            if not base.completion_declared:
                continue
            surfaces.append(
                _surface(
                    origin,
                    status="http_service",
                    protocol="openapi",
                    base_path=base_path,
                    detail=(
                        "POST completion request found, but its documented success "
                        "response does not declare an OpenAI choices envelope"
                        if base.completion_request_compatible
                        else "POST completion-shaped path found, but its OpenAPI "
                        "request schema does not prove a JSON model and input contract"
                    ),
                    routeable_base=False,
                    evidence_path=f"{base_path or '/'} completion",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            continue
        if base.protected_completion_paths and not base.completion_paths:
            surfaces.append(
                _surface(
                    origin,
                    status="authentication_required",
                    protocol="openai",
                    base_path=base_path,
                    models=[
                        {
                            "id": identifier,
                            "capabilities": base.protected_capabilities,
                            "available": True,
                        }
                        for identifier in base.protected_model_ids
                    ],
                    capabilities=base.protected_capabilities,
                    completion_paths=base.protected_completion_paths,
                    needs_model_identity=not base.protected_model_ids,
                    detail=(
                        "Documented completion operation requires credentials "
                        "before this API can be connected"
                    ),
                    routeable_base=True,
                    evidence_path=f"{base_path or '/'} completion",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
            continue
        surfaces.append(
            _surface(
                origin,
                status="model_surface",
                protocol="openai",
                base_path=base_path,
                models=[
                    {
                        "id": identifier,
                        "capabilities": base.capabilities,
                        "available": True,
                    }
                    for identifier in base.model_ids
                ],
                capabilities=base.capabilities,
                completion_paths=base.completion_paths,
                needs_model_identity=not base.model_ids,
                detail=(
                    "POST completion operation declares exact model identities"
                    if base.model_ids
                    else "POST completion operation found; no readable model catalog or exact model identity was published"
                ),
                evidence_path=f"{base_path or '/'} completion",
                catalog_attempts=catalog_attempts,
                catalog_paths_unprobed=catalog_paths_unprobed,
            )
        )
    if not surfaces:
        if any(document.authentication_required for document in openapi_documents):
            surfaces.append(
                _surface(
                    origin,
                    status="authentication_required",
                    detail="Credentials are required before the service can be inspected",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
        elif any(evidence_set.capabilities for evidence_set in evidence_sets):
            surfaces.append(
                _surface(
                    origin,
                    status="http_service",
                    protocol="openapi",
                    capabilities=[
                        capability
                        for evidence_set in evidence_sets
                        for capability in evidence_set.capabilities
                    ],
                    detail="OpenAPI operations found, but no POST text completion operation was declared",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
        elif any(document.responded for document in [*openapi_documents, *documents]):
            surfaces.append(
                _surface(
                    origin,
                    status="http_service",
                    protocol="http",
                    detail="HTTP endpoint found; no model catalog or completion transport identified",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
        else:
            surfaces.append(
                _surface(
                    origin,
                    status="unidentified",
                    catalog_attempts=catalog_attempts,
                    catalog_paths_unprobed=catalog_paths_unprobed,
                )
            )
    openapi_title = next(
        (evidence_set.title for evidence_set in evidence_sets if evidence_set.title),
        "",
    )
    if openapi_title:
        for surface in surfaces:
            surface["openapi_title"] = openapi_title
    return sorted(surfaces, key=_surface_rank, reverse=True)


async def inspect_service(http: httpx.AsyncClient, origin: str) -> dict:
    """Return the strongest surface for compatibility callers.

    Network discovery consumes :func:`inspect_services` so it does not lose
    other API bases at the same address.
    """
    return (await inspect_services(http, origin))[0]
