"""Operator-declared model inventories remain distinct from live catalogs."""

import json
import time

import httpx
import pytest
from pydantic import ValidationError

from gateway.app import create_app
from gateway.network.report import publish
from gateway.routing import decide
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    ModelSettings,
    Route,
    declared_inventory_signature,
)
from gateway.topology import route_map


def record_declared_success(app, engine, succeeded_at):
    token = app.state.store.declared_inventory_token(engine)
    assert token is not None
    receipt = app.state.store.record_declared_inventory_success(token, succeeded_at)
    assert receipt is not None
    return receipt


def test_declared_inventory_requires_normalized_model_ids():
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=[" configured-model ", "configured-model", "other-model"],
    )
    assert engine.declared_models == ["configured-model", "other-model"]

    with pytest.raises(ValidationError, match="at least one model ID"):
        Engine(
            name="Missing inventory",
            base_url="http://provider.test/v1",
            model_inventory_source="declared",
        )
    with pytest.raises(ValidationError, match="require a declared model inventory"):
        Engine(
            name="Mixed provenance",
            base_url="http://provider.test/v1",
            declared_models=["configured-model"],
        )
    with pytest.raises(ValidationError, match="cannot contain control characters"):
        Engine(
            name="Invalid ID",
            base_url="http://provider.test/v1",
            model_inventory_source="declared",
            declared_models=["configured\x00model"],
        )
    with pytest.raises(ValidationError, match="must be a list"):
        Engine(
            name="Wrong model collection",
            base_url="http://provider.test/v1",
            model_inventory_source="declared",
            declared_models="configured-model",
        )


@pytest.mark.parametrize(
    "model_id", ["model*", "model?", "model[one", "model]one"]
)
def test_declared_model_ids_are_literals_not_selectors(model_id):
    with pytest.raises(ValidationError, match="exact names, not patterns"):
        Engine(
            name="Configured endpoint",
            base_url="http://provider.test/v1",
            model_inventory_source="declared",
            declared_models=[model_id],
        )


async def test_declared_inventory_is_selectable_before_a_success_and_healthy_afterward(
    tmp_path,
):
    upstream_calls = []

    async def upstream(request):
        if request.url.path.endswith("/models"):
            pytest.fail("A declared inventory must not be represented as a catalog probe")
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        upstream_calls.append(body)
        return httpx.Response(200, json={"model": body["model"], "choices": []})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    saved = app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        await app.state.discovery.refresh_engine(engine)
        view = app.state.discovery.views()[0]
        assert view["status"] == "configured"
        assert view["models"] == [
            {
                "id": "configured-model",
                "capabilities": ["text", "streaming"],
                "context_length": None,
                "enabled": True,
            }
        ]
        decision = decide(
            saved,
            app.state.discovery.views(),
            caller,
            {"model": "auto", "messages": []},
        )
        assert [candidate["model"] for candidate in decision["candidates"]] == [
            "configured-model"
        ]
        assert route_map(saved, app.state.discovery.views(), [])["route_engines"][0][
            "ready"
        ]
        assert (await client.get("/health")).json()["ok"] is False
        advertised = (await client.get("/v1/models")).json()["data"]
        assert {model["id"] for model in advertised} == {"auto", "configured-model"}

        response = await client.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert response.status_code == 200
        assert upstream_calls == [{"model": "configured-model", "messages": []}]
        assert app.state.discovery.views()[0]["status"] == "available"
        assert (await client.get("/health")).json()["ok"] is True


async def test_declared_inventory_success_proof_survives_restart(tmp_path):
    calls = []

    async def upstream(request):
        if request.url.path.endswith("/models"):
            pytest.fail("Declared inventory must never issue a catalog probe")
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"model": "configured-model", "choices": []},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        response = await client.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert response.status_code == 200
        assert app.state.discovery.views()[0]["status"] == "available"
    assert calls == ["/v1/chat/completions"]
    app.state.store.db.close()

    async def unexpected_upstream(request):
        pytest.fail(f"Restart restore unexpectedly contacted {request.url}")

    restarted = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(unexpected_upstream),
    )
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted),
            base_url="http://router.test",
        ) as client,
    ):
        restored = restarted.state.discovery.views()[0]
        assert restored["status"] == "available"
        assert restored["last_success"] is not None
        assert (await client.get("/health")).json()["ok"] is True


@pytest.mark.parametrize("failure", ["http", "transport"])
async def test_declared_success_proof_is_cleared_after_upstream_failure(
    tmp_path, failure
):
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"model": "configured-model", "choices": []},
            )
        if failure == "http":
            return httpx.Response(503, json={"error": {"message": "busy"}})
        raise httpx.ConnectError("provider unavailable", request=request)

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        assert (
            await client.post(
                "/v1/chat/completions", json={"model": "auto", "messages": []}
            )
        ).status_code == 200
        assert app.state.discovery.views()[0]["status"] == "available"
        assert (
            await client.post(
                "/v1/chat/completions", json={"model": "auto", "messages": []}
            )
        ).status_code == 503
        assert app.state.discovery.views()[0]["status"] == "unavailable"
        assert app.state.store.declared_inventory_success(
            engine.id, declared_inventory_signature(engine)
        ) is None
    app.state.store.db.close()

    restarted = create_app(str(tmp_path), background=False)
    restored = restarted.state.discovery.views()[0]
    assert restored["status"] == "configured"
    assert restored["last_success"] is None


async def test_declared_inventory_stream_completion_persists_success_proof(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            pytest.fail("Declared inventory must never issue a catalog probe")
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            content=(
                b'data: {"model":"configured-model","choices":[]}\n\n'
                b"data: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [], "stream": True},
        )
        assert response.status_code == 200
        assert "data: [DONE]" in response.text
        assert app.state.discovery.views()[0]["status"] == "available"
    app.state.store.db.close()

    restarted = create_app(str(tmp_path), background=False)
    assert restarted.state.discovery.views()[0]["status"] == "available"


async def test_incomplete_declared_stream_does_not_persist_success_proof(tmp_path):
    async def upstream(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [], "stream": True},
        )
        assert response.status_code == 200
        assert "Upstream stream ended before completion" in response.text
        assert app.state.discovery.views()[0]["status"] == "unavailable"
        assert app.state.store.declared_inventory_success(
            engine.id, declared_inventory_signature(engine)
        ) is None
    app.state.store.db.close()

    restarted = create_app(str(tmp_path), background=False)
    restored = restarted.state.discovery.views()[0]
    assert restored["status"] == "configured"
    assert restored["last_success"] is None


async def test_done_only_declared_stream_revokes_prior_response_proof(tmp_path):
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"model": "configured-model", "choices": []},
            )
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        proved = await client.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert proved.status_code == 200
        assert app.state.discovery.views()[0]["status"] == "available"

        streamed = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [], "stream": True},
        )
        # [DONE] is still a successful client stream termination. It is not
        # enough evidence to retain a declared engine's response proof.
        assert streamed.status_code == 200
        assert streamed.content == b"data: [DONE]\n\n"
        assert app.state.discovery.views()[0]["status"] == "unavailable"
        assert app.state.store.declared_inventory_success(
            engine.id, declared_inventory_signature(engine)
        ) is None


async def test_interrupted_declared_stream_revokes_prior_response_proof(tmp_path):
    calls = 0

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"model":"configured-model","choices":[]}\n\n'
            raise httpx.ReadError("provider stream interrupted")

    async def upstream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"model": "configured-model", "choices": []},
            )
        return httpx.Response(
            200,
            stream=InterruptedStream(),
            headers={"content-type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", allow_direct_models=True)
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    caller_key = app.state.store.issue_key(caller.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {caller_key}"},
        ) as client,
    ):
        proved = await client.post(
            "/v1/chat/completions", json={"model": "auto", "messages": []}
        )
        assert proved.status_code == 200
        assert app.state.discovery.views()[0]["status"] == "available"

        interrupted = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [], "stream": True},
        )
        assert interrupted.status_code == 200
        assert b"Upstream stream interrupted" in interrupted.content
        assert app.state.discovery.views()[0]["status"] == "unavailable"
        assert app.state.store.declared_inventory_success(
            engine.id, declared_inventory_signature(engine)
        ) is None
    app.state.store.db.close()

    restarted = create_app(str(tmp_path), background=False)
    restored = restarted.state.discovery.views()[0]
    assert restored["status"] == "configured"
    assert restored["last_success"] is None


async def test_stale_declared_success_is_selectable_but_not_healthy(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller")
    saved = app.state.store.save(
        Configuration(
            engines=[engine],
            clients=[caller],
            routes=[Route(name="auto")],
            discovery=Discovery(stale_seconds=5),
        )
    )
    record_declared_success(app, engine, time.time() - 6)

    views = app.state.discovery.views()
    assert views[0]["status"] == "configured"
    assert views[0]["last_success"] is not None
    decision = decide(saved, views, caller, {"model": "auto", "messages": []})
    assert [candidate["model"] for candidate in decision["candidates"]] == [
        "configured-model"
    ]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test"
        ) as client,
    ):
        assert (await client.get("/health")).json()["ok"] is False


async def test_declared_inventory_proof_is_pruned_for_changed_or_deleted_inventory(
    tmp_path,
):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
        model_patterns=["configured-model"],
    )
    app.state.store.save(Configuration(engines=[engine]))
    previous_signature = declared_inventory_signature(engine)
    record_declared_success(app, engine, time.time())

    changed = app.state.store.config()
    changed.engines[0] = changed.engines[0].model_copy(
        update={
            "declared_models": ["replacement-model"],
            "model_patterns": ["replacement-model"],
        }
    )
    app.state.store.save(changed)
    assert app.state.store.declared_inventory_success(
        engine.id, previous_signature
    ) is None

    restarted = create_app(str(tmp_path), background=False)
    view = restarted.state.discovery.views()[0]
    assert view["models"][0]["id"] == "replacement-model"
    assert view["status"] == "configured"
    assert view["last_success"] is None

    current_engine = restarted.state.store.config().engines[0]
    current_signature = declared_inventory_signature(current_engine)
    record_declared_success(restarted, current_engine, time.time())
    deleted = restarted.state.store.config()
    deleted.engines = []
    restarted.state.store.save(deleted)
    assert restarted.state.store.declared_inventory_success(
        engine.id, current_signature
    ) is None


def test_declared_inventory_applies_operator_model_settings_and_patterns(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["selected-model", "filtered-model"],
        model_patterns=["selected-*"],
        model_settings={
            "selected-model": ModelSettings(
                capabilities=["text", "tools"], context_length=4096
            ),
            "filtered-model": ModelSettings(enabled=False),
        },
    )
    app.state.store.save(Configuration(engines=[engine]))

    view = app.state.discovery.views()[0]
    assert view["status"] == "configured"
    assert view["models"] == [
        {
            "id": "selected-model",
            "capabilities": ["text", "tools"],
            "context_length": 4096,
            "enabled": True,
        },
        {
            "id": "filtered-model",
            "capabilities": ["text", "streaming"],
            "context_length": None,
            "enabled": False,
        },
    ]


def test_declared_inventory_does_not_bypass_the_route_key_gate(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(
        name="Configured endpoint",
        base_url="http://provider.test/v1",
        model_inventory_source="declared",
        declared_models=["configured-model"],
    )
    caller = Client(name="Caller", route_names=["private"])
    config = Configuration(
        engines=[engine],
        clients=[caller],
        routes=[Route(name="private", require_caller_key=True)],
    )
    app.state.store.save(config)
    views = app.state.discovery.views()

    denied = decide(
        config, views, caller, {"model": "private"}, caller_key_present=False
    )
    assert denied["status"] == 401
    allowed = decide(
        config, views, caller, {"model": "private"}, caller_key_present=True
    )
    assert [candidate["model"] for candidate in allowed["candidates"]] == [
        "configured-model"
    ]


async def test_network_admission_requires_explicit_or_legacy_catalog_evidence(
    tmp_path, monkeypatch
):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(Configuration(discovery=Discovery(enabled=True)))
    publish(
        app.state.store.discovery_directory,
        {
            "hosts": [
                {
                    "name": "provider.test",
                    "services": [
                        {
                            "base_url": "http://not-a-catalog.test/v1",
                            "status": "model_surface",
                            "models": [],
                            "registration_eligible": False,
                        },
                        {
                            "base_url": "http://legacy-catalog.test/v1",
                            "status": "model_service",
                            "models": [{"id": "legacy-model"}],
                            "registration_eligible": True,
                            "completion_paths": ["/chat/completions", "/completions"],
                        },
                        {
                            "base_url": "http://verified-singleton.test/v1",
                            "status": "model_service",
                            "models": [{"id": "verified-model"}],
                            "registration_eligible": True,
                            "completion_paths": ["/chat/completions"],
                        },
                    ],
                }
            ]
        },
    )
    discovered = []

    async def record(url, *args, **kwargs):
        discovered.append((url, args, kwargs))

    monkeypatch.setattr(app.state.discovery, "discover_url", record)
    await app.state.discovery.consume_network()
    assert [url for url, _, _ in discovered] == [
        "http://legacy-catalog.test/v1",
        "http://verified-singleton.test/v1",
    ]
