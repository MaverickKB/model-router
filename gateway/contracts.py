"""JSON contracts between catalog observation, routing policy, transport, and UI."""

from typing import Literal, NotRequired, Required

from typing_extensions import TypedDict

Kind = Literal["local", "cloud"]
Tier = Literal["primary", "fallback"]
EngineStatus = Literal[
    "checking",
    "available",
    "draining",
    "offline",
    "disabled",
    "unconfigured",
    "stale",
    "unavailable",
]


class ModelView(TypedDict):
    id: str
    capabilities: list[str]
    context_length: int | None
    enabled: bool
    loaded: NotRequired[bool]


class EngineView(TypedDict):
    id: str
    name: str
    base_url: str
    aliases: list[str]
    kind: Kind
    protocol: str
    catalog_protocol: str
    enabled: bool
    draining: bool
    source: str
    tags: list[str]
    members: list[str]
    capabilities: list[str]
    model_patterns: list[str]
    model_settings: dict
    unsupported_parameters: list[str]
    value_mappings: dict[str, dict[str, str]]
    timeout_seconds: float
    max_inflight: int
    max_response_bytes: int
    failure_cooldown_seconds: float
    rate_limit_cooldown_seconds: float
    models: list[ModelView]
    has_credential: bool
    status: EngineStatus
    checked_at: float
    observed_at: float
    latency_ms: float | None
    error: str
    inflight: int
    last_success: float | None


class Candidate(TypedDict):
    engine_id: str
    engine: str
    model: str
    kind: Kind
    tier: Tier
    order: int
    load: float
    skipped_defaults: list[str]
    reason: str


class Rejection(TypedDict):
    engine_id: str
    engine: str
    model: str | None
    tier: Tier
    reason: str


class Decision(TypedDict, total=False):
    candidates: Required[list[Candidate]]
    rejections: Required[list[Rejection]]
    route: str
    client: str
    error: str
    status: int
    error_type: str
    error_code: str
    required_capabilities: list[str]
    defaults: dict
    revision: int
