"""Every account surface is inert while accounts.enabled is false.

Each PR in the accounts series appends one check per surface it adds. The
fixture turns both switches on, provisions every account record, then turns
the master switch off so the assertions prove the disabled state, not an
empty one.
"""

import json
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import (
    AccountLevel,
    AccountsSettings,
    Configuration,
    Engine,
    Route,
    Selector,
)
from gateway.security.credentials import digest


class Upstream:
    def __init__(self):
        self.completions = 0

    async def handle(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        self.completions += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": body["model"], "choices": [{"message": {"content": "ok"}}]},
        )


class Disabled:
    def __init__(self, app, upstream, account, key, device):
        self.app, self.upstream = app, upstream
        self.account, self.key, self.device = account, key, device
        self.store = app.state.store

    def http(self, address, **headers):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url="http://router.test",
            headers=headers,
        )


async def provision(tmp_path) -> Disabled:
    upstream = Upstream()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream.handle)
    )
    store = app.state.store
    engine = Engine(name="Local endpoint", base_url="http://local.test/v1")
    level = AccountLevel(name="Standard", route_names=["private"])
    store.save(
        Configuration(
            engines=[engine],
            routes=[
                Route(
                    name="private",
                    primary=Selector(engine_ids=[engine.id]),
                    require_caller_key=True,
                )
            ],
            accounts=AccountsSettings(enabled=True, device_registration_enabled=True),
            account_levels=[level],
        )
    )
    account = store.create_account("alice", "Alice", level.id)
    token, _ = store.issue_activation(account["id"], "activate")
    assert store.activate_account(token, digest("correct horse battery"))
    key, _ = store.create_account_key(account["id"], "laptop")
    device = store.register_device(account["id"], "192.0.2.77", "bench")
    store.save_portal_session("session-digest", account["id"], time.time() + 3600)
    config = store.config()
    config.accounts.enabled = False
    store.save(config)
    await app.state.discovery.refresh()
    return Disabled(app, upstream, store.account_snapshot(account["id"]), key, device)


async def account_key_is_refused_before_lookup(ctx: Disabled):
    async with ctx.http("192.0.2.50", Authorization=f"Bearer {ctx.key}") as http:
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "private", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "User accounts are not enabled on this router"
    assert ctx.upstream.completions == 0
    caller = next(c for c in ctx.store.callers() if c["source_address"] == "192.0.2.50")
    assert caller["identity_basis"] == "unassigned" and caller["account_id"] is None


async def registered_device_grants_nothing(ctx: Disabled):
    async with ctx.http(ctx.device["address"]) as http:
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "private", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    assert ctx.upstream.completions == 0
    caller = next(
        c for c in ctx.store.callers() if c["source_address"] == ctx.device["address"]
    )
    assert caller["identity_basis"] == "shared_access" and caller["account_id"] is None


async def limits_are_never_consulted(ctx: Disabled):
    limits = ctx.app.state.proxy.limits
    admissions = []
    original = limits.admit
    limits.admit = lambda *args, **kwargs: (
        admissions.append(args) or original(*args, **kwargs)
    )
    try:
        for address, headers in (
            ("192.0.2.50", {"Authorization": f"Bearer {ctx.key}"}),
            (ctx.device["address"], {}),
        ):
            async with ctx.http(address, **headers) as http:
                response = await http.post(
                    "/v1/chat/completions",
                    json={
                        "model": "private",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            assert response.status_code == 401
    finally:
        limits.admit = original
    assert admissions == []
    assert ctx.store.usage_windows(ctx.account["id"]) == []
    assert limits.ledger.windows == {}


SURFACES = [
    account_key_is_refused_before_lookup,
    registered_device_grants_nothing,
    limits_are_never_consulted,
]


@pytest.mark.asyncio
async def test_every_account_surface_is_inert_while_disabled(tmp_path):
    ctx = await provision(tmp_path)
    assert ctx.store.config().accounts.enabled is False
    # Records survive the switch so an operator can prepare before enabling.
    assert ctx.store.account_snapshot(ctx.account["id"])["status"] == "active"
    assert ctx.store.account_keys(ctx.account["id"])
    for surface in SURFACES:
        await surface(ctx)
