"""Protocol evidence tests for catalog, completion, and native inventory discovery."""

import httpx
import pytest

from gateway.adapters.openai import read_catalog
from gateway.network.protocols import inspect_service, inspect_services
from gateway.network.report import publish
from gateway.network.views import network_view


def completion_operation(
    path="/v1/chat/completions",
    *,
    model_schema=None,
    input_schema=None,
    choices_schema=None,
    response_status="200",
):
    """A minimum documented contract the router can actually forward."""
    is_chat = path.endswith("/chat/completions")
    input_name = "messages" if is_chat else "prompt"
    model_schema = model_schema if model_schema is not None else {"type": "string"}
    input_schema = input_schema if input_schema is not None else {
        "type": "array" if is_chat else "string"
    }
    choices_schema = (
        choices_schema if choices_schema is not None else {"type": "array"}
    )
    return {
        "post": {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "model": model_schema,
                                input_name: input_schema,
                            },
                        }
                    }
                }
            },
            "responses": {
                response_status: {
                    "description": "OpenAI-compatible completion response",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"choices": choices_schema},
                            }
                        }
                    },
                }
            },
        }
    }


def completion_spec(path="/v1/chat/completions", model_schema=None, **kwargs):
    return {
        "openapi": "3.1.0",
        "paths": {
            path: completion_operation(path, model_schema=model_schema, **kwargs)
        },
    }


@pytest.mark.asyncio
async def test_strict_singleton_catalog_is_verified_and_adapter_can_reprobe_it():
    origin = "http://fixture.test:8711"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec())
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"id": "one-model", "object": "model"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)
        catalog = await read_catalog(http, origin + "/v1", {})

    assert service["status"] == "model_service"
    assert service["base_url"] == origin + "/v1"
    assert service["registration_eligible"] is True
    assert [row["id"] for row in service["models"]] == ["one-model"]
    assert catalog == {"data": [{"id": "one-model", "object": "model"}]}


@pytest.mark.asyncio
async def test_generic_id_document_is_not_accepted_as_a_singleton_catalog():
    async def handler(request):
        return httpx.Response(200, json={"id": "looks-like-a-model"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="readable model catalog"):
            await read_catalog(http, "http://fixture.test:8717/v1", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"enum": ["alpha", "beta"]}, ["alpha", "beta"]),
        ({"const": "fixed-model"}, ["fixed-model"]),
    ],
)
async def test_schema_declared_model_ids_are_manual_completion_surface_evidence(
    schema, expected
):
    origin = "http://fixture.test:8712"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec("/api/v2/chat/completions", schema))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_surface"
    assert service["compatible_base_url"] == origin + "/api/v2"
    assert service["registration_eligible"] is False
    assert service["needs_model_identity"] is False
    assert [row["id"] for row in service["models"]] == expected
    assert all(set(attempt) == {"path", "status"} for attempt in service["catalog_attempts"])


@pytest.mark.asyncio
async def test_free_form_openapi_values_never_become_discovered_model_ids():
    origin = "http://fixture.test:8713"
    schema = {
        "type": "string",
        "default": "not-an-inventory",
        "examples": ["also-not-an-inventory"],
        "description": "This completion service accepts a model.",
    }

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec(model_schema=schema))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_surface"
    assert service["models"] == []
    assert service["needs_model_identity"] is True
    assert service["compatible_base_url"] == origin + "/v1"


@pytest.mark.asyncio
async def test_local_openapi_refs_are_bounded_and_schema_enum_is_explicit_inventory():
    origin = "http://fixture.test:8718"
    spec = {
        "paths": {
            "/v1/chat/completions": {
                "post": {
                    "requestBody": {"$ref": "#/components/requestBodies/Completion"},
                    "responses": {"200": {"$ref": "#/components/responses/Completion"}},
                }
            }
        },
        "components": {
            "requestBodies": {
                "Completion": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Completion"}
                        }
                    }
                }
            },
            "schemas": {
                "Completion": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {
                                "model": {"$ref": "#/components/schemas/ModelId"}
                            },
                        },
                        {
                            "type": "object",
                            "properties": {"messages": {"type": "array"}},
                        },
                    ]
                },
                "ModelId": {"enum": ["ref-model"]},
                "CompletionResponse": {
                    "type": "object",
                    "properties": {"choices": {"type": "array"}},
                },
            },
            "responses": {
                "Completion": {
                    "description": "OpenAI-compatible completion response",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CompletionResponse"}
                        }
                    },
                }
            },
        },
    }

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_surface"
    assert [row["id"] for row in service["models"]] == ["ref-model"]


@pytest.mark.asyncio
async def test_root_completion_path_never_invents_a_v1_base_url():
    origin = "http://fixture.test:8719"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec("/chat/completions"))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_surface"
    assert service["compatible_base_url"] == origin
    assert service["registration_eligible"] is False


@pytest.mark.asyncio
async def test_root_and_v1_catalogs_are_preserved_as_independent_same_origin_bases():
    origin = "http://fixture.test:8730"
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/chat/completions": completion_operation("/chat/completions"),
            "/models": {"get": {}},
            "/v1/chat/completions": completion_operation(),
            "/v1/models": {"get": {}},
        },
    }

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "root-model"}]})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "v1-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    by_base = {service["base_url"]: service for service in services}
    assert set(by_base) == {origin, origin + "/v1"}
    assert by_base[origin]["models"][0]["id"] == "root-model"
    assert by_base[origin + "/v1"]["models"][0]["id"] == "v1-model"
    assert all(service["registration_eligible"] for service in by_base.values())


@pytest.mark.asyncio
async def test_relative_openapi_server_prefix_is_applied_without_crossing_origin():
    origin = "http://fixture.test:8731"
    spec = completion_spec("/chat/completions")
    spec["servers"] = [{"url": "/api/v3"}]
    spec["paths"]["/models"] = {"get": {}}

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        if request.url.path == "/api/v3/models":
            return httpx.Response(200, json={"data": [{"id": "server-base-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_service"
    assert service["base_url"] == origin + "/api/v3"
    assert service["models"][0]["id"] == "server-base-model"


@pytest.mark.asyncio
async def test_external_openapi_server_cannot_pair_a_local_catalog_with_completion():
    origin = "http://fixture.test:8732"
    spec = completion_spec("/chat/completions")
    spec["servers"] = [{"url": "https://outside.example/v1"}]
    spec["paths"]["/models"] = {"get": {}}

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "local-only"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    assert not any(service["registration_eligible"] for service in services)
    catalog = next(service for service in services if service["models"])
    assert catalog["status"] == "http_service"
    assert catalog["compatible_base_url"] == ""


@pytest.mark.asyncio
async def test_trailing_models_path_is_probed_and_deduplicated_to_its_api_base():
    origin = "http://fixture.test:8733"
    spec = completion_spec()
    spec["paths"]["/v1/models/"] = {"get": {}}
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        if request.url.path in {"/v1/models", "/v1/models/"}:
            return httpx.Response(200, json={"data": [{"id": "trailing-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    matching = [
        service for service in services if service["surface_id"] == origin + "/v1"
    ]
    assert requests.count("/v1/models/") == 1
    assert len(matching) == 1
    assert matching[0]["base_url"] == origin + "/v1"
    assert matching[0]["models"][0]["id"] == "trailing-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_schema": {"type": "integer"}},
        {"input_schema": {"type": "string"}},
        {"choices_schema": {"type": "string"}},
    ],
)
async def test_wrong_openapi_transport_types_never_admit_a_catalog(kwargs):
    origin = "http://fixture.test:8734"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec(**kwargs))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "not-routeable"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    assert not any(service["registration_eligible"] for service in services)
    assert any(service["status"] == "http_service" for service in services)


@pytest.mark.asyncio
async def test_default_openapi_response_does_not_prove_a_successful_completion():
    origin = "http://fixture.test:8735"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json=completion_spec(response_status="default")
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "default-only"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    assert not any(service["registration_eligible"] for service in services)
    assert any(service["status"] == "http_service" for service in services)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_path", "catalog_path"),
    [
        ("/v1//chat/completions", "/v1//models"),
        ("/v1/../chat/completions", "/v1/../models"),
    ],
)
async def test_noncanonical_openapi_paths_cannot_prove_a_routeable_base(
    completion_path, catalog_path
):
    origin = "http://fixture.test:8736"
    spec = completion_spec(completion_path)
    spec["paths"][catalog_path] = {"get": {}}

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        if request.url.path in {catalog_path, "/models"}:
            return httpx.Response(200, json={"data": [{"id": "noncanonical"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    assert not any(service["registration_eligible"] for service in services)


@pytest.mark.asyncio
async def test_catalog_and_completion_at_different_bases_remain_separate_evidence():
    origin = "http://fixture.test:8723"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json=completion_spec("/api/v2/chat/completions")
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "listed-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        services = await inspect_services(http, origin)

    by_id = {service["surface_id"]: service for service in services}
    catalog = by_id[origin + "/v1"]
    completion = by_id[origin + "/api/v2"]
    assert catalog["status"] == "http_service"
    assert catalog["compatible_base_url"] == ""
    assert catalog["registration_eligible"] is False
    assert [row["id"] for row in catalog["models"]] == ["listed-model"]
    assert completion["status"] == "model_surface"
    assert completion["compatible_base_url"] == origin + "/api/v2"
    assert completion["needs_model_identity"] is True


@pytest.mark.asyncio
async def test_catalog_authentication_precedes_openapi_completion_classification():
    origin = "http://fixture.test:8714"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec())
        if request.url.path == "/v1/models":
            return httpx.Response(401, json={"detail": "key required"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "authentication_required"
    assert service["models"] == []
    assert service["compatible_base_url"] == origin + "/v1"
    assert {attempt["path"]: attempt["status"] for attempt in service["catalog_attempts"]}["/v1/models"] == 401


@pytest.mark.asyncio
async def test_native_inventory_requires_a_completion_contract_at_the_same_base():
    origin = "http://fixture.test:8715"

    async def native_only(request):
        if request.url.path == "/models":
            return httpx.Response(200, json={"models": [{"key": "native-a"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(native_only)) as http:
        inventory = await inspect_service(http, origin)
    assert inventory["status"] == "native_inventory"
    assert inventory["compatible_base_url"] == ""
    assert [row["id"] for row in inventory["models"]] == ["native-a"]

    async def compatible(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec())
        if request.url.path == "/models":
            return httpx.Response(200, json={"models": [{"id": "native-a"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(compatible)) as http:
        services = await inspect_services(http, origin)
    by_id = {service["surface_id"]: service for service in services}
    assert by_id[origin]["status"] == "native_inventory"
    assert [row["id"] for row in by_id[origin]["models"]] == ["native-a"]
    assert by_id[origin + "/v1"]["status"] == "model_surface"
    assert by_id[origin + "/v1"]["needs_model_identity"] is True
    assert by_id[origin + "/v1"]["registration_eligible"] is False


@pytest.mark.asyncio
async def test_native_string_inventory_is_manual_only_and_ignores_malformed_values():
    origin = "http://fixture.test:8740"

    async def handler(request):
        if request.url.path == "/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        " native-string ",
                        {"key": "native-record"},
                        "",
                        "\x00not-safe",
                        7,
                        {"id": "\x7fnot-safe"},
                        {"name": "not-an-explicit-id"},
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "native_inventory"
    assert service["protocol"] == "native"
    assert service["registration_eligible"] is False
    assert service["compatible_base_url"] == ""
    assert [row["id"] for row in service["models"]] == [
        "native-string",
        "native-record",
    ]


@pytest.mark.asyncio
async def test_native_v1_inventory_does_not_pair_with_a_different_completion_base():
    origin = "http://fixture.test:8720"

    async def inventory_only(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{"key": "native-v1"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(inventory_only)) as http:
        inventory = await inspect_service(http, origin)
    assert inventory["status"] == "native_inventory"
    assert [row["id"] for row in inventory["models"]] == ["native-v1"]
    attempts = {item["path"]: item["status"] for item in inventory["catalog_attempts"]}
    assert attempts["/api/v1/models"] == 200

    async def compatible(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=completion_spec())
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{"id": "native-v1"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(compatible)) as http:
        services = await inspect_services(http, origin)
    by_id = {service["surface_id"]: service for service in services}
    assert by_id[origin + "/api/v1"]["status"] == "native_inventory"
    assert [row["id"] for row in by_id[origin + "/api/v1"]["models"]] == [
        "native-v1"
    ]
    assert by_id[origin + "/v1"]["status"] == "model_surface"
    assert by_id[origin + "/v1"]["needs_model_identity"] is True


@pytest.mark.asyncio
async def test_nonstandard_openai_list_requires_matching_post_transport():
    """A list-shaped native endpoint cannot become an auto-registered engine."""
    origin = "http://fixture.test:8722"

    async def inventory_only(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "listed-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(inventory_only)) as http:
        inventory = await inspect_service(http, origin)

    assert inventory["status"] == "http_service"
    assert inventory["models"] == [
        {
            "id": "listed-model",
            "capabilities": [],
            "available": True,
        }
    ]
    assert inventory["compatible_base_url"] == ""
    assert inventory["registration_eligible"] is False

    async def compatible(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json=completion_spec("/api/v1/chat/completions")
            )
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "listed-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(compatible)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_service"
    assert service["base_url"] == origin + "/api/v1"
    assert service["registration_eligible"] is True


@pytest.mark.asyncio
async def test_later_routeable_catalog_beats_earlier_identity_only_catalog():
    origin = "http://fixture.test:8724"

    async def handler(request):
        if request.url.path == "/openapi.json":
            spec = completion_spec("/api/v1/chat/completions")
            spec["paths"]["/api/v1/models"] = {"get": {}}
            return httpx.Response(
                200,
                json=spec,
            )
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "identity-only"}]})
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "routeable-model"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    assert service["status"] == "model_service"
    assert service["base_url"] == origin + "/api/v1"
    assert service["registration_eligible"] is True
    assert [row["id"] for row in service["models"]] == ["routeable-model"]


@pytest.mark.asyncio
async def test_openapi_catalog_probe_paths_are_bounded():
    origin = "http://fixture.test:8725"
    spec = completion_spec()
    spec["paths"].update(
        {f"/custom-{index}/models": {"get": {}} for index in range(12)}
    )

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)

    attempts = {attempt["path"] for attempt in service["catalog_attempts"]}
    assert "/custom-7/models" in attempts
    assert "/custom-8/models" not in attempts
    assert service["catalog_paths_unprobed"] == 4


@pytest.mark.asyncio
async def test_openapi_title_is_evidence_not_the_suggested_engine_name():
    origin = "http://fixture.test:8721"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "info": {"title": "A document title is not an engine name"},
                    "paths": {},
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)
    assert service["name"] == "fixture.test"
    assert service["openapi_title"] == "A document title is not an engine name"


@pytest.mark.asyncio
async def test_get_only_openapi_operation_cannot_prove_completion_transport():
    origin = "http://fixture.test:8716"

    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json={"paths": {"/v1/chat/completions": {"get": {}}}}
            )
        if request.url.path == "/models":
            return httpx.Response(200, json={"models": [{"id": "native-a"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = await inspect_service(http, origin)
    assert service["status"] == "native_inventory"
    assert service["compatible_base_url"] == ""


def test_network_categories_keep_legacy_model_services_and_split_catalogs_from_surfaces(
    tmp_path,
):
    publish(
        tmp_path,
        {
            "hosts": [
                {
                    "address": "192.0.2.10",
                    "status": "up",
                    "services": [
                        {
                            "status": "model_service",
                            "models": [{"id": "catalog-model"}],
                        }
                    ],
                },
                {
                    "address": "192.0.2.11",
                    "status": "up",
                    "services": [{"status": "model_surface", "models": []}],
                },
                {
                    "address": "192.0.2.12",
                    "status": "up",
                    "services": [{"status": "native_inventory", "models": []}],
                },
                {
                    "address": "192.0.2.13",
                    "status": "up",
                    "services": [{"status": "gateway", "models": []}],
                },
                {
                    "address": "192.0.2.14",
                    "status": "up",
                    "services": [
                        {
                            "status": "authentication_required",
                            "compatible_base_url": "https://protected.example/v1",
                            "models": [],
                        }
                    ],
                },
            ]
        },
    )

    verified = network_view(tmp_path, [], category="Verified catalogs")
    completion = network_view(tmp_path, [], category="Completion endpoints")
    legacy = network_view(tmp_path, [], category="Model services")

    assert [host["address"] for host in verified["hosts"]] == [
        "192.0.2.10",
        "192.0.2.13",
    ]
    assert [host["address"] for host in completion["hosts"]] == [
        "192.0.2.11",
        "192.0.2.14",
    ]
    assert [host["address"] for host in legacy["hosts"]] == [
        "192.0.2.10",
        "192.0.2.11",
        "192.0.2.13",
    ]
    assert verified["counts"]["models"] == 1
    assert verified["counts"]["completion_endpoints"] == 2
