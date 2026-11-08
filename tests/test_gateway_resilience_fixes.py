"""Regressions for discovery claims, suggestion identity and unkeyed policy."""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from gateway.discovery import DiscoveryService, _machine
from gateway.engine_identity import MergeEngines, merged_configuration
from gateway.engine_suggestions import suggestions
from gateway.request_policy import resolve_unkeyed_policy
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    Route,
    Security,
    Selector,
)
from gateway.store import Store


def test_catalog_declarations_not_invented_for_host_following_engines(tmp_path):
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(404)
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "plain-model"}]})

    engine = Engine(name="Host follower", base_url="http://host.test/v1")
    config = Configuration(
        engines=[engine],
        routes=[Route(name="auto", primary=Selector(hosts=["host.test"]))],
    )
    store = Store(tmp_path)
    store.save(config)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    discovery = DiscoveryService(store, http)

    assert discovery.follows_host(engine) is True
    models = asyncio.run(discovery.probe(engine))

    assert models[0]["capabilities"] == ["text", "streaming"]


def test_machine_returns_none_for_an_unparsable_port_on_an_ip_literal():
    assert _machine("http://192.0.2.5:notaport/v1") is None


def test_scan_error_does_not_end_the_refresh_loop(tmp_path):
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(404)
        assert request.url.path == "/v1/models"
        refreshed.set()
        return httpx.Response(200, json={"data": [{"id": "model"}]})

    engine = Engine(name="Healthy", base_url="http://healthy.test/v1")
    config = Configuration(
        engines=[engine], discovery=Discovery(enabled=False, refresh_seconds=2)
    )
    store = Store(tmp_path)
    store.save(config)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    discovery = DiscoveryService(store, http)
    refreshed = asyncio.Event()
    calls = 0
    original = discovery.consume_network

    async def flaky_consume_network():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("malformed discovery report")
        await original()

    discovery.consume_network = flaky_consume_network

    async def run_until_refreshed():
        task = asyncio.create_task(discovery.run())
        try:
            await asyncio.wait_for(refreshed.wait(), 5)
            assert not task.done()
            assert discovery.scan_error == "malformed discovery report"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_until_refreshed())


def test_bad_cidr_entries_are_skipped_for_unkeyed_policy():
    default = Client(name="Default", route_names=["default"])
    default.source_networks = ["not-a-network", "192.0.2.10/32"]
    config = Configuration(
        clients=[default],
        security=Security(anonymous_client_id=default.id),
    )

    matched, basis = resolve_unkeyed_policy(config, "192.0.2.10")
    assert matched.id == default.id
    assert basis == "shared_access"

    outside, _ = resolve_unkeyed_policy(config, "198.51.100.10")
    assert outside.name == "Unkeyed connection"


def test_all_invalid_source_networks_are_rejected():
    with pytest.raises(ValidationError, match="no usable network"):
        Client(name="Default", source_networks=["not-a-network", "also-bad"])


def test_one_valid_source_network_survives_beside_invalid_entries():
    client = Client(
        name="Default",
        source_networks=["192.0.2.10/32", "not-a-network"],
    )

    assert client.source_networks == ["192.0.2.10/32"]


def test_client_source_networks_drop_unparsable_entries():
    client = Client(
        name="Default",
        source_networks=["not-a-network", "127.0.0.1/32", "127.0.0.1/32"],
    )

    assert client.source_networks == ["127.0.0.1/32"]


def test_merge_unions_tags_and_relinks_members():
    target = Engine(name="Target", base_url="http://target.test/v1")
    target.tags = ["shared"]
    target.members = ["kept-member"]
    source = Engine(name="Source", base_url="http://source.test/v1")
    source.tags = ["extra", "shared"]
    source.members = [source.id, "extra-member"]
    other = Engine(name="Other", base_url="http://other.test/v1")
    other.members = [source.id]
    config = Configuration(engines=[target, source, other])
    merge = MergeEngines(
        revision=config.revision,
        source_id=source.id,
        target_id=target.id,
        preferred_url=target.base_url,
        credential_source="target",
    )

    merged = merged_configuration(config, merge)

    kept = next(engine for engine in merged.engines if engine.id == target.id)
    assert kept.tags == ["shared", "extra"]
    assert kept.members == ["kept-member", "extra-member"]
    assert next(engine for engine in merged.engines if engine.id == other.id).members == [
        target.id
    ]


def test_same_hostname_and_address_does_not_suggest_a_merge():
    def view(name, base_url):
        return {
            "id": name,
            "name": name,
            "hostname": "spark1",
            "name_source": "operator",
            "base_url": base_url,
            "aliases": [],
            "models": [],
        }

    distinct_services = suggestions(
        [
            view("vllm", "http://192.0.2.91:8000/v1"),
            view("ollama", "http://192.0.2.91:11434/v1"),
        ]
    )
    assert distinct_services == []

    several_interfaces = suggestions(
        [
            view("wired", "http://192.0.2.91:8000/v1"),
            view("wifi", "http://192.0.2.92:8000/v1"),
        ]
    )
    assert len(several_interfaces) == 1
    assert several_interfaces[0]["kind"] == "merge"
