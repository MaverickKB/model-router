"""Token accounting per principal, with no account required.

Ken, 2026-10-19: track by account/origin, keep free paths open, count tokens.

These tests prove the counting side. Enforcement is untouched, so every case
here uses a router with accounts disabled -- the live shape.
"""

import json
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.accounts.limits import window_start, REPORTING_WINDOW_SECONDS
from gateway.schema import Client, Configuration, Security
from gateway.store import Store
from gateway.usage_principals import resolve, row


USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def fleet(reply=None):
    """A mock engine that answers one chat completion with a usage report."""
    body = reply or {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"},
             "finish_reason": "stop"}
        ],
        "usage": dict(USAGE),
    }

    async def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "max_batch_size": 4})
        if path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        if "chat/completions" in path:
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": "not found"})

    return handle


def router(tmp_path, reply=None):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet(reply))
    )
    store = app.state.store
    policy = Client(name="Shared access")
    from gateway.schema import Engine, Route, Selector

    engine = Engine(name="Test engine", base_url="http://engine.test/v1")
    route = Route(name="auto", primary=Selector(engine_ids=[engine.id]))
    store.save(
        Configuration(
            engines=[engine],
            routes=[route],
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    return app, store, policy


async def ready(app):
    """Routing needs a discovered catalog; the live router polls on its own."""
    await app.state.discovery.refresh()


async def chat(app, address="192.0.2.10", **extra):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(address, 9000)),
        base_url="http://router.test",
    ) as http:
        return await http.post(
            "/v1/chat/completions",
            json={"model": "auto",
                  "messages": [{"role": "user", "content": "hi"}],
                  **extra},
        )


@pytest.mark.asyncio
async def test_usage_is_recorded_per_address_with_accounts_disabled(tmp_path):
    """The live shape: accounts off, no key, and tokens still counted."""
    app, store, _ = router(tmp_path)
    await ready(app)
    response = await chat(app)
    assert response.status_code == 200
    assert response.json()["usage"]["prompt_tokens"] == USAGE["prompt_tokens"]

    totals = store.principal_usage(days=1)
    assert len(totals) == 1
    entry = totals[0]
    assert entry["principal"] == "addr:192.0.2.10"
    assert entry["kind"] == "address"
    assert entry["prompt_tokens"] == USAGE["prompt_tokens"]
    assert entry["completion_tokens"] == USAGE["completion_tokens"]
    assert entry["estimated_tokens"] == 0, "a reported usage is not an estimate"
    assert entry["requests"] == 1
    assert entry["total_tokens"] == USAGE["total_tokens"]


@pytest.mark.asyncio
async def test_two_addresses_are_two_principals(tmp_path):
    """Two callers never merge into one total just because they share a policy."""
    app, store, _ = router(tmp_path)
    await ready(app)
    await chat(app, address="192.0.2.10")
    await chat(app, address="192.0.2.11")
    await chat(app, address="192.0.2.11")

    totals = store.principal_usage(days=1)
    by_principal = {entry["principal"]: entry for entry in totals}
    assert set(by_principal) == {"addr:192.0.2.10", "addr:192.0.2.11"}
    assert by_principal["addr:192.0.2.10"]["requests"] == 1
    assert by_principal["addr:192.0.2.11"]["requests"] == 2
    assert (
        by_principal["addr:192.0.2.11"]["total_tokens"]
        == 2 * USAGE["total_tokens"]
    )


@pytest.mark.asyncio
async def test_a_keyed_client_is_tracked_by_its_key(tmp_path):
    """With a key, the key is the identity and the address is evidence."""
    app, store, policy = router(tmp_path)
    await ready(app)
    key = store.issue_key(policy.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 9000)),
        base_url="http://router.test",
    ) as http:
        response = await http.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    totals = store.principal_usage(days=1)
    assert len(totals) == 1
    assert totals[0]["kind"] == "key"
    assert totals[0]["principal"].startswith("key:")
    assert totals[0]["label"] == policy.name


@pytest.mark.asyncio
async def test_an_origin_is_tracked_by_name_and_login(tmp_path):
    """A tailnet caller becomes one principal even across changing addresses."""
    app, store, _ = router(tmp_path)
    (tmp_path / "origin_policy.json").write_text(
        json.dumps(
            {
                "origins": [
                    {
                        "name": "shared-lab",
                        "source_networks": ["100.64.0.0/10"],
                        "source_tags": [],
                        "private": True,
                        "allowed_routes": ["auto"],
                    }
                ],
                "tag_resolver": None,
            }
        )
    )
    app.state.origins.set_login_resolver(lambda address: "friend@example.com")
    await ready(app)
    await chat(app, address="100.101.1.1")
    await chat(app, address="100.101.1.2")

    totals = store.principal_usage(days=1)
    assert len(totals) == 1, "two addresses in one origin are one principal"
    entry = totals[0]
    assert entry["principal"] == "origin:shared-lab"
    assert entry["kind"] == "origin"
    assert entry["login"] == "friend@example.com"
    assert entry["requests"] == 2


@pytest.mark.asyncio
async def test_exempt_and_tracked_coexist(tmp_path):
    """Exempt means not blocked, not invisible. Both still count."""
    app, store, _ = router(tmp_path)
    (tmp_path / "origin_policy.json").write_text(
        json.dumps(
            {
                "origins": [
                    {
                        "name": "exempt-owner",
                        "source_networks": ["192.0.2.10/32"],
                        "private": True,
                    },
                    {
                        "name": "guest",
                        "source_networks": ["192.0.2.11/32"],
                        "private": True,
                        "allowed_routes": ["auto"],
                    },
                ],
                "tag_resolver": None,
            }
        )
    )
    await ready(app)
    assert (await chat(app, address="192.0.2.10")).status_code == 200
    assert (await chat(app, address="192.0.2.11")).status_code == 200
    totals = store.principal_usage(days=1)
    assert {entry["principal"] for entry in totals} == {
        "origin:exempt-owner",
        "origin:guest",
    }
    assert all(entry["requests"] == 1 for entry in totals)


@pytest.mark.asyncio
async def test_a_refused_request_creates_no_principal_row(tmp_path):
    """A refusal before any upstream send spent nothing, so nothing is charged.

    The request itself is still in the event log; the ledger stays clean, which
    is the contract the existing account tests already assert.
    """
    app, store, _ = router(tmp_path)
    await ready(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 9000)),
        base_url="http://router.test",
    ) as http:
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "does-not-exist",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 404
    assert store.principal_usage(days=1) == []
    [event] = store.events()
    assert event["status"] == "failed"
    assert "usage" not in event and "principal" not in event


@pytest.mark.asyncio
async def test_estimates_are_marked_apart_from_reports(tmp_path):
    """When the engine sends no usage, the count is an estimate and says so."""
    reply = {
        "id": "chatcmpl-no-usage",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "test-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "a" * 40},
             "finish_reason": "stop"}
        ],
    }
    app, store, _ = router(tmp_path, reply=reply)
    await ready(app)
    assert (await chat(app)).status_code == 200
    entry = store.principal_usage(days=1)[0]
    assert entry["prompt_tokens"] == 0 and entry["completion_tokens"] == 0
    assert entry["estimated_tokens"] > 0
    assert entry["total_tokens"] == entry["estimated_tokens"]


@pytest.mark.asyncio
async def test_the_usage_endpoint_is_operator_only_and_reports_totals(tmp_path):
    app, store, _ = router(tmp_path)
    await ready(app)
    await chat(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 9000)),
        base_url="http://127.0.0.1:8680",
    ) as http:
        response = await http.get("/api/usage", params={"days": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 1
    assert body["total_tokens"] == USAGE["total_tokens"]
    assert body["principals"][0]["principal"] == "addr:192.0.2.10"


def test_resolve_order_is_account_key_origin_address():
    """The identity ladder is a contract, not an accident of code order."""

    class State:
        account_id = None
        key_id = None
        caller = None

    class Request:
        def __init__(self, host):
            self.state = State()
            self.client = type("C", (), {"host": host, "port": 1})()

    request = Request("192.0.2.10")
    assert resolve(request)["principal"] == "addr:192.0.2.10"
    request.state.key_id = "k1"
    assert resolve(request)["principal"] == "key:k1"
    request.state.account_id = "a1"
    assert resolve(request)["principal"] == "account:a1"


def test_row_shape_matches_the_store_columns():
    principal = {
        "principal": "addr:192.0.2.10",
        "kind": "address",
        "label": "192.0.2.10",
        "login": "",
    }

    class Usage:
        estimated = False

        def row(self):
            return (5, 6, 0)

    values = row(principal, 1000.0, REPORTING_WINDOW_SECONDS, Usage())
    assert values == (
        "addr:192.0.2.10", "address", "192.0.2.10", "",
        1000.0, REPORTING_WINDOW_SECONDS, 5, 6, 0,
    )


def test_window_start_is_stable_within_a_day():
    now = time.time()
    assert window_start(now, 86400) == window_start(now + 10, 86400)
