"""Discovery acceptance exercises network listeners, not a pre-filled engine registry."""

import json
import time

import httpx
import pytest
from fastapi import FastAPI
from test_transport import serving

from gateway.app import create_app
from gateway.network.collector import Collector
from gateway.network.protocols import inspect_service
from gateway.network.report import read_report
from gateway.schema import Client, Configuration, Discovery


@pytest.mark.asyncio
async def test_program_discovers_an_arbitrary_fixture_port_and_follows_model_replacement(
    tmp_path,
):
    model_server = FastAPI()
    hosted = {"id": "new-model-not-in-any-config"}

    @model_server.get("/v1/models")
    async def catalog():
        return {"data": [{"id": hosted["id"], "owned_by": "fixture-serving-runtime"}]}

    async with serving(model_server) as origin:
        port = int(origin.rsplit(":", 1)[1])
        gateway = create_app(str(tmp_path), background=False)
        gateway.state.store.save(
            Configuration(
                clients=[Client(name="Local test", source_networks=["127.0.0.1/32"])],
                discovery=Discovery(
                    enabled=True,
                    auto_register=True,
                    http_ports=[port],
                    targets=["127.0.0.1"],
                    port_range=f"{port - 2}-{port + 2}",
                    mdns=False,
                ),
            )
        )

        discovery_root = gateway.state.store.discovery_directory
        collector = Collector(discovery_root)
        assert not collector.report["hosts"]
        await collector.run(once=True)
        report = read_report(discovery_root)
        service = report["hosts"][0]["services"][0]
        assert service["base_url"] == origin + "/v1"
        assert service["models"][0]["id"] == hosted["id"]
        assert report["hosts"][0]["scan_complete"]
        assert not gateway.state.store.config().engines
        await gateway.state.discovery.refresh()
        engines = gateway.state.store.config().engines
        assert len(engines) == 1 and engines[0].base_url == origin + "/v1"
        hosted["id"] = "replaced-during-serving"
        await gateway.state.discovery.refresh()
        assert gateway.state.discovery.views()[0]["models"][0]["id"] == hosted["id"]
        await _close_app(gateway)


async def _close_app(app):
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_native_catalog_capabilities_and_non_chat_surfaces_do_not_become_chat_models():
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"model": "arbitrary-vector-model"},
                        {"model": "arbitrary-visual-model"},
                    ]
                },
            )
        if path == "/api/ps":
            return httpx.Response(
                200, json={"models": [{"model": "arbitrary-visual-model"}]}
            )
        if path == "/api/show":
            model = json.loads(request.content)["model"]
            return httpx.Response(
                200,
                json={
                    "capabilities": ["embedding"]
                    if "vector" in model
                    else ["completion", "vision"]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await inspect_service(http, "http://runtime.test:49191")
    assert result["protocol"] == "ollama"
    assert result["models"][0]["capabilities"] == ["embeddings"]
    assert result["models"][1]["capabilities"] == ["text", "streaming", "vision"]
    assert result["models"][1]["loaded"]
    assert all(
        path
        in {
            "/api/tags",
            "/api/ps",
            "/api/show",
            "/v1/models",
            "/models",
            "/openapi.json",
        }
        for _, path in calls
    )

    async def speech(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200, json={"paths": {"/v1/audio/speech": {"post": {}}}}
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "arbitrary-voice"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(speech)) as http:
        result = await inspect_service(http, "http://speech.test:54321")
    assert result["capabilities"] == ["speech"]


@pytest.mark.asyncio
async def test_discovery_coverage_is_visible_before_scan_finishes(tmp_path):
    async def runner(*args, **kwargs):
        yield {
            "host": {
                "address": "127.0.0.9",
                "status": "up",
                "evidence": "tcp-response",
                "name": "",
                "ports": [],
                "filtered_ports": 65535,
            }
        }
        yield {"progress": 42, "task": "SYN Stealth Scan"}

    collector = Collector(tmp_path, runner=runner)
    collector.report = {
        "hosts": [],
        "packets_per_second": 100,
        "completed_at": 0,
        "error": "",
    }
    async with httpx.AsyncClient() as http:
        await collector.pass_scan(http, ["127.0.0.9"], "1-65535", "full_scan")
    report = read_report(tmp_path)
    assert report["phase"] == "full_scan" and report["progress"] == 42
    assert report["completed_at"] == 0
    assert report["hosts"][0]["filtered_ports"] == 65535


@pytest.mark.asyncio
async def test_scan_without_hardware_evidence_clears_prior_hardware_address(tmp_path):
    async def runner(*args, **kwargs):
        yield {
            "host": {
                "address": "192.0.2.9",
                "status": "up",
                "evidence": "TCP connection accepted",
                "name": "",
                "ports": [],
                "filtered_ports": 0,
            }
        }

    host = {
        "address": "192.0.2.9",
        "hardware_address": "00:11:22:33:44:55",
        "ports": [],
        "services": [],
    }
    collector = Collector(tmp_path, runner=runner)
    collector.report = {"hosts": [host], "packets_per_second": 100}
    async with httpx.AsyncClient() as http:
        await collector.pass_scan(http, [host["address"]], "1-65535", "full_scan")
    assert "hardware_address" not in host


@pytest.mark.asyncio
async def test_disabled_admission_keeps_catalog_observable_and_network_view_current(
    tmp_path,
):
    from gateway.network.report import publish
    from gateway.network.views import network_view
    from gateway.schema import Engine

    model_server = FastAPI()
    hosted = {"id": "observed-before-change"}

    @model_server.get("/v1/models")
    async def catalog():
        return {"data": [hosted]}

    async with serving(model_server) as origin:
        app = create_app(str(tmp_path), background=False)
        engine = Engine(
            name="Observed endpoint", base_url=origin + "/v1", enabled=False
        )
        app.state.store.save(Configuration(engines=[engine]))
        publish(
            tmp_path,
            {
                "phase": "full_scan",
                "updated_at": time.time(),
                "completed_at": 0,
                "hosts": [
                    {
                        "services": [
                            {
                                "base_url": engine.base_url,
                                "checked_at": 1,
                                "models": [{"id": "old-sweep-result"}],
                            }
                        ]
                    }
                ],
            },
        )
        await app.state.discovery.refresh()
        views = app.state.discovery.views()
        assert views[0]["status"] == "disabled"
        assert views[0]["models"][0]["id"] == hosted["id"]
        report = network_view(tmp_path, views)
        assert report["hosts"][0]["services"][0]["models"][0]["id"] == hosted["id"]
        assert report["phase"] == "full_scan" and report["completed_at"] == 0
        hosted["id"] = "changed-while-admission-disabled"
        await app.state.discovery.refresh()
        assert (
            network_view(tmp_path, app.state.discovery.views())["hosts"][0]["services"][
                0
            ]["models"][0]["id"]
            == hosted["id"]
        )
        await _close_app(app)


@pytest.mark.asyncio
async def test_declared_serving_surface_exposes_health_model_without_inventing_chat_catalog():
    async def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/health": {"get": {}},
                        "/v1/audio/transcriptions": {"post": {}},
                    }
                },
            )
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"model": "observed-speech-model", "status": "ok"}
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await inspect_service(http, "http://speech.test:54321")
    assert result["models"] == [
        {"id": "observed-speech-model", "capabilities": ["transcription"]}
    ]
    assert result["status"] == "model_surface"
    assert result["base_url"] == ""


@pytest.mark.asyncio
async def test_collector_shutdown_cancels_active_scan_and_manual_scan_works_when_paused(
    tmp_path, monkeypatch
):
    import asyncio

    from gateway.network import collector as module

    policy = {"enabled": False, "targets": []}
    monkeypatch.setattr(module, "read_policy", lambda root: policy)
    entered, released = asyncio.Event(), asyncio.Event()

    class ObservedCollector(Collector):
        async def collect(self, settings):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                released.set()

    from gateway.network.report import request_scan

    request_scan(tmp_path)
    task = asyncio.create_task(ObservedCollector(tmp_path).run(once=True))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert released.is_set()


@pytest.mark.asyncio
async def test_relay_provenance_is_visible_and_never_auto_registered(tmp_path):
    from gateway.network.protocols import is_gateway_catalog
    from gateway.schema import Engine

    rows = [
        {
            "id": "a-retired-backend",
            "owned_by": "arbitrary-owner",
            "backend_id": "upstream-1",
            "provenance": {
                "endpoint_identity": "private://upstream",
                "backend_id": "upstream-1",
                "health": {"status": "unhealthy", "code": "backend_unreachable"},
            },
        }
    ]
    declared = {"data": rows, "model_serving": {"version": 1, "kind": "router"}}
    assert is_gateway_catalog(declared)
    assert not is_gateway_catalog(
        {"data": [{"id": "alias", "owned_by": "local-proxy"}]}
    )

    async def handler(request):
        return (
            httpx.Response(200, json=declared)
            if request.url.path == "/v1/models"
            else httpx.Response(404)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await inspect_service(http, "http://relay.test:50123")
    assert result["status"] == "gateway"
    assert result["models"][0]["available"] is False
    assert result["models"][0]["capabilities"] == []
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    assert (
        await app.state.discovery.discover_url("http://relay.test:50123/v1", "network")
        is None
    )
    assert not app.state.store.config().engines
    # An operator may deliberately configure a relay with a separate policy.
    configured = await app.state.discovery.probe(
        Engine(
            name="Explicit provider",
            base_url="http://relay.test:50123/v1",
            source="manual",
        )
    )
    assert configured and not configured[0]["enabled"]
    await _close_app(app)


@pytest.mark.asyncio
async def test_native_runtime_missing_metadata_preserves_operator_capability_declaration(
    tmp_path,
):
    from gateway.schema import Engine

    async def handler(request):
        payload = {
            "/v1/models": {"data": [{"id": "unclassified-native-model"}]},
            "/api/tags": {"models": [{"model": "unclassified-native-model"}]},
            "/api/show": {"details": {}},
            "/api/ps": {"models": []},
        }.get(request.url.path)
        return httpx.Response(200, json=payload) if payload else httpx.Response(404)

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    engine = Engine(
        name="Declared vector service",
        base_url="http://native.test/v1",
        catalog_protocol="ollama",
        capabilities=["embeddings"],
    )
    models = await app.state.discovery.probe(engine)
    assert models[0]["capabilities"] == ["embeddings"]
    assert not models[0]["loaded"]
    await _close_app(app)


def test_anonymous_scan_cannot_replace_authenticated_catalog(tmp_path):
    from gateway.network.report import publish
    from gateway.network.views import network_view

    publish(
        tmp_path,
        {
            "phase": "complete",
            "hosts": [
                {
                    "services": [
                        {
                            "base_url": "http://engine.test/v1",
                            "status": "authentication_required",
                            "checked_at": 200,
                            "models": [],
                        }
                    ]
                }
            ],
        },
    )
    engine = {
        "id": "catalog-owner",
        "name": "Registered engine",
        "base_url": "http://engine.test/v1",
        "checked_at": 190,
        "models": [{"id": "authenticated-model"}],
        "error": "",
    }
    service = network_view(tmp_path, [engine])["hosts"][0]["services"][0]
    assert service["models"] == engine["models"]
    assert service["status"] == "model_service"


@pytest.mark.asyncio
async def test_live_catalog_response_survives_inconclusive_port_sweep(tmp_path):
    import time

    collector = Collector(tmp_path)
    host = {
        "address": "127.0.0.9",
        "status": "up",
        "ports": [53191],
        "services": [
            {
                "port": 53191,
                "status": "model_service",
                "models": [{"id": "live-model"}],
                "checked_at": 0,
                "catalog_tracked": True,
            }
        ],
    }

    async def runner(*args, **kwargs):
        host["services"][0]["checked_at"] = time.time()
        yield {
            "host": {
                "address": host["address"],
                "status": "up",
                "evidence": "user-set",
                "ports": [],
                "filtered_ports": 65535,
            }
        }

    collector.runner = runner
    collector.report = {"hosts": [host], "packets_per_second": 100}
    async with httpx.AsyncClient() as http:
        await collector.pass_scan(http, [host["address"]], "1-65535", "full_scan")
    assert host["ports"] == [53191]
    assert host["services"][0]["models"][0]["id"] == "live-model"
