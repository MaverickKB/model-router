from __future__ import annotations

import ipaddress
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .adapters import CATALOG_ADAPTERS


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NamedRecord(Record):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = Field(min_length=1, max_length=100)

    @field_validator("id")
    @classmethod
    def opaque_identifier(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("name")
    @classmethod
    def readable_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Choose a nonempty name")
        return value


def identifier() -> str:
    return uuid4().hex


class ModelSettings(Record):
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=lambda: ["text", "streaming"])
    context_length: int | None = Field(default=None, ge=1)


class Engine(NamedRecord):
    id: str = Field(default_factory=identifier)
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["local", "cloud"] = "local"
    protocol: Literal["openai"] = "openai"
    catalog_protocol: str = "openai"
    # Discovery may suggest a host-derived label. Operators keep ownership of
    # manually edited names, so later scans cannot overwrite them.
    name_source: Literal["operator", "discovered"] = "operator"

    @field_validator("catalog_protocol")
    @classmethod
    def installed_catalog_adapter(cls, value: str) -> str:
        if value not in CATALOG_ADAPTERS:
            raise ValueError("Catalog adapter is not installed")
        return value

    enabled: bool = True
    draining: bool = False
    source: str = "manual"
    tags: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=lambda: ["text", "streaming"])
    model_settings: dict[str, ModelSettings] = Field(default_factory=dict)
    value_mappings: dict[str, dict[str, str]] = Field(default_factory=dict)
    model_patterns: list[str] = Field(default_factory=lambda: ["*"])
    unsupported_parameters: list[str] = Field(default_factory=list)

    timeout_seconds: float = Field(default=300, ge=1, le=3600)
    max_inflight: int = Field(default=32, ge=1, le=10000)
    max_response_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024
    )
    failure_cooldown_seconds: float = Field(default=15, ge=0, le=300)
    rate_limit_cooldown_seconds: float = Field(default=5, ge=0, le=300)

    @model_validator(mode="after")
    def cloud_selection_is_explicit(self):
        if self.kind == "cloud" and "model_patterns" not in self.model_fields_set:
            self.model_patterns = []
        return self

    @field_validator("value_mappings")
    @classmethod
    def preserve_request_structure(cls, value):
        structural = {
            "model",
            "messages",
            "prompt",
            "tools",
            "tool_choice",
            "stream",
            "functions",
            "function_call",
            "input",
        }
        if set(value) & structural:
            raise ValueError(
                "Translations may change option values, not model identity or request structure"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Use an http(s) endpoint without credentials, query, or fragment"
            )
        path = url.path.rstrip("/")
        if not path:
            path = "/v1"
        return f"{url.scheme}://{url.netloc}{path}"

    @classmethod
    def validate_connection_url(cls, value: str) -> None:
        # Stored configurations may contain endpoints accepted by earlier
        # versions. Validate connection ports when an endpoint is changed,
        # while allowing an unchanged invalid endpoint to be repaired in place.
        try:
            port = urlsplit(cls.validate_url(value)).port
            if port == 0:
                raise ValueError
        except ValueError:
            raise ValueError(
                "Use an endpoint with a valid TCP port (1-65535)"
            ) from None

    @field_validator("aliases")
    @classmethod
    def alias_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(cls.validate_url(value) for value in values))

    @model_validator(mode="after")
    def preferred_is_not_an_alias(self):
        self.aliases = [url for url in self.aliases if url != self.base_url]
        return self

    @property
    def endpoint_urls(self) -> list[str]:
        return [self.base_url, *self.aliases]


class Selector(Record):
    kind: Literal["local", "cloud", "any"] = "local"
    engine_ids: list[str] = Field(default_factory=list)
    model_patterns: list[str] = Field(default_factory=lambda: ["*"])
    tags: list[str] = Field(default_factory=list)


class Route(NamedRecord):
    id: str = Field(default_factory=identifier)
    name: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    )
    purpose: str = Field(default="", max_length=500)
    enabled: bool = True
    primary: Selector = Field(default_factory=Selector)
    fallback: Selector | None = None
    strategy: Literal["ordered", "least_busy"] = "least_busy"
    # Access is a property of the advertised route.  It applies equally to
    # local and cloud engines, and is deliberately independent of the global
    # operator/session settings below.
    require_caller_key: bool = False
    defaults: dict = Field(default_factory=dict)

    @field_validator("defaults")
    @classmethod
    def safe_defaults(cls, value: dict) -> dict:
        allowed = {
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "reasoning_effort",
            "chat_template_kwargs",
        }
        if set(value) - allowed:
            raise ValueError(
                "Route defaults may contain sampling and reasoning options only"
            )
        return value


class Client(NamedRecord):
    id: str = Field(default_factory=identifier)
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["shared", "agent", "machine", "person"] | None = None
    enabled: bool = True
    route_names: list[str] = Field(default_factory=lambda: ["auto"])
    engine_ids: list[str] = Field(default_factory=list)
    model_patterns: list[str] = Field(default_factory=lambda: ["*"])
    allow_cloud: bool = False
    allow_direct_models: bool = False
    allow_network_auth: bool = False
    source_networks: list[str] = Field(default_factory=list)


class Discovery(Record):
    enabled: bool = False
    targets: list[str] = Field(default_factory=list)
    interval_seconds: int = Field(default=60, ge=5, le=3600)
    refresh_seconds: int = Field(default=10, ge=2, le=300)
    stale_seconds: int = Field(default=35, ge=5, le=3600)
    port_range: str = "8000-8100"
    scanner: Literal["connect", "nmap"] = "connect"
    inspect_all_open_ports: bool = False
    http_ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(
        default_factory=list
    )
    include_loopback: bool = False
    max_addresses: int = Field(default=4096, ge=1)
    network_interval_seconds: int = Field(default=1800, ge=60, le=86400)
    packets_per_second: int = Field(default=1500, ge=10, le=10000)
    auto_register: bool = False
    mdns: bool = False
    ignored_urls: list[str] = Field(default_factory=list)

    @field_validator("port_range")
    @classmethod
    def valid_range(cls, value: str) -> str:
        for part in value.split(","):
            ends = part.split("-")
            if len(ends) > 2 or any(
                not e.isdigit() or not 1 <= int(e) <= 65535 for e in ends
            ):
                raise ValueError(
                    "Use TCP ports or ascending ranges between 1 and 65535"
                )
            if int(ends[0]) > int(ends[-1]):
                raise ValueError("Port ranges must be ascending")
        return value


class Security(Record):
    operator_auth_enabled: bool = True
    # Kept for pre-v4 config migration. New authorization decisions are made
    # by Route.require_caller_key, never by this global compatibility field.
    client_auth_enabled: bool = True
    operator_networks: list[str] = Field(
        default_factory=lambda: ["127.0.0.1/32", "::1/128"]
    )
    anonymous_client_id: str | None = None
    session_hours: int = Field(default=168, ge=1, le=720)

    @field_validator("operator_networks")
    @classmethod
    def valid_networks(cls, values: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(value, strict=False)) for value in values]


class Configuration(Record):
    schema_version: Literal[4] = 4
    # Installation provenance for Settings copy; never changes access or routing.
    upgraded_from_schema: int | None = Field(default=None, ge=0, lt=4)
    security: Security = Field(default_factory=Security)
    revision: int = 0
    engines: list[Engine] = Field(default_factory=list)
    routes: list[Route] = Field(
        default_factory=lambda: [
            Route(name="auto", purpose="Use an available local model")
        ]
    )
    clients: list[Client] = Field(default_factory=list)
    discovery: Discovery = Field(default_factory=Discovery)

    def validate_endpoint_changes(self, previous: Configuration) -> None:
        previous_engines = {engine.id: engine for engine in previous.engines}
        for engine in self.engines:
            old = previous_engines.get(engine.id)
            changed = set(engine.endpoint_urls) - set(old.endpoint_urls if old else [])
            if old is None or engine.base_url != old.base_url:
                changed.add(engine.base_url)
            for url in changed:
                Engine.validate_connection_url(url)

    @model_validator(mode="after")
    def unique_records(self):
        for records in [self.engines, self.routes, self.clients]:
            if len({r.id for r in records}) != len(records):
                raise ValueError("Record IDs must be unique")
        if self.security.anonymous_client_id and not any(
            c.id == self.security.anonymous_client_id for c in self.clients
        ):
            raise ValueError(
                "Choose an existing client for shared access before removing its current policy"
            )
        if len({r.name for r in self.routes}) != len(self.routes):
            raise ValueError("Route names must be unique")
        urls = [url for engine in self.engines for url in engine.endpoint_urls]
        if len(set(urls)) != len(urls):
            raise ValueError(
                "An endpoint URL already belongs to another engine. Merge the engines explicitly."
            )
        return self
