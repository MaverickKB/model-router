"""Structural OpenAPI evidence for discovery, without title or example guessing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from .catalogs import model_id

MAX_LOCAL_REF_DEPTH = 8

_TASK_SUFFIXES = {
    "/chat/completions": ["text", "streaming"],
    "/completions": ["text", "streaming"],
    "/embeddings": ["embeddings"],
    "/audio/speech": ["speech"],
    "/audio/transcriptions": ["transcription"],
    "/images/generations": ["image_generation"],
}


@dataclass(frozen=True)
class ApiBaseEvidence:
    """One same-origin API base and facts declared for that exact base."""

    base_path: str
    capabilities: list[str]
    model_ids: list[str]
    completion_declared: bool = False
    completion_request_compatible: bool = False
    completion_response_compatible: bool = False
    completion_compatible: bool = False
    # These are route-relative operations whose full JSON request and response
    # contracts were proved in this document. Public operations can be
    # registered without credentials; protected operations remain visible for
    # an operator to connect with a credential.
    completion_paths: list[str] = field(default_factory=list)
    protected_completion_paths: list[str] = field(default_factory=list)
    # Static model IDs and capabilities have the same access boundary as the
    # operation that declared them. A zero-credential engine must never adopt
    # identifiers or capabilities published only by a protected operation.
    protected_capabilities: list[str] = field(default_factory=list)
    protected_model_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpenAPIEvidence:
    """Only protocol facts that a same-origin OpenAPI document actually declares."""

    title: str
    bases: list[ApiBaseEvidence]
    catalog_paths: list[str]

    @property
    def capabilities(self) -> list[str]:
        return _unique(
            [capability for base in self.bases for capability in base.capabilities]
        )

    @property
    def compatible_base_paths(self) -> list[str]:
        return [base.base_path for base in self.bases if "text" in base.capabilities]

    @property
    def static_model_ids(self) -> list[str]:
        return _unique([model for base in self.bases for model in base.model_ids])

    def base(self, path: str) -> ApiBaseEvidence | None:
        return next((base for base in self.bases if base.base_path == path), None)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def resolve_local(document: dict, value: object) -> dict | None:
    """Resolve a bounded local JSON Pointer and reject remote or cyclic refs."""
    current = value
    seen: set[str] = set()
    for _ in range(MAX_LOCAL_REF_DEPTH):
        if not isinstance(current, dict):
            return None
        reference = current.get("$ref")
        if not isinstance(reference, str):
            return current
        if not reference.startswith("#/") or reference in seen:
            return None
        seen.add(reference)
        target: Any = document
        for token in reference[2:].split("/"):
            if not isinstance(target, dict):
                return None
            target = target.get(token.replace("~1", "/").replace("~0", "~"))
        current = target
    return None


def _safe_relative_path(value: object, *, allow_empty: bool) -> str | None:
    """Return a literal, canonical same-origin path without rewriting it.

    OpenAPI paths feed directly into HTTP probes and later become engine bases.
    A dot segment or repeated separator has transport-dependent normalization,
    so it cannot safely identify the exact API base discovered on the network.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if allow_empty and value in {"", "/"}:
        return ""
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "{" in value
        or "}" in value
    ):
        return None
    path = parsed.path
    if path == "/":
        return "" if allow_empty else "/"
    # A trailing slash is an exact documented path and may be meaningful to a
    # server. Interior empty, dot, encoded-separator, and backslash segments
    # are rejected instead of silently canonicalized.
    segments = path[1:].split("/")
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if not segments:
        return "" if allow_empty else "/"
    for segment in segments:
        decoded = unquote(segment)
        if (
            not segment
            or decoded in {"", ".", ".."}
            or "/" in decoded
            or "\\" in decoded
        ):
            return None
    return path


def _safe_server_segment(value: object) -> str | None:
    """Accept one literal or declared-default path segment for a server URL."""
    if not isinstance(value, str) or not value:
        return None
    decoded = unquote(value)
    if (
        decoded in {"", ".", ".."}
        or "/" in decoded
        or "\\" in decoded
        or "{" in value
        or "}" in value
    ):
        return None
    return value


def _expand_server_variables(value: str, variables: object) -> str | None:
    """Expand only full safe server-path segments with one declared default."""
    segments = value.split("/")
    expanded = []
    for segment in segments:
        if segment.startswith("{") or segment.endswith("}"):
            if not (segment.startswith("{") and segment.endswith("}")):
                return None
            name = segment[1:-1]
            variable = variables.get(name) if isinstance(variables, dict) else None
            default = variable.get("default") if isinstance(variable, dict) else None
            safe = _safe_server_segment(default)
            if not safe:
                return None
            expanded.append(safe)
        elif "{" in segment or "}" in segment:
            return None
        else:
            expanded.append(segment)
    return "/".join(expanded)


def _document_directory(document_path: str) -> str | None:
    """Return a safe absolute directory for a fetched OpenAPI document."""
    path = _safe_relative_path(document_path, allow_empty=False)
    if path is None:
        return None
    directory = path.rsplit("/", 1)[0]
    return directory if directory else ""


def _canonical_origin(value: str | None) -> tuple[str, str, int] | None:
    """Return a comparison key for a literal HTTP origin without credentials."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )


def _safe_server_prefix(
    value: object,
    variables: object,
    document_path: str,
    origin: str | None,
) -> str | None:
    """Resolve one literal same-origin OAS server URL without traversal.

    OAS permits relative server URLs. They are resolved against the fetched
    document location only when explicit, while a document with no ``servers``
    retains the OAS root-base default.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value in {"", "/"}:
        return ""
    parsed = urlsplit(value)
    if value.startswith("//") or parsed.query or parsed.fragment or "\\" in value:
        return None
    if parsed.scheme or parsed.netloc:
        # A literal absolute URL is allowed only when it names the exact
        # inspected HTTP origin. It is converted to its same-origin API path,
        # never fetched as a new network target.
        if (
            _canonical_origin(value) != _canonical_origin(origin)
            or "{" in value
            or "}" in value
        ):
            return None
        path = _safe_relative_path(parsed.path or "/", allow_empty=True)
        return path.rstrip("/") if path is not None else None
    value = _expand_server_variables(value, variables)
    if value is None:
        return None
    if value.startswith("/"):
        path = _safe_relative_path(value, allow_empty=True)
        return path.rstrip("/") if path is not None else None

    # `./v1` is a valid relative OAS server URL. Only leading current-directory
    # segments are accepted; parent traversal and interior dot segments are not.
    if value in {".", "./"}:
        return _document_directory(document_path)
    parts = value.split("/")
    while parts and parts[0] == ".":
        parts.pop(0)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    relative = "/".join(parts)
    path = _safe_relative_path("/" + relative, allow_empty=False)
    directory = _document_directory(document_path)
    if path is None or directory is None:
        return None
    resolved = f"{directory}{path}" if directory else path
    path = _safe_relative_path(resolved, allow_empty=True)
    return path.rstrip("/") if path is not None else None


def _server_prefixes(
    servers: object,
    inherited: list[str],
    document_path: str,
    origin: str | None,
) -> list[str]:
    """Use the closest literal relative OAS servers without crossing origin.

    OpenAPI permits ``servers`` at the document, path-item, and operation
    levels.  A closer non-empty list replaces its parent.  If every entry in
    that closer list is external or templated, there is no safe same-origin
    base to derive, so callers must not fall back to a different parent base.
    """
    if not isinstance(servers, list) or not servers:
        return inherited
    prefixes = [
        prefix
        for server in servers
        if isinstance(server, dict)
        and (
            prefix := _safe_server_prefix(
                server.get("url"),
                server.get("variables"),
                document_path,
                origin,
            )
        )
        is not None
    ]
    return _unique(prefixes)


def _paths(document: dict) -> dict:
    paths = document.get("paths")
    return paths if isinstance(paths, dict) else {}


def _security_required(document: dict, path_item: dict, operation: dict) -> bool:
    """Apply the nearest declared OAS security requirement to one operation."""
    for value in (operation, path_item, document):
        if "security" in value and isinstance(value.get("security"), list):
            requirements = value["security"]
            # OpenAPI treats entries as alternatives. An empty security
            # requirement object is the documented anonymous alternative, so
            # `[{}, {"bearer": []}]` is public just like `security: []`.
            # A nonempty list with no anonymous alternative requires a
            # credential; malformed entries stay conservative.
            return bool(requirements) and not any(
                isinstance(requirement, dict) and not requirement
                for requirement in requirements
            )
    return False


def operations(
    document: dict,
    method: str,
    *,
    document_path: str = "/openapi.json",
    origin: str | None = None,
) -> list[tuple[str, dict, bool]]:
    """Return exact same-origin operations after safe OAS server resolution."""
    result = []
    document_prefixes = _server_prefixes(
        document.get("servers"), [""], document_path, origin
    )
    for path, path_item in _paths(document).items():
        path = _safe_relative_path(path, allow_empty=False)
        if path is None:
            continue
        resolved_item = resolve_local(document, path_item)
        if not resolved_item:
            continue
        operation = resolve_local(document, resolved_item.get(method.lower()))
        if operation is None:
            continue
        path_prefixes = _server_prefixes(
            resolved_item.get("servers"), document_prefixes, document_path, origin
        )
        operation_prefixes = _server_prefixes(
            operation.get("servers"), path_prefixes, document_path, origin
        )
        path_parameters = resolved_item.get("parameters")
        operation_parameters = operation.get("parameters")
        if isinstance(path_parameters, list) or isinstance(operation_parameters, list):
            # OpenAPI allows path-item parameters. Operation parameters with
            # the same (name, in) pair replace them.
            parameters: dict[tuple[str, str], object] = {}
            for values in (path_parameters, operation_parameters):
                if not isinstance(values, list):
                    continue
                for parameter in values:
                    resolved_parameter = resolve_local(document, parameter)
                    if not resolved_parameter:
                        continue
                    name = resolved_parameter.get("name")
                    location = resolved_parameter.get("in")
                    if isinstance(name, str) and isinstance(location, str):
                        parameters[(name, location)] = parameter
            operation = {**operation, "parameters": list(parameters.values())}
        for prefix in operation_prefixes:
            result.append(
                (
                    f"{prefix}{path}" if prefix else path,
                    operation,
                    _security_required(document, resolved_item, operation),
                )
            )
    return result


def _base_for_path(path: str, suffix: str) -> str | None:
    base = path.removesuffix(suffix).rstrip("/")
    if base in {"", "/"}:
        return ""
    if "{" not in base and "}" not in base:
        return base
    return None


def _static_ids(document: dict, schema: object, depth=0) -> list[str]:
    """Accept only a string const or string enum at a model schema node."""
    if depth >= MAX_LOCAL_REF_DEPTH:
        return []
    resolved = resolve_local(document, schema)
    if not resolved:
        return []
    values = []
    constant = model_id(resolved.get("const"))
    if constant:
        values.append(constant)
    enumeration = resolved.get("enum")
    if isinstance(enumeration, list):
        values.extend(value for value in (model_id(item) for item in enumeration) if value)
    for branch_name in ("allOf", "anyOf", "oneOf"):
        branches = resolved.get(branch_name)
        if isinstance(branches, list):
            for branch in branches:
                values.extend(_static_ids(document, branch, depth + 1))
    return _unique(values)


def _request_model_ids(document: dict, schema: object, depth=0) -> list[str]:
    """Find the named request `model` property through bounded local schemas."""
    if depth >= MAX_LOCAL_REF_DEPTH:
        return []
    resolved = resolve_local(document, schema)
    if not resolved:
        return []
    values = []
    properties = resolved.get("properties")
    if isinstance(properties, dict) and "model" in properties:
        values.extend(_static_ids(document, properties["model"], depth + 1))
    for branch_name in ("allOf", "anyOf", "oneOf"):
        branches = resolved.get(branch_name)
        if isinstance(branches, list):
            for branch in branches:
                values.extend(_request_model_ids(document, branch, depth + 1))
    return _unique(values)


def _json_request_schemas(document: dict, operation: dict) -> list[dict]:
    """Return request schemas the router can actually send as JSON."""
    schemas = []
    body = resolve_local(document, operation.get("requestBody"))
    content = body.get("content") if body else None
    if isinstance(content, dict):
        for media_type, media in content.items():
            if not isinstance(media_type, str) or not (
                media_type == "application/json" or media_type.endswith("+json")
            ):
                continue
            resolved = resolve_local(document, media)
            schema = resolve_local(document, resolved.get("schema")) if resolved else None
            if schema:
                schemas.append(schema)
    return schemas


def _merge_property_variants(
    left: dict[str, list[object]], right: dict[str, list[object]]
) -> dict[str, list[object]]:
    """Combine all-of property constraints without losing either schema."""
    return {
        name: [*left.get(name, []), *right.get(name, [])]
        for name in left.keys() | right.keys()
    }


def _property_variants(
    document: dict, schema: object, depth=0
) -> list[dict[str, list[object]]]:
    """Enumerate bounded property alternatives without mixing one-of branches."""
    if depth >= MAX_LOCAL_REF_DEPTH:
        return []
    resolved = resolve_local(document, schema)
    if not resolved:
        return []
    properties = resolved.get("properties")
    direct = (
        {name: [value] for name, value in properties.items() if isinstance(name, str)}
        if isinstance(properties, dict)
        else {}
    )
    alternatives = [direct]
    all_of = resolved.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            branch_variants = _property_variants(document, branch, depth + 1)
            if not branch_variants:
                continue
            alternatives = [
                _merge_property_variants(current, candidate)
                for current in alternatives
                for candidate in branch_variants
            ][:32]
    for branch_name in ("anyOf", "oneOf"):
        branches = resolved.get(branch_name)
        if not isinstance(branches, list):
            continue
        candidates = [
            _merge_property_variants(current, candidate)
            for current in alternatives
            for branch in branches
            for candidate in _property_variants(document, branch, depth + 1)
        ]
        if candidates:
            alternatives = candidates[:32]
    return alternatives


def _type_verdict(document: dict, schema: object, expected: str, depth=0) -> int:
    """Return accept (1), unknown (0), or reject (-1) for one JSON type."""
    if depth >= MAX_LOCAL_REF_DEPTH:
        return 0
    resolved = resolve_local(document, schema)
    if not resolved:
        return 0
    verdicts = []
    declared = resolved.get("type")
    if isinstance(declared, str):
        verdicts.append(1 if declared == expected else -1)
    elif isinstance(declared, list) and all(isinstance(item, str) for item in declared):
        verdicts.append(1 if expected in declared else -1)
    values = []
    if "const" in resolved:
        values.append(resolved["const"])
    if isinstance(resolved.get("enum"), list):
        values.extend(resolved["enum"])
    if values:
        value_matches = {
            "string": lambda value: isinstance(value, str),
            "array": lambda value: isinstance(value, list),
        }.get(expected, lambda value: False)
        verdicts.append(1 if any(value_matches(value) for value in values) else -1)
    for branch in resolved.get("allOf", []):
        verdicts.append(_type_verdict(document, branch, expected, depth + 1))
    for branch_name in ("anyOf", "oneOf"):
        branches = resolved.get(branch_name)
        if not isinstance(branches, list):
            continue
        branch_verdicts = [
            _type_verdict(document, branch, expected, depth + 1)
            for branch in branches
        ]
        if any(value == 1 for value in branch_verdicts):
            verdicts.append(1)
        elif branch_verdicts and all(value == -1 for value in branch_verdicts):
            verdicts.append(-1)
        else:
            verdicts.append(0)
    if any(value == -1 for value in verdicts):
        return -1
    return 1 if any(value == 1 for value in verdicts) else 0


def _schemas_allow_type(document: dict, schemas: list[object], expected: str) -> bool:
    """Require a positive compatible type and reject contradictory all-of constraints."""
    verdicts = [_type_verdict(document, schema, expected) for schema in schemas]
    return bool(verdicts) and any(value == 1 for value in verdicts) and not any(
        value == -1 for value in verdicts
    )


def _completion_contract(document: dict, operation: dict, suffix: str) -> bool:
    """Require the JSON request shape that this router will forward.

    The path name alone does not prove OpenAI compatibility.  Automatic
    registration needs a JSON request body containing ``model`` and the input
    field used by that endpoint: ``messages`` for chat or ``prompt`` for text
    completion.  Other POST APIs remain visible as unverified services.
    """
    input_name = "messages" if suffix == "/chat/completions" else "prompt"
    input_type = "array" if input_name == "messages" else "string"
    return any(
        "model" in properties
        and input_name in properties
        and _schemas_allow_type(document, properties["model"], "string")
        and _schemas_allow_type(document, properties[input_name], input_type)
        for schema in _json_request_schemas(document, operation)
        for properties in _property_variants(document, schema)
    )


def _completion_response_contract(document: dict, operation: dict) -> bool:
    """Require an OpenAI choices envelope in a documented success response."""
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return False
    for status, response in responses.items():
        is_success = (
            (isinstance(status, str) and status.upper() == "2XX")
            or (
                isinstance(status, str)
                and status.isdigit()
                and 200 <= int(status) < 300
            )
        )
        if not is_success:
            continue
        resolved_response = resolve_local(document, response)
        content = resolved_response.get("content") if resolved_response else None
        if not isinstance(content, dict):
            continue
        for media_type, media in content.items():
            if not isinstance(media_type, str) or not (
                media_type == "application/json" or media_type.endswith("+json")
            ):
                continue
            resolved_media = resolve_local(document, media)
            schema = (
                resolve_local(document, resolved_media.get("schema"))
                if resolved_media
                else None
            )
            if schema and any(
                "choices" in properties
                and _schemas_allow_type(document, properties["choices"], "array")
                for properties in _property_variants(document, schema)
            ):
                return True
    return False


def operation_model_ids(document: dict, operation: dict) -> list[str]:
    """Read only JSON-body schema model identifiers for one completion operation."""
    return _unique(
        identifier
        for schema in _json_request_schemas(document, operation)
        for identifier in _request_model_ids(document, schema)
    )


def inspect_openapi(
    document: dict | None,
    *,
    document_path: str = "/openapi.json",
    origin: str | None = None,
) -> OpenAPIEvidence:
    """Classify an OpenAPI document by transport and explicit model metadata."""
    document = document if isinstance(document, dict) else {}
    info = document.get("info")
    title = str(info.get("title", "")) if isinstance(info, dict) else ""
    facts: dict[str, dict[str, list]] = {}
    for path, operation, requires_credentials in operations(
        document, "post", document_path=document_path, origin=origin
    ):
        for suffix, capabilities in _TASK_SUFFIXES.items():
            if not path.endswith(suffix):
                continue
            base = _base_for_path(path, suffix)
            if base is None:
                continue
            fact = facts.setdefault(
                base,
                {
                    "capabilities": [],
                    "model_ids": [],
                    "completion_declared": [],
                    "completion_request_compatible": [],
                    "completion_response_compatible": [],
                    "completion_compatible": [],
                    "completion_paths": [],
                    "protected_completion_paths": [],
                    "protected_capabilities": [],
                    "protected_model_ids": [],
                },
            )
            request_compatible = suffix not in {
                "/chat/completions",
                "/completions",
            } or _completion_contract(document, operation, suffix)
            response_compatible = suffix not in {
                "/chat/completions",
                "/completions",
            } or _completion_response_contract(document, operation)
            compatible = request_compatible and response_compatible
            if compatible:
                capability_key = (
                    "protected_capabilities" if requires_credentials else "capabilities"
                )
                fact[capability_key].extend(capabilities)
            if suffix in {"/chat/completions", "/completions"}:
                fact["completion_declared"].append(True)
                fact["completion_request_compatible"].append(request_compatible)
                fact["completion_response_compatible"].append(response_compatible)
                fact["completion_compatible"].append(compatible)
                if compatible:
                    if requires_credentials:
                        fact["protected_completion_paths"].append(suffix)
                        fact["protected_model_ids"].extend(
                            operation_model_ids(document, operation)
                        )
                    else:
                        fact["completion_paths"].append(suffix)
                        fact["model_ids"].extend(
                            operation_model_ids(document, operation)
                        )
            break
    catalog_paths = [
        path
        for path, _, _ in operations(
            document, "get", document_path=document_path, origin=origin
        )
        if path.rstrip("/").endswith("/models")
        and "{" not in path
        and "}" not in path
    ]
    return OpenAPIEvidence(
        title=title,
        bases=[
            ApiBaseEvidence(
                base_path=base,
                capabilities=_unique(values["capabilities"]),
                model_ids=_unique(values["model_ids"]),
                completion_declared=bool(values["completion_declared"]),
                # The lists retain one fact per documented operation. Their
                # non-emptiness only says an operation was inspected, not that
                # it met the contract. A base is compatible when at least one
                # of its own completion operations proves the full contract.
                completion_request_compatible=any(
                    values["completion_request_compatible"]
                ),
                completion_response_compatible=any(
                    values["completion_response_compatible"]
                ),
                completion_compatible=any(values["completion_compatible"]),
                completion_paths=_unique(values["completion_paths"]),
                protected_completion_paths=_unique(
                    values["protected_completion_paths"]
                ),
                protected_capabilities=_unique(values["protected_capabilities"]),
                protected_model_ids=_unique(values["protected_model_ids"]),
            )
            for base, values in facts.items()
        ],
        catalog_paths=_unique(catalog_paths),
    )


def task_capabilities(paths: dict) -> list[str]:
    """Compatibility wrapper for callers that only have an OpenAPI `paths` map."""
    return inspect_openapi({"paths": paths}).capabilities
