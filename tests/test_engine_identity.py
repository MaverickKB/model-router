import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from gateway.app import create_app
from gateway.engine_identity import MergeEngines
from gateway.engine_suggestions import suggestions
from gateway.proxy import InflightRequest
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    Route,
    Security,
    Selector,
)
from gateway.store import Conflict, Store
from gateway.topology import route_map


def configuration():
    primary = Engine(
        name="Named service",
        base_url="http://model.test:8000/v1",
        aliases=["http://192.0.2.2:8000/v1"],
    )
    duplicate = Engine(
        name="Other address",
        base_url="http://192.0.2.3:8000/v1",
        aliases=["http://127.0.0.1:9000/v1"],
    )
    caller = Client(
        name="Shared",
        kind="shared",
        engine_ids=[duplicate.id],
        source_networks=["127.0.0.1/32"],
        allow_network_auth=True,
    )
    route = Route(
        name="auto",
        primary=Selector(engine_ids=[duplicate.id]),
        fallback=Selector(engine_ids=[primary.id, duplicate.id]),
    )
    config = Configuration(
        engines=[primary, duplicate],
        clients=[caller],
        routes=[route],
        security=Security(operator_auth_enabled=False, client_auth_enabled=False),
    )
    return config, primary, duplicate, caller


@pytest.mark.asyncio
async def test_merge_relinks_policy_preserves_keys_and_alias_rediscovery(
    tmp_path, monkeypatch
):
    calls = []

    def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "chat-model"}]})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    store, discovery = app.state.store, app.state.discovery
    config, target, source, caller = configuration()
    store.save(config)
    store.set_secret(source.id, "selected-provider-key")
    store.set_secret(target.id, "old-target-key")
    key = store.issue_key(caller.id)

    async def trusted(_hostname):
        return True

    monkeypatch.setattr(discovery, "trusted_host", trusted)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        ) as client,
    ):
        response = await client.post(
            "/api/v1/engines/merge",
            json={
                "revision": store.config().revision,
                "source_id": source.id,
                "target_id": target.id,
                "preferred_url": source.base_url,
                "credential_source": "source",
            },
        )
        assert response.status_code == 200, response.text
        saved = store.config()
        assert len(saved.engines) == 1
        assert saved.engines[0].id == target.id
        assert saved.engines[0].base_url == source.base_url
        assert set(saved.engines[0].endpoint_urls) == set(
            source.endpoint_urls + target.endpoint_urls
        )
        assert saved.routes[0].primary.engine_ids == [target.id]
        assert saved.routes[0].fallback.engine_ids == [target.id]
        assert saved.clients[0].engine_ids == [target.id]
        assert saved.clients[0].kind == "shared"
        assert saved.security == config.security
        assert store.key_client(key) == caller.id
        assert store.secret(target.id) == "selected-provider-key"
        assert not store.secret(source.id)
        before = len(calls)
        for url in saved.engines[0].endpoint_urls:
            assert await discovery.discover_url(url) == target.id
        assert len(calls) == before
        assert len(store.config().engines) == 1
    restarted = Store(str(tmp_path))
    assert restarted.config().engines[0].aliases == saved.engines[0].aliases
    assert restarted.secret(target.id) == "selected-provider-key"
    assert restarted.key_client(key) == caller.id


def test_merge_conflict_does_not_remove_source_and_urls_have_one_owner(tmp_path):
    store = Store(str(tmp_path))
    config, target, source, _ = configuration()
    saved = store.save(config)
    store.set_secret(source.id, "keep-this")
    request = MergeEngines(
        revision=0,
        source_id=source.id,
        target_id=target.id,
        preferred_url=target.base_url,
        credential_source="source",
    )
    with pytest.raises(Conflict):
        store.merge_engines(request)
    assert len(store.config().engines) == 2 and store.secret(source.id) == "keep-this"
    request.revision = saved.revision
    request.preferred_url = "http://unrelated.test/v1"
    with pytest.raises(ValueError, match="preferred URL"):
        store.merge_engines(request)
    with pytest.raises(ValidationError, match="already belongs"):
        Configuration(
            engines=[target, Engine(name="Duplicate alias", base_url=target.aliases[0])]
        )


@pytest.mark.asyncio
async def test_active_request_blocks_merge_and_identity_lock_blocks_admission(tmp_path):
    app = create_app(str(tmp_path), background=False)
    config, target, source, _ = configuration()
    saved = app.state.store.save(config)
    observation = app.state.discovery.observation(source.id)
    claim = InflightRequest.claim(observation, 1, 15)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    ) as client:
        response = await client.post(
            "/api/v1/engines/merge",
            json={
                "revision": saved.revision,
                "source_id": source.id,
                "target_id": target.id,
                "preferred_url": target.base_url,
                "credential_source": "target",
            },
        )
        assert response.status_code == 409
        assert len(app.state.store.config().engines) == 2
    await claim.release()
    observation.identity_locked = True
    assert InflightRequest.claim(observation, 1, 15) is None
    observation.identity_locked = False
    claim = InflightRequest.claim(observation, 1, 15)
    assert claim is not None
    await claim.release()


def test_map_uses_caller_permissions_and_kind_is_only_a_label(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(
        name="Provider",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    caller = Client(name="Caller", route_names=["auto"])
    config = Configuration(
        engines=[engine],
        clients=[caller],
        routes=[Route(name="auto", primary=Selector(kind="cloud"))],
    )
    app.state.store.save(config)
    obs = app.state.discovery.observation(engine.id)
    obs.status = "available"
    obs.models = [
        {
            "id": "chat",
            "capabilities": ["text"],
            "context_length": None,
            "enabled": True,
        }
    ]
    for kind in [None, "shared", "agent", "machine", "person"]:
        caller.kind = kind
        topology = route_map(
            config,
            app.state.discovery.views(),
            [{"id": "observed", "policy_id": caller.id, "identity_basis": "api_key"}],
        )
        assert topology["caller_routes"][0]["ready_engines"] == []
        assert topology["route_engines"][0]["ready"]
    caller.allow_cloud = True
    assert route_map(
        config,
        app.state.discovery.views(),
        [{"id": "observed", "policy_id": caller.id, "identity_basis": "api_key"}],
    )["caller_routes"][0]["ready_engines"] == [engine.id]


def test_map_emits_one_engine_edge_per_route_not_per_policy(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(name="Local provider", base_url="http://local.test/v1")
    route = Route(name="auto", primary=Selector(kind="local"))
    config = Configuration(
        engines=[engine],
        routes=[route],
        clients=[
            Client(name="First policy", route_names=[route.name]),
            Client(name="Second policy", route_names=[route.name]),
        ],
    )
    app.state.store.save(config)
    observation = app.state.discovery.observation(engine.id)
    observation.status = "available"
    observation.models = [
        {
            "id": "generic-chat",
            "capabilities": ["text"],
            "context_length": None,
            "enabled": True,
        }
    ]

    topology = route_map(config, app.state.discovery.views(), [])

    assert topology["route_engines"] == [
        {
            "route_id": route.id,
            "engine_id": engine.id,
            "tier": "primary",
            "dynamic": True,
            "models": ["generic-chat"],
            "ready": True,
        }
    ]


def test_failed_merge_rolls_back_policy_credentials_and_caches(tmp_path):
    store = Store(str(tmp_path))
    config, target, source, caller = configuration()
    saved = store.save(config)
    store.set_secret(source.id, "source-key")
    store.set_secret(target.id, "target-key")
    key = store.issue_key(caller.id)
    store.db.execute(
        "CREATE TRIGGER reject_merge BEFORE UPDATE ON config "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    request = MergeEngines(
        revision=saved.revision,
        source_id=source.id,
        target_id=target.id,
        preferred_url=source.base_url,
        credential_source="source",
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
        store.merge_engines(request)
    assert store.config() == saved
    assert store.secret(source.id) == "source-key"
    assert store.secret(target.id) == "target-key"
    assert store.key_client(key) == caller.id
    assert not store.db.in_transaction
    # Remove the injected failure before the startup normalization writes.
    store.db.execute("DROP TRIGGER reject_merge")
    restarted = Store(str(tmp_path))
    assert restarted.config() == saved
    assert restarted.secret(source.id) == "source-key"
    assert restarted.secret(target.id) == "target-key"
    assert restarted.key_client(key) == caller.id
    merged = restarted.merge_engines(request)
    assert len(merged.engines) == 1
    assert restarted.secret(target.id) == "source-key"


def test_map_does_not_advertise_speech_as_a_text_route(tmp_path):
    app = create_app(str(tmp_path), background=False)
    engine = Engine(name="Speech API", base_url="http://speech.test/v1")
    config = Configuration(engines=[engine])
    app.state.store.save(config)
    observation = app.state.discovery.observation(engine.id)
    observation.status = "available"
    observation.models = [
        {
            "id": "voice",
            "capabilities": ["speech"],
            "enabled": True,
            "context_length": None,
        }
    ]
    assert route_map(config, app.state.discovery.views(), [])["route_engines"] == []


@pytest.mark.asyncio
async def test_network_discovery_keeps_ip_transport_and_records_reported_hostname(
    tmp_path, monkeypatch
):
    def upstream(request):
        assert str(request.url) == "http://192.0.2.10:51234/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "generic-chat", "capabilities": ["text"]}]},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    app.state.store.save(
        Configuration(
            discovery=Discovery(
                enabled=True,
                auto_register=True,
                targets=["192.0.2.10"],
            )
        )
    )

    async def trusted(_hostname):
        return True

    monkeypatch.setattr(app.state.discovery, "trusted_host", trusted)
    (app.state.store.discovery_directory / "network.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "address": "192.0.2.10",
                        "name": "provider.test",
                        "services": [
                            {
                                "status": "model_service",
                                "base_url": "http://192.0.2.10:51234/v1",
                                "port": 51234,
                                "models": ["generic-chat"],
                                "capabilities": ["text"],
                                "protocol": "openai",
                            }
                        ],
                    }
                ]
            }
        )
    )
    await app.state.discovery.consume_network()
    engine = app.state.store.config().engines[0]
    assert engine.base_url == "http://192.0.2.10:51234/v1"
    assert engine.name == "provider.test"
    assert engine.name_source == "discovered"
    assert engine.aliases == ["http://provider.test:51234/v1"]
    await app.state.discovery.consume_network()
    assert len(app.state.store.config().engines) == 1


@pytest.mark.asyncio
async def test_network_discovery_does_not_auto_register_native_or_conflicting_catalogs(
    tmp_path,
):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(
        Configuration(
            discovery=Discovery(
                enabled=True,
                auto_register=True,
                targets=["192.0.2.10"],
            )
        )
    )
    (app.state.store.discovery_directory / "network.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "address": "192.0.2.10",
                        "services": [
                            {
                                "status": "model_service",
                                "protocol": "ollama",
                                "base_url": "http://192.0.2.10:51234/v1",
                                "models": [{"id": "native-only"}],
                            },
                            {
                                "status": "model_service",
                                "protocol": "openai",
                                "catalog_conflict": True,
                                "base_url": "http://192.0.2.10:51235/v1",
                                "models": [{"id": "conflicting"}],
                            }
                        ],
                    }
                ]
            }
        )
    )
    await app.state.discovery.consume_network()
    assert not app.state.store.config().engines


def test_duplicate_suggestions_require_endpoint_evidence():
    def view(name, base_url, model_id="generic-chat", display_name=None):
        return {
            "id": name,
            "name": display_name or name,
            "name_source": "discovered",
            "base_url": base_url,
            "aliases": [],
            "models": [
                {
                    "id": model_id,
                    "capabilities": ["text"],
                    "context_length": None,
                    "enabled": True,
                }
            ],
        }

    assert suggestions(
        [
            view(
                "IP endpoint", "http://192.0.2.10:51234/v1", display_name="provider.test"
            ),
            view(
                "DNS endpoint", "http://provider.test:51234/v1", display_name="provider.test"
            ),
        ]
    )[0]["reasons"] == [
        "same API path and port",
        "same discovered catalog",
        "same discovered host name",
    ]
    assert (
        suggestions(
            [
                view("One", "http://192.0.2.10:51234/v1"),
                view("Other port", "http://192.0.2.10:51235/v1"),
            ]
        )
        == []
    )
