"""Endpoint mistakes stay local to their engine across configuration and refresh."""

import asyncio

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Configuration, Engine
from gateway.store import Store


def write_legacy_configuration(directory, config):
    """Reproduce a configuration accepted before connection-port validation."""
    store = Store(directory)
    with store.db:
        store.db.execute(
            "UPDATE config SET body=? WHERE id=1", (config.model_dump_json(),)
        )
    store.db.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.test/v1",
        "https://provider.test/v1",
        "http://provider.test:80/v1",
        "https://provider.test:443/v1",
        "http://[::1]/v1",
        "http://[2001:db8::1]:8080/v1",
    ],
)
def test_valid_endpoint_ports_and_ipv6_remain_configurable(tmp_path, url):
    store = Store(tmp_path)
    saved = store.save(Configuration(engines=[Engine(name="Provider", base_url=url)]))
    assert saved.engines[0].base_url == url


@pytest.mark.parametrize("port", ["notaport", "65536", "0"])
@pytest.mark.parametrize("field", ["base_url", "aliases"])
def test_invalid_new_endpoint_is_rejected_without_saving(tmp_path, port, field):
    store = Store(tmp_path)
    config = store.config()
    engine = Engine(name="Provider", base_url="http://provider.test/v1")
    invalid = f"http://provider.test:{port}/v1"
    if field == "base_url":
        engine.base_url = invalid
    else:
        engine.aliases = [invalid]
    config.engines.append(engine)
    before = store.config().model_dump()
    with pytest.raises(ValueError, match="valid TCP port"):
        store.save(config)
    assert store.config().model_dump() == before
    assert Store(tmp_path).config().model_dump() == before


def test_historic_invalid_endpoint_loads_and_can_be_repaired(tmp_path):
    engine = Engine(name="Provider", base_url="http://provider.test:notaport/v1")
    write_legacy_configuration(tmp_path, Configuration(engines=[engine]))
    store = Store(tmp_path)
    config = store.config()
    assert config.engines[0].base_url == engine.base_url
    config.routes[0].purpose = "Updated route description"
    saved = store.save(config)
    assert saved.engines[0].base_url == engine.base_url
    assert Store(tmp_path).config().routes[0].purpose == "Updated route description"
    saved.engines[0].base_url = "http://provider.test:9000/v1"
    repaired = store.save(saved)
    assert repaired.engines[0].base_url == "http://provider.test:9000/v1"


async def test_bad_and_unreachable_catalogs_do_not_stop_neighbor_refresh(tmp_path):
    healthy = Engine(name="Healthy", base_url="http://healthy.test/v1")
    malformed = Engine(name="Malformed", base_url="http://malformed.test:notaport/v1")
    unreachable = Engine(name="Unreachable", base_url="http://unreachable.test/v1")
    config = Configuration(engines=[healthy, malformed, unreachable])
    config.discovery.refresh_seconds = 2
    write_legacy_configuration(tmp_path, config)
    refresh_count = 0
    refreshed_twice = asyncio.Event()

    async def handler(request):
        nonlocal refresh_count
        if request.url.host == "unreachable.test":
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.host == "healthy.test"
        refresh_count += 1
        if refresh_count >= 2:
            refreshed_twice.set()
        return httpx.Response(200, json={"data": [{"id": f"model-{refresh_count}"}]})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    async with app.router.lifespan_context(app):
        refresh_task = asyncio.create_task(app.state.discovery.run())
        try:
            await asyncio.wait_for(refreshed_twice.wait(), 5)
            async with asyncio.timeout(1):
                while (
                    app.state.discovery.observation(healthy.id).models[0]["id"]
                    != "model-2"
                ):
                    await asyncio.sleep(0)
            assert not refresh_task.done()
            views = {engine["id"]: engine for engine in app.state.discovery.views()}
            assert views[healthy.id]["status"] == "available"
            assert views[malformed.id]["status"] == "offline"
            assert views[malformed.id]["error"] == "Endpoint URL is invalid"
            assert views[unreachable.id]["status"] == "offline"
        finally:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)


async def test_invalid_discovered_endpoint_does_not_escape_into_refresher(tmp_path):
    async def handler(request):
        pytest.fail("An invalid transport URL must fail before HTTP is sent")

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    async with app.router.lifespan_context(app):
        assert (
            await app.state.discovery.discover_url(
                "http://provider.test:notaport/v1",
                source="manual",
                hostname="provider.test",
            )
            is None
        )
        assert app.state.store.config().engines == []
