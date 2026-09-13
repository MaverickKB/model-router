"""A pre-registered source address reaches key-gated routes as its account, only in dev mode."""

import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from gateway.accounts.limits import window_start
from gateway.app import create_app
from gateway.schema import (
    AccountLevel,
    AccountsSettings,
    Client,
    Configuration,
    Engine,
    Route,
    Security,
    Selector,
    TokenBudget,
)
from gateway.security.credentials import digest

PASSWORD = "correct horse battery"
ORIGIN = "http://localhost"
REGISTERED = "192.0.2.20"
NEIGHBOUR = "192.0.2.21"
BUDGET = TokenBudget(max_tokens=3000, window_seconds=3600)
OUTSIDE = "Route is outside the client's allowlist"


class Fleet:
    """Two local engines so a failed attempt has a retry target."""

    def __init__(self):
        self.models = {
            "local-a.test": ["local-model"],
            "local-b.test": ["local-model"],
            "cloud.test": ["remote"],
        }
        self.calls = []
        self.statuses = []
        self.on_call = None

    async def handle(self, request):
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": m} for m in self.models.get(host, [])]}
            )
        body = json.loads(request.content)
        self.calls.append((host, body["model"]))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 30},
            },
        )


class Setup:
    def __init__(self, app, fleet, level, lan, account, device):
        self.app, self.fleet, self.level, self.lan = app, fleet, level, lan
        self.account, self.device = account, device
        self.store = app.state.store

    def caller(self, address=REGISTERED, **headers):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url="http://router.test",
            headers={"User-Agent": "python-requests/2.33.0", **headers},
        )

    def operator(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        )

    @asynccontextmanager
    async def portal(self, username="alice"):
        """A portal session for an activated account, signed in through the API."""
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
        )
        async with http:
            signed = await http.post(
                "/api/v1/portal/login",
                json={"username": username, "password": PASSWORD},
            )
            assert signed.status_code == 200, signed.text
            yield http

    def add_account(self, username, name):
        account = self.store.create_account(username, name, self.level.id)
        token, _ = self.store.issue_activation(account["id"], "activate")
        assert self.store.activate_account(token, digest(PASSWORD)) == account["id"]
        return self.store.account_snapshot(account["id"])

    def switches(self, enabled: bool, devices: bool):
        config = self.store.config()
        config.accounts.enabled = enabled
        config.accounts.device_registration_enabled = devices
        self.store.save(config)

    def caller_row(self, address):
        return next(c for c in self.store.callers() if c["source_address"] == address)

    def seed_used(self, used: int):
        now = time.time()
        seconds = BUDGET.window_seconds
        self.store.record_usage(
            self.account["id"], window_start(now, seconds), seconds, used, 0, 0
        )
        self.app.state.proxy.limits.ledger.seed(self.store.open_usage_windows(now))


async def build(tmp_path, *, enabled=True, devices=True, budget=None) -> Setup:
    fleet = Fleet()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet.handle)
    )
    store = app.state.store
    local_a = Engine(name="Local A", base_url="http://local-a.test/v1")
    local_b = Engine(name="Local B", base_url="http://local-b.test/v1")
    cloud = Engine(
        name="Cloud endpoint",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    ordered = Selector(engine_ids=[local_a.id, local_b.id])
    lan = Client(
        name="LAN policy",
        route_names=["free"],
        allow_network_auth=True,
        source_networks=["192.0.2.0/24"],
    )
    level = AccountLevel(
        name="Standard",
        route_names=["free", "private"],
        allow_cloud=False,
        token_budget=budget,
    )
    store.save(
        Configuration(
            engines=[local_a, local_b, cloud],
            routes=[
                Route(name="free", primary=ordered, strategy="ordered"),
                Route(
                    name="private",
                    primary=ordered,
                    strategy="ordered",
                    require_caller_key=True,
                ),
                Route(
                    name="cloud-only",
                    primary=Selector(kind="cloud", engine_ids=[cloud.id]),
                    require_caller_key=True,
                ),
            ],
            clients=[lan],
            security=Security(operator_auth_enabled=False),
            accounts=AccountsSettings(
                enabled=enabled, device_registration_enabled=devices
            ),
            account_levels=[level],
        )
    )
    store.set_secret(cloud.id, "demo-provider-key")
    setup = Setup(app, fleet, level, lan, None, None)
    setup.account = setup.add_account("alice", "Alice")
    setup.device = store.register_device(setup.account["id"], REGISTERED, "bench")
    await app.state.discovery.refresh()
    return setup


async def complete(http, model, **extra):
    return await http.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,devices", [(False, False), (True, False), (False, True)]
)
async def test_device_registration_requires_both_switches(tmp_path, enabled, devices):
    setup = await build(tmp_path, enabled=enabled, devices=devices)
    async with setup.caller() as http:
        open_route = await complete(http, "free")
        gated = await complete(http, "private")
    # Exactly today's behaviour: the admin source policy, whose allowlist
    # excludes the gated route before its key gate is even reached.
    assert open_route.status_code == 200
    assert gated.status_code == 403
    assert gated.json()["error"]["message"] == "Route is outside the client's allowlist"
    row = setup.caller_row(REGISTERED)
    assert row["identity_basis"] == "source_network"
    assert row["policy_id"] == setup.lan.id
    assert row["account_id"] is None and row["device_id"] is None
    assert setup.store.device_snapshot(setup.device["id"])["last_matched"] is None
    if enabled:
        async with setup.portal() as portal:
            register = await portal.post(
                "/api/v1/portal/devices", json={"address": NEIGHBOUR, "name": "x"}
            )
            remove = await portal.delete(f"/api/v1/portal/devices/{setup.device['id']}")
            me = await portal.get("/api/v1/portal/me")
            status = await portal.get("/api/v1/portal/status")
        assert register.status_code == 404
        assert register.json()["detail"] == "Device registration is not enabled"
        assert remove.status_code == 404
        assert me.json()["devices"]["registered"] is None
        assert status.json()["device_registration_enabled"] is False
        assert setup.store.device_snapshot(setup.device["id"]) is not None
    async with setup.operator() as operator:
        state = await operator.get("/api/v1/state")
    assert not [w for w in state.json()["warnings"] if "Registered device" in w]


@pytest.mark.asyncio
async def test_registered_device_reaches_key_gated_route_with_level_permissions_and_limits(
    tmp_path,
):
    setup = await build(tmp_path, budget=BUDGET)
    async with setup.caller() as http:
        gated = await complete(http, "private")
        cloud = await complete(http, "cloud-only")
        models = await http.get("/v1/models")
    assert gated.status_code == 200
    assert cloud.status_code == 403
    assert {row["id"] for row in models.json()["data"]} == {"free", "private"}
    row = setup.caller_row(REGISTERED)
    assert row["identity_basis"] == "registered_device"
    assert row["name"] == "Alice · bench"
    assert row["account_id"] == setup.account["id"]
    assert row["device_id"] == setup.device["id"] and row["key_id"] is None
    assert row["policy_id"] == setup.account["id"]
    assert setup.store.device_snapshot(setup.device["id"])["last_matched"] is not None
    event = next(e for e in setup.store.events() if e["status"] == "completed")
    assert event["account_id"] == setup.account["id"] and event["usage"]["estimated"] is False
    assert setup.store.usage_windows(setup.account["id"])[0]["requests"] == 1

    setup.seed_used(BUDGET.max_tokens)
    calls = len(setup.fleet.calls)
    async with setup.caller() as http:
        limited = await complete(http, "private")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "token_budget_exceeded"
    assert "Retry-After" in limited.headers
    assert len(setup.fleet.calls) == calls
    # The device path is unkeyed, so the refusal must not name the account.
    text = limited.text.casefold()
    assert "alice" not in text and "standard" not in text and "bench" not in text


@pytest.mark.asyncio
async def test_unusable_key_header_from_registered_address_is_still_the_device(tmp_path):
    # Clients that always send a placeholder key header are the reason this
    # mode exists: an unusable bearer from the registered host is still the
    # device, while the same header from a neighbour stays unassigned.
    setup = await build(tmp_path)
    async with setup.caller(Authorization="Bearer sk-placeholder") as placeholder:
        assert (await complete(placeholder, "private")).status_code == 200
    async with setup.caller(NEIGHBOUR, Authorization="Bearer sk-placeholder") as other:
        assert (await complete(other, "private")).status_code == 403
    row = setup.caller_row(REGISTERED)
    assert row["identity_basis"] == "registered_device"
    assert row["device_id"] == setup.device["id"]
    assert setup.caller_row(NEIGHBOUR)["identity_basis"] == "unassigned"


@pytest.mark.asyncio
async def test_device_outranks_network_policy_and_state_names_the_policy(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as registered, setup.caller(NEIGHBOUR) as neighbour:
        assert (await complete(registered, "free")).status_code == 200
        assert (await complete(neighbour, "free")).status_code == 200
        assert (await complete(neighbour, "private")).status_code == 403
    assert setup.caller_row(REGISTERED)["identity_basis"] == "registered_device"
    other = setup.caller_row(NEIGHBOUR)
    assert other["identity_basis"] == "source_network"
    assert other["policy_id"] == setup.lan.id and other["account_id"] is None

    async with setup.operator() as operator:
        state = await operator.get("/api/v1/state")
        devices = await operator.get("/api/v1/devices")
    assert (
        "Registered device 'bench' (Alice) takes precedence over source policy "
        "'LAN policy' (192.0.2.0/24) for 192.0.2.20"
    ) in state.json()["warnings"]
    (device,) = devices.json()["devices"]
    assert device["account_name"] == "Alice"
    assert device["shadows"] == [
        {"client_id": setup.lan.id, "client_name": "LAN policy", "network": "192.0.2.0/24"}
    ]
    async with setup.portal() as portal:
        me = await portal.get("/api/v1/portal/me")
    (registered_row,) = me.json()["devices"]["registered"]
    assert registered_row["shadowed"] is True
    assert registered_row["address"] == REGISTERED


@pytest.mark.asyncio
async def test_disabled_or_suspended_device_falls_through_to_network_policy(tmp_path):
    setup = await build(tmp_path)
    device_id = setup.device["id"]

    async def basis_now():
        async with setup.caller() as http:
            open_route = await complete(http, "free")
            gated = await complete(http, "private")
        assert open_route.status_code == 200
        return setup.caller_row(REGISTERED)["identity_basis"], gated.status_code

    async with setup.operator() as operator:
        disabled = await operator.put(
            f"/api/v1/devices/{device_id}", json={"enabled": False}
        )
        assert disabled.status_code == 200
        assert disabled.json()["device"]["enabled"] is False
        assert await basis_now() == ("source_network", 403)
        state = await operator.get("/api/v1/state")
        assert not [w for w in state.json()["warnings"] if "Registered device" in w]

        enabled = await operator.put(
            f"/api/v1/devices/{device_id}", json={"enabled": True}
        )
        assert enabled.json()["device"]["enabled"] is True
        assert await basis_now() == ("registered_device", 200)

        suspended = await operator.put(
            f"/api/v1/accounts/{setup.account['id']}", json={"status": "suspended"}
        )
        assert suspended.status_code == 200
        assert await basis_now() == ("source_network", 403)
        state = await operator.get("/api/v1/state")
        assert not [w for w in state.json()["warnings"] if "Registered device" in w]


@pytest.mark.asyncio
async def test_device_disable_applies_before_next_attempt(tmp_path):
    setup = await build(tmp_path)
    setup.fleet.statuses = [503]

    def disable(call_number):
        if call_number == 1:
            setup.store.set_device_enabled(setup.device["id"], False)

    setup.fleet.on_call = disable
    async with setup.app.router.lifespan_context(setup.app), setup.caller() as http:
        response = await complete(http, "private")
    # The proxy re-identifies the host before every attempt, so the disabled
    # row falls through to the source policy exactly as a fresh request would.
    assert response.status_code == 403
    assert response.json()["error"]["message"] == OUTSIDE
    assert len(setup.fleet.calls) == 1
    event = setup.store.events()[0]
    assert event["status"] == "denied" and event["http_status"] == 403
    assert setup.app.state.proxy.limits.active(setup.account["id"]) == 0

    switched = await build(tmp_path / "switch")
    switched.fleet.statuses = [503]

    def turn_off(call_number):
        if call_number == 1:
            switched.switches(enabled=True, devices=False)

    switched.fleet.on_call = turn_off
    async with (
        switched.app.router.lifespan_context(switched.app),
        switched.caller() as http,
    ):
        response = await complete(http, "private")
    assert response.status_code == 403
    assert response.json()["error"]["message"] == OUTSIDE
    assert len(switched.fleet.calls) == 1


@pytest.mark.asyncio
async def test_forwarded_headers_never_match_a_device(tmp_path):
    setup = await build(tmp_path)
    forwarded = {
        "X-Forwarded-For": REGISTERED,
        "X-Real-IP": REGISTERED,
        "Forwarded": f"for={REGISTERED}",
    }
    async with setup.caller(NEIGHBOUR, **forwarded) as http:
        assert (await complete(http, "free")).status_code == 200
        assert (await complete(http, "private")).status_code == 403
    row = setup.caller_row(NEIGHBOUR)
    assert row["identity_basis"] == "source_network" and row["account_id"] is None
    assert setup.store.device_snapshot(setup.device["id"])["last_matched"] is None


@pytest.mark.asyncio
async def test_device_address_must_be_single_literal_and_globally_unique(tmp_path):
    setup = await build(tmp_path)
    bob = setup.add_account("bob", "Bob")
    async with setup.portal("bob") as portal:
        for address in (
            "192.0.2.0/24",
            "224.0.0.1",
            "169.254.1.1",
            "0.0.0.0",
            "2001:db8::1%eth0",
            "desk",
        ):
            response = await portal.post(
                "/api/v1/portal/devices", json={"address": address, "name": "x"}
            )
            assert response.status_code == 422, address
            assert response.json()["detail"] == "Enter one IPv4 or IPv6 address"
        taken = await portal.post(
            "/api/v1/portal/devices", json={"address": REGISTERED, "name": "x"}
        )
        assert taken.status_code == 409
        assert taken.json()["detail"] == "This address is already registered"
        mapped = await portal.post(
            "/api/v1/portal/devices",
            json={"address": "::ffff:192.0.2.30", "name": "  Desk  "},
        )
        assert mapped.status_code == 201, mapped.text
        assert mapped.json()["device"]["address"] == "192.0.2.30"
        assert mapped.json()["device"]["name"] == "Desk"
        assert mapped.json()["device"]["shadowed"] is True
        loopback = await portal.post(
            "/api/v1/portal/devices", json={"address": "::1", "name": "host"}
        )
        assert loopback.status_code == 201
        assert loopback.json()["device"]["shadowed"] is False
        for n in range(8):
            more = await portal.post(
                "/api/v1/portal/devices",
                json={"address": f"198.51.100.{n}", "name": f"d{n}"},
            )
            assert more.status_code == 201
        capped = await portal.post(
            "/api/v1/portal/devices", json={"address": "198.51.100.99", "name": "d"}
        )
        assert capped.status_code == 409
        assert (
            capped.json()["detail"] == "This account already has 10 registered devices"
        )
    assert len(setup.store.devices_for(bob["id"])) == 10
    assert len(setup.store.devices_for(setup.account["id"])) == 1


@pytest.mark.asyncio
async def test_observed_devices_are_scoped_to_the_account(tmp_path):
    setup = await build(tmp_path)
    bob = setup.add_account("bob", "Bob")
    bob_key, _ = setup.store.create_account_key(bob["id"], "phone")
    async with setup.caller() as device_path:
        assert (await complete(device_path, "private")).status_code == 200
    async with setup.caller("192.0.2.60", Authorization=f"Bearer {bob_key}") as keyed:
        assert (await complete(keyed, "private")).status_code == 200
    async with setup.portal("alice") as alice, setup.portal("bob") as bob_portal:
        alice_me = (await alice.get("/api/v1/portal/me")).json()
        bob_me = (await bob_portal.get("/api/v1/portal/me")).json()
    (alice_seen,) = alice_me["devices"]["observed"]
    assert alice_seen["source_address"] == REGISTERED
    assert alice_seen["via"] == "device" and alice_seen["credential_name"] == "bench"
    (bob_seen,) = bob_me["devices"]["observed"]
    assert bob_seen["source_address"] == "192.0.2.60"
    assert bob_seen["via"] == "key" and bob_seen["credential_name"] == "phone"
    assert bob_me["devices"]["registered"] == []
    assert alice_me["devices"]["registered"][0]["id"] == setup.device["id"]


@pytest.mark.asyncio
async def test_portal_delete_is_scoped_and_admin_delete_is_not(tmp_path):
    setup = await build(tmp_path)
    bob = setup.add_account("bob", "Bob")
    bob_device = setup.store.register_device(bob["id"], NEIGHBOUR, "bob-box")
    async with setup.portal("bob") as portal:
        foreign = await portal.delete(f"/api/v1/portal/devices/{setup.device['id']}")
        own = await portal.delete(f"/api/v1/portal/devices/{bob_device['id']}")
    assert foreign.status_code == 404
    assert setup.store.device_snapshot(setup.device["id"]) is not None
    assert own.status_code == 200 and own.json() == {"ok": True}
    assert setup.store.device_snapshot(bob_device["id"]) is None

    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup.app, client=("203.0.113.9", 1)),
        base_url="http://router.test",
    )
    async with anonymous:
        assert (await anonymous.get("/api/v1/devices")).status_code == 401
    async with setup.operator() as operator:
        assert (await operator.put("/api/v1/devices/nope", json={"enabled": True})).status_code == 404
        assert (await operator.delete("/api/v1/devices/nope")).status_code == 404
        removed = await operator.delete(f"/api/v1/devices/{setup.device['id']}")
        listed = await operator.get("/api/devices")
    assert removed.status_code == 200
    assert listed.json() == {"devices": []}
    async with setup.caller() as http:
        assert (await complete(http, "private")).status_code == 403
    assert setup.caller_row(REGISTERED)["identity_basis"] == "source_network"


@pytest.mark.asyncio
async def test_portal_device_writes_require_same_origin(tmp_path):
    setup = await build(tmp_path)
    async with setup.portal() as portal:
        cookies = {"router_portal": portal.cookies["router_portal"]}
    for origin in (None, "http://evil.test"):
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup.app, client=("127.0.0.1", 1)),
            base_url=ORIGIN,
            headers={"Origin": origin} if origin else {},
            cookies=cookies,
        )
        async with http:
            assert (await http.get("/api/v1/portal/me")).status_code == 200
            writes = [
                await http.post(
                    "/api/v1/portal/devices", json={"address": NEIGHBOUR, "name": "x"}
                ),
                await http.delete(f"/api/v1/portal/devices/{setup.device['id']}"),
            ]
        for response in writes:
            assert response.status_code == 403, origin
            assert (
                response.json()["detail"]
                == "Portal changes require a same-origin request"
            )
    (device,) = setup.store.devices_for(setup.account["id"])
    assert device["id"] == setup.device["id"]
