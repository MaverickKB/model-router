"""Exact OpenAI completion operation contracts stay intact through routing."""

import httpx
import pytest

from gateway.app import create_app
from gateway.migration import configuration as migrate_configuration
from gateway.network.openapi import inspect_openapi
from gateway.network.protocols import inspect_services
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    Route,
    Security,
    Selector,
)


def completion_operation(
    *,
    input_name="messages",
    input_type="array",
    request_valid=True,
    response_valid=True,
    security=None,
):
    properties = {
        "model": {"type": "string" if request_valid else "integer"},
        input_name: {"type": input_type},
    }
    response_properties = {
        "choices": {"type": "array" if response_valid else "object"},
    }
    operation = {
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {"type": "object", "properties": properties}
                }
            }
        },
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": response_properties,
                        }
                    }
                }
            }
        },
    }
    if security is not None:
        operation["security"] = security
    return {"post": operation}


def test_pre_endpoint_contract_engine_migrates_to_both_operations():
    raw = Configuration(
        engines=[Engine(name="Existing engine", base_url="http://api.test/v1")]
    ).model_dump()
    raw["schema_version"] = 4
    raw["engines"][0].pop("completion_paths")

    migrated = migrate_configuration(raw)
    assert migrated["schema_version"] == 5
    assert migrated["engines"][0]["completion_paths"] == [
        "/chat/completions",
        "/completions",
    ]
    assert Configuration.model_validate(migrated).engines[0].completion_paths == [
        "/chat/completions",
        "/completions",
    ]


@pytest.mark.asyncio
async def test_legacy_completion_skips_a_chat_only_engine_and_uses_supported_fallback(
    tmp_path,
):
    calls = []

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        calls.append((request.url.host, request.url.path))
        return httpx.Response(200, json={"choices": []})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    chat = Engine(
        name="Chat only",
        base_url="http://chat.test/v1",
        completion_paths=["/chat/completions"],
    )
    legacy = Engine(
        name="Legacy only",
        base_url="http://legacy.test/v1",
        completion_paths=["/completions"],
    )
    caller = Client(name="Caller", route_names=["auto"])
    app.state.store.save(
        Configuration(
            engines=[chat, legacy],
            clients=[caller],
            routes=[
                Route(
                    name="auto",
                    primary=Selector(engine_ids=[chat.id]),
                    fallback=Selector(engine_ids=[legacy.id]),
                )
            ],
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        ) as client,
    ):
        response = await client.post(
            "/v1/completions",
            json={"model": "auto", "prompt": "hello"},
        )
    assert response.status_code == 200
    assert calls == [("legacy.test", "/v1/completions")]


@pytest.mark.asyncio
async def test_unsupported_completion_operation_returns_clear_400_without_forwarding(
    tmp_path,
):
    calls = []

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": []})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Chat only",
        base_url="http://chat.test/v1",
        completion_paths=["/chat/completions"],
    )
    caller = Client(name="Caller", route_names=["auto"])
    app.state.store.save(
        Configuration(
            engines=[engine],
            clients=[caller],
            routes=[Route(name="auto", primary=Selector(engine_ids=[engine.id]))],
        )
    )
    key = app.state.store.issue_key(caller.id)
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        ) as client,
    ):
        response = await client.post(
            "/v1/completions",
            json={"model": "auto", "prompt": "hello"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_operation"
    assert calls == []


def test_openapi_keeps_public_and_protected_operations_separate():
    document = {
        "security": [{"token": []}],
        "servers": [
            {
                "url": "/{basePath}",
                "variables": {"basePath": {"default": "v1"}},
            }
        ],
        "paths": {
            "/chat/completions": completion_operation(),
            "/completions": completion_operation(
                input_name="prompt",
                input_type="string",
                security=[],
            ),
        },
    }
    evidence = inspect_openapi(document)
    assert len(evidence.bases) == 1
    base = evidence.bases[0]
    assert base.base_path == "/v1"
    assert base.completion_paths == ["/completions"]
    assert base.protected_completion_paths == ["/chat/completions"]


def test_openapi_anonymous_security_alternative_keeps_completion_public():
    document = {
        "security": [{"bearer": []}],
        "paths": {
            "/chat/completions": completion_operation(
                security=[{}, {"bearer": []}]
            )
        },
    }
    evidence = inspect_openapi(document)
    base = evidence.bases[0]
    assert base.completion_paths == ["/chat/completions"]
    assert base.protected_completion_paths == []


def test_openapi_keeps_static_model_ids_with_their_operation_access_boundary():
    public = completion_operation()
    public["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["model"] = {"type": "string", "enum": ["public-chat"]}
    protected = completion_operation(input_name="prompt", input_type="string")
    protected["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["model"] = {"type": "string", "enum": ["private-legacy"]}
    protected["post"]["security"] = [{"bearer": []}]
    evidence = inspect_openapi(
        {
            "paths": {
                "/chat/completions": public,
                "/completions": protected,
            }
        }
    )
    base = evidence.bases[0]
    assert base.completion_paths == ["/chat/completions"]
    assert base.model_ids == ["public-chat"]
    assert base.protected_completion_paths == ["/completions"]
    assert base.protected_model_ids == ["private-legacy"]


@pytest.mark.parametrize(
    ("server_url", "document_path", "expected_base"),
    [
        ("v1", "/openapi.json", "/v1"),
        ("./v1", "/openapi.json", "/v1"),
        (".", "/v1/openapi.json", "/v1"),
        ("./", "/v1/openapi.json", "/v1"),
    ],
)
def test_openapi_relative_servers_resolve_against_the_fetched_document(
    server_url, document_path, expected_base
):
    document = {
        "servers": [{"url": server_url}],
        "paths": {"/chat/completions": completion_operation()},
    }
    evidence = inspect_openapi(document, document_path=document_path)
    assert evidence.bases[0].base_path == expected_base
    assert evidence.bases[0].completion_paths == ["/chat/completions"]


def test_openapi_same_origin_absolute_server_is_an_exact_api_base():
    evidence = inspect_openapi(
        {
            "servers": [{"url": "http://provider.test/v1"}],
            "paths": {"/chat/completions": completion_operation()},
        },
        origin="http://provider.test",
    )
    assert evidence.bases[0].base_path == "/v1"
    assert evidence.bases[0].completion_paths == ["/chat/completions"]


@pytest.mark.parametrize(
    "server",
    [
        {"url": "https://other.example/v1"},
        {"url": "http://other.example/v1"},
        {"url": "../v1"},
        {"url": "/{basePath}", "variables": {"basePath": {"default": "../v1"}}},
        {"url": "/{basePath}", "variables": {}},
    ],
)
def test_openapi_server_resolution_rejects_external_or_traversing_bases(server):
    document = {
        "servers": [server],
        "paths": {"/chat/completions": completion_operation()},
    }
    assert inspect_openapi(document, origin="http://provider.test").bases == []


@pytest.mark.asyncio
async def test_prefixed_openapi_document_is_probed_from_a_strict_catalog_once():
    origin = "http://provider.test"
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "."}],
                    "paths": {"/chat/completions": completion_operation()},
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)
    service = next(item for item in services if item["status"] == "model_service")
    assert service["base_url"] == origin + "/v1"
    assert service["completion_paths"] == ["/chat/completions"]
    assert service["registration_eligible"] is True
    assert calls.count("/openapi.json") == 1
    assert calls.count("/v1/openapi.json") == 1


@pytest.mark.asyncio
async def test_separate_openapi_documents_cannot_combine_partial_contracts():
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "v1"}],
                    "paths": {
                        "/chat/completions": completion_operation(
                            response_valid=False
                        )
                    },
                },
            )
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "."}],
                    "paths": {
                        "/chat/completions": completion_operation(
                            request_valid=False
                        )
                    },
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, "http://provider.test")
    catalog = next(service for service in services if service["protocol"] == "openai")
    assert catalog["status"] == "http_service"
    assert catalog["registration_eligible"] is False
    assert catalog["completion_paths"] == []


@pytest.mark.asyncio
async def test_separate_openapi_documents_union_independently_complete_operations():
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "v1"}],
                    "paths": {"/chat/completions": completion_operation()},
                },
            )
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "."}],
                    "paths": {
                        "/completions": completion_operation(
                            input_name="prompt", input_type="string"
                        )
                    },
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, "http://provider.test")
    service = next(item for item in services if item["status"] == "model_service")
    assert service["completion_paths"] == ["/chat/completions", "/completions"]


@pytest.mark.asyncio
async def test_conflicting_public_and_protected_operation_stays_manual_review_only(
    tmp_path,
):
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "v1"}],
                    "paths": {"/chat/completions": completion_operation()},
                },
            )
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "."}],
                    "paths": {
                        "/chat/completions": completion_operation(
                            security=[{"bearer": []}]
                        )
                    },
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, "http://provider.test")
    service = next(item for item in services if item["protocol"] == "openai")
    assert service["status"] == "authentication_required"
    assert service["completion_paths"] == ["/chat/completions"]
    assert service["registration_eligible"] is False

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    app.state.store.save(
        Configuration(
            discovery=Discovery(
                targets=["provider.test", "127.0.0.1"], auto_register=True
            ),
            security=Security(operator_auth_enabled=False),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4312)),
            base_url="http://router.test",
        ) as client,
    ):
        response = await client.post(
            "/v1/gateway/register", json={"base_url": "http://provider.test/v1"}
        )
    assert response.status_code == 202
    assert not app.state.store.config().engines


@pytest.mark.asyncio
async def test_registration_persists_only_the_completion_operation_it_proved(tmp_path):
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(
                200,
                json={
                    "servers": [{"url": "."}],
                    "paths": {"/chat/completions": completion_operation()},
                },
            )
        return httpx.Response(404)

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    app.state.store.save(
        Configuration(
            discovery=Discovery(
                targets=["provider.test", "127.0.0.1"], auto_register=True
            ),
            security=Security(operator_auth_enabled=False),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4312)),
            base_url="http://router.test",
        ) as client,
    ):
        response = await client.post(
            "/v1/gateway/register", json={"base_url": "http://provider.test/v1"}
        )
    assert response.status_code == 200
    engine = app.state.store.config().engines[0]
    assert engine.base_url == "http://provider.test/v1"
    assert engine.completion_paths == ["/chat/completions"]


@pytest.mark.asyncio
async def test_registration_keeps_catalog_only_endpoint_pending_for_manual_review(tmp_path):
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        return httpx.Response(404)

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    app.state.store.save(
        Configuration(
            discovery=Discovery(
                targets=["provider.test", "127.0.0.1"], auto_register=True
            ),
            security=Security(operator_auth_enabled=False),
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4312)),
            base_url="http://router.test",
        ) as client,
    ):
        response = await client.post(
            "/v1/gateway/register", json={"base_url": "http://provider.test/v1"}
        )
    assert response.status_code == 202
    assert not app.state.store.config().engines
    pending = app.state.discovery.pending_views()
    assert pending[0]["base_url"] == "http://provider.test/v1"
    assert pending[0]["completion_paths"] == []
