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
        if request.method == "GET" and str(request.url) == "http://192.0.2.10:51234/health":
            return httpx.Response(404)
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
    def view(name, base_url, model_id="generic-chat", display_name=None, aliases=None):
        return {
            "id": name,
            "name": display_name or name,
            "name_source": "discovered",
            "base_url": base_url,
            "aliases": aliases or [],
            "models": [
                {
                    "id": model_id,
                    "capabilities": ["text"],
                    "context_length": None,
                    "enabled": True,
                }
            ],
        }

    # A hostname is not proof of which machine it is. Without resolver evidence
    # the two records cannot be shown to be one box, so nothing is claimed.
    assert (
        suggestions(
            [
                view(
                    "IP endpoint",
                    "http://192.0.2.10:51234/v1",
                    display_name="provider.test",
                ),
                view(
                    "DNS endpoint",
                    "http://provider.test:51234/v1",
                    display_name="provider.test",
                ),
            ]
        )
        == []
    )

    # With resolver evidence they are one machine: same address and port, the
    # path differs only by the API root.
    found = suggestions(
        [
            view(
                "IP endpoint",
                "http://192.0.2.10:51234/v1",
                display_name="provider.test",
            ),
            view(
                "DNS endpoint",
                "http://provider.test:51234/v1",
                display_name="provider.test",
            ),
        ],
        resolve={"provider.test": "192.0.2.10"},
    )
    assert len(found) == 1
    assert found[0]["kind"] == "duplicate"
    assert found[0]["endpoints"] == ["192.0.2.10:51234"]

    assert (
        suggestions(
            [
                view("One", "http://192.0.2.10:51234/v1"),
                view("Other port", "http://192.0.2.10:51235/v1"),
            ]
        )
        == []
    )


def test_same_machine_written_two_ways_is_one_duplicate():
    """The live failure: the operator engine and the scan disagree only on /v1."""

    def view(name, base_url, name_source="operator"):
        return {
            "id": name,
            "name": name,
            "name_source": name_source,
            "base_url": base_url,
            "aliases": [],
            "models": [
                {
                    "id": "DeepSeek-V4.1-Flash-TF",
                    "capabilities": ["text", "streaming"],
                    "context_length": None,
                    "enabled": True,
                }
            ],
        }

    found = suggestions(
        [
            view("Spark 1 (currently 2 spark host)", "http://192.0.2.91:8888/v1"),
            view("192.0.2.91", "http://192.0.2.91:8888", "discovered"),
        ]
    )
    assert len(found) == 1
    assert found[0]["kind"] == "duplicate"
    assert found[0]["endpoints"] == ["192.0.2.91:8888"]
    assert "same discovered catalog" in found[0]["reasons"]


def test_two_machines_on_the_same_port_are_not_one_machine():
    """TP=2: only the head serves, but a second box on :8888 must stay separate."""

    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": [],
            "models": [
                {
                    "id": "DeepSeek-V4.1-Flash-TF",
                    "capabilities": ["text"],
                    "context_length": None,
                    "enabled": True,
                }
            ],
        }

    assert (
        suggestions(
            [
                view("spark-host", "http://192.0.2.91:8888/v1"),
                view("spark-19cd", "http://192.0.2.33:8888/v1"),
            ]
        )
        == []
    )


def test_a_display_label_is_not_a_hostname():
    """Two machines may share a label. That proves nothing and must not merge."""

    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": [],
            "models": [],
        }

    # Same typed label, two different machines in one subnet: clean, not a merge.
    assert (
        suggestions(
            [
                view("Spark 1", "http://192.0.2.91:8888/v1"),
                view("Spark 1", "http://192.0.2.92:8888/v1"),
            ]
        )
        == []
    )

    # The live label is a sentence, and it must never act as an identity either.
    assert (
        suggestions(
            [
                view("Spark 1 (currently 2 spark host)", "http://192.0.2.91:8888/v1"),
                view("Spark 1 (currently 2 spark host)", "http://192.0.2.92:8888/v1"),
            ]
        )
        == []
    )


def test_one_machine_several_addresses_merges_by_hostname():
    def view(name, base_url, hostname=None, aliases=None):
        return {
            "id": name,
            "name": name,
            "hostname": hostname,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": aliases or [],
            "models": [],
        }

    # Wired and WiFi on one box: different addresses, same hostname field. Merge.
    wired = suggestions(
        [
            view("spark1-wired", "http://192.0.2.91:8888/v1", "spark1"),
            view("spark1-wifi", "http://192.0.2.92:8888/v1", "spark1"),
        ]
    )
    assert len(wired) == 1
    assert wired[0]["kind"] == "merge"

    # Same box on LAN and tailnet: different subnets, same hostname. Merge.
    transports = suggestions(
        [
            view("spark1-lan", "http://192.0.2.91:8888/v1", "spark1"),
            view("spark1-tail", "http://100.74.202.23:8888/v1", "spark1"),
        ]
    )
    assert len(transports) == 1
    assert transports[0]["kind"] == "merge"

    # Discovery writes the host name as an alias URL. That is hostname evidence too.
    discovered = suggestions(
        [
            view("a", "http://192.0.2.91:8888/v1", aliases=["http://spark1:8888/v1"]),
            view("b", "http://192.0.2.92:8888/v1", aliases=["http://spark1:8888/v1"]),
        ]
    )
    assert len(discovered) == 1
    assert discovered[0]["kind"] == "merge"


def test_one_machine_without_a_name_needs_an_operator_pin():
    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": ["Spark1"],
            "models": [],
        }

    pair = [
        view("wired", "http://192.0.2.91:8888/v1"),
        view("wifi", "http://192.0.2.92:8888/v1"),
    ]
    # No hostname resolves and no pin: the shared alias in one subnet is a name
    # error, which is the honest reading. Two machines cannot share a name.
    unpinned = suggestions(pair)
    assert len(unpinned) == 1
    assert unpinned[0]["kind"] == "name_error"

    pinned = suggestions(
        pair, pins={"192.0.2.91": "spark1", "192.0.2.92": "spark1"}
    )
    assert len(pinned) == 1
    assert pinned[0]["kind"] == "merge"


def test_an_operator_may_pin_by_hostname():
    """The override must work in the form the operator naturally reaches for."""

    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": [],
            "models": [],
        }

    pair = [
        view("wired", "http://192.0.2.91:8888/v1"),
        view("wifi", "http://192.0.2.92:8888/v1"),
    ]
    found = suggestions(
        pair,
        resolve={"spark1": "192.0.2.91"},
        pins={"spark1": "box", "192.0.2.92": "box"},
    )
    assert len(found) == 1
    assert found[0]["kind"] == "merge"


def test_shared_alias_on_two_machines_in_one_subnet_is_a_name_error():
    def view(name, base_url, hostname):
        return {
            "id": name,
            "name": name,
            "hostname": hostname,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": ["Spark"],
            "models": [],
        }

    found = suggestions(
        [
            view("a", "http://192.0.2.91:8888/v1", "spark-host"),
            view("b", "http://192.0.2.33:8888/v1", "spark-19cd"),
        ]
    )
    assert len(found) == 1
    assert found[0]["kind"] == "name_error"

    # Distinct names never conflict, whatever the addresses are.
    assert (
        suggestions(
            [
                {
                    **view("a", "http://192.0.2.91:8888/v1", "spark-host"),
                    "aliases": ["Spark1"],
                },
                {
                    **view("b", "http://192.0.2.33:8888/v1", "spark-19cd"),
                    "aliases": ["Spark2"],
                },
            ]
        )
        == []
    )


def test_a_stored_url_with_a_bad_port_does_not_crash_the_console():
    """An old config may hold an endpoint an earlier version accepted."""

    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "name_source": "operator",
            "base_url": base_url,
            "aliases": [],
            "models": [],
        }

    assert (
        suggestions(
            [
                view("bad", "http://192.0.2.91:99999/v1"),
                view("good", "http://192.0.2.92:8888/v1"),
            ]
        )
        == []
    )


def test_discovery_recognises_an_engine_written_with_a_different_api_root():
    """A scan must not append a second record for a machine already configured."""
    from gateway.discovery import _existing_engine
    from gateway.schema import Configuration, Engine

    config = Configuration(
        engines=[
            Engine(
                id="a" * 32,
                name="Spark 1",
                base_url="http://192.0.2.91:8888/v1",
                name_source="operator",
            )
        ]
    )
    assert _existing_engine(config, "http://192.0.2.91:8888").id == "a" * 32
    assert _existing_engine(config, "http://192.0.2.91:8888/v1").id == "a" * 32
    assert _existing_engine(config, "http://192.0.2.33:8888/v1") is None
    assert _existing_engine(config, "http://192.0.2.91:8889/v1") is None
    # A hostname cannot prove which machine it is, so it never matches by name.
    assert _existing_engine(config, "http://spark-host:8888/v1") is None
