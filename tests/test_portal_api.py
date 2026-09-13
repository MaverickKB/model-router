"""Account holders activate, sign in and manage keys through a portal session that is never operator authority."""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager

import httpx
import pytest

import gateway.accounts.portal as portal_module
from gateway.app import create_app
from gateway.schema import (
    AccountLevel,
    AccountsSettings,
    Configuration,
    Engine,
    Route,
    Selector,
)

USAGE = {"prompt_tokens": 12, "completion_tokens": 30}
PASSWORD = "correct horse battery"
NEW_PASSWORD = "a different long password"
ORIGIN = "http://localhost"
STATUS = "/api/v1/portal/status"
ACTIVATE = "/api/v1/portal/activate"
LOGIN = "/api/v1/portal/login"
LOGOUT = "/api/v1/portal/logout"
ME = "/api/v1/portal/me"
PASSWORD_PATH = "/api/v1/portal/password"
KEYS = "/api/v1/portal/keys"
DISABLED = "User accounts are not enabled on this router"
SUSPENDED = "Account is suspended. Contact the administrator."
COMPLETION = {"model": "private", "messages": [{"role": "user", "content": "hi"}]}


def cookie_attributes(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:]}


class Fleet:
    def __init__(self):
        self.calls = 0

    async def handle(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        self.calls += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": USAGE,
            },
        )


class Harness:
    def __init__(self, app, fleet, level):
        self.app, self.fleet, self.level = app, fleet, level
        self.store = app.state.store

    def browser(self, address="127.0.0.1", origin=ORIGIN, cookies=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url=ORIGIN,
            headers={"Origin": origin} if origin else {},
            cookies=cookies,
        )

    @asynccontextmanager
    async def operator(self):
        async with self.browser() as http:
            yield http

    def caller(self, key, address="192.0.2.50"):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url="http://router.test",
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "python-requests/2.33.0",
            },
        )

    async def add_account(self, username="alice", name="Alice"):
        async with self.operator() as http:
            created = await http.post(
                "/api/v1/accounts",
                json={"username": username, "name": name, "level_id": self.level.id},
            )
            assert created.status_code == 201, created.text
        return created.json()["account"], created.json()["activation"]["token"]

    @asynccontextmanager
    async def activated(self, username="alice", name="Alice"):
        """An activated account with the browser that activated it."""
        account, token = await self.add_account(username, name)
        async with self.browser() as http:
            response = await http.post(
                ACTIVATE, json={"token": token, "password": PASSWORD}
            )
            assert response.status_code == 200, response.text
            yield account, http

    @asynccontextmanager
    async def signed_in(self, username="alice", password=PASSWORD, address="127.0.0.1"):
        async with self.browser(address) as http:
            response = await http.post(
                LOGIN, json={"username": username, "password": password}
            )
            assert response.status_code == 200, response.text
            yield http

    def set_enabled(self, enabled: bool):
        config = self.store.config()
        config.accounts.enabled = enabled
        self.store.save(config)

    def dump(self) -> str:
        return "\n".join(self.store.db.iterdump())


async def build(tmp_path) -> Harness:
    fleet = Fleet()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet.handle)
    )
    local = Engine(name="Local endpoint", base_url="http://local.test/v1")
    cloud = Engine(
        name="Cloud endpoint",
        base_url="https://cloud.test/v1",
        kind="cloud",
        model_patterns=["*"],
    )
    level = AccountLevel(name="Standard", route_names=["free", "private", "cloud-only"])
    app.state.store.save(
        Configuration(
            engines=[local, cloud],
            routes=[
                Route(name="free", primary=Selector(engine_ids=[local.id])),
                Route(
                    name="private",
                    primary=Selector(engine_ids=[local.id]),
                    require_caller_key=True,
                ),
                Route(
                    name="cloud-only",
                    primary=Selector(kind="cloud", engine_ids=[cloud.id]),
                    require_caller_key=True,
                ),
            ],
            accounts=AccountsSettings(enabled=True),
            account_levels=[level],
        )
    )
    app.state.store.set_secret(cloud.id, "demo-provider-key")
    await app.state.discovery.refresh()
    return Harness(app, fleet, level)


@pytest.mark.asyncio
async def test_activation_sets_password_once_and_signs_in(tmp_path):
    harness = await build(tmp_path)
    account, token = await harness.add_account("alice-example", "Alice")
    async with harness.browser() as http:
        short = await http.post(ACTIVATE, json={"token": token, "password": "short"})
        same = await http.post(
            ACTIVATE, json={"token": token, "password": "ALICE-EXAMPLE"}
        )
        bogus = await http.post(ACTIVATE, json={"token": "mra_x", "password": PASSWORD})
        assert short.status_code == 422
        assert short.json()["detail"] == "Use at least 12 characters"
        assert same.status_code == 422
        assert same.json()["detail"] == "Password must not be the username"
        assert bogus.status_code == 400
        assert bogus.json()["detail"] == "Activation link is invalid or expired"
        assert harness.store.account_snapshot(account["id"])["status"] == "pending"
        # A refused password never consumed the link.
        activated = await http.post(
            ACTIVATE, json={"token": token, "password": PASSWORD}
        )
        assert activated.status_code == 200 and activated.json() == {"ok": True}
        attributes = cookie_attributes(activated.headers["set-cookie"])
        assert activated.headers["set-cookie"].startswith("router_portal=")
        assert {"httponly", "samesite=strict", "path=/", "max-age=604800"} <= attributes
        assert "secure" not in attributes
        again = await http.post(ACTIVATE, json={"token": token, "password": PASSWORD})
        assert again.status_code == 400
        me = await http.get(ME)
        brief = await harness.browser().post(
            LOGIN,
            json={"username": "Alice-Example", "password": PASSWORD, "remember": False},
        )
    assert me.status_code == 200
    assert me.json()["account"]["username"] == "alice-example"
    assert me.json()["account"]["status"] == "active"
    assert me.json()["account"]["activated_at"] is not None
    assert brief.status_code == 200
    assert "max-age=3600" in cookie_attributes(brief.headers["set-cookie"])
    assert harness.store.account_snapshot(account["id"])["last_login"] is not None


@pytest.mark.asyncio
async def test_unknown_wrong_and_pending_logins_are_indistinguishable(
    tmp_path, monkeypatch
):
    harness = await build(tmp_path)
    async with harness.activated():
        pass
    await harness.add_account("pending", "Pending")
    verified = []
    real = portal_module.verify
    monkeypatch.setattr(
        portal_module,
        "verify",
        lambda encoded, value: verified.append(encoded) or real(encoded, value),
    )
    async with harness.browser() as http:
        outcomes = [
            await http.post(LOGIN, json={"username": username, "password": password})
            for username, password in (
                ("nobody", PASSWORD),
                ("alice", "wrong password here"),
                ("pending", PASSWORD),
            )
        ]
    assert [(r.status_code, r.json()) for r in outcomes] == [
        (401, {"detail": "Username or password is incorrect"})
    ] * 3
    assert all("set-cookie" not in r.headers for r in outcomes)
    # Every attempt pays for one verification; unknown and pending use the dummy.
    assert len(verified) == 3
    assert verified[0] == verified[2] == portal_module.DUMMY_VERIFIER
    assert verified[1] != portal_module.DUMMY_VERIFIER


@pytest.mark.asyncio
async def test_suspended_login_reveals_only_after_correct_password(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (account, first):
        async with harness.signed_in() as second, harness.operator() as admin:
            path = f"/api/v1/accounts/{account['id']}"
            assert (
                await admin.put(path, json={"status": "suspended"})
            ).status_code == 200
            # Suspension through the API ends every session at once, used or not.
            assert (await first.get(ME)).status_code == 401
            assert (await second.get(ME)).status_code == 401
            async with harness.browser() as http:
                wrong = await http.post(
                    LOGIN, json={"username": "alice", "password": "not the password"}
                )
                right = await http.post(
                    LOGIN, json={"username": "alice", "password": PASSWORD}
                )
            assert wrong.status_code == 401
            assert wrong.json()["detail"] == "Username or password is incorrect"
            assert right.status_code == 403 and right.json()["detail"] == SUSPENDED
            assert "set-cookie" not in right.headers
            assert (await admin.put(path, json={"status": "active"})).status_code == 200
            assert (await first.get(ME)).status_code == 401
        async with harness.signed_in() as fresh:
            assert (await fresh.get(ME)).status_code == 200
            # A session that outlives a store-level suspension is refused and
            # revoked on its first use, and stays gone after re-enabling.
            harness.store.update_account(account["id"], status="suspended")
            locked = await fresh.get(ME)
            assert locked.status_code == 403 and locked.json()["detail"] == SUSPENDED
            harness.store.update_account(account["id"], status="active")
            assert (await fresh.get(ME)).status_code == 401
    assert harness.store.portal_sessions() == {}


@pytest.mark.asyncio
async def test_portal_login_is_rate_limited_per_source_and_username(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated("alice", "Alice"), harness.activated("bob", "Bob"):
        pass
    async with harness.browser("192.0.2.10") as http:
        by_source = [
            await http.post(LOGIN, json={"username": "alice", "password": "wrong"})
            for _ in range(11)
        ]
    assert [r.status_code for r in by_source] == [401] * 10 + [429]
    assert by_source[-1].headers["retry-after"] == "60"
    by_user = []
    for n in range(11):
        async with harness.browser(f"192.0.2.{100 + n}") as http:
            by_user.append(
                await http.post(LOGIN, json={"username": "bob", "password": "wrong"})
            )
    assert [r.status_code for r in by_user] == [401] * 10 + [429]
    async with harness.browser("192.0.2.200") as http:
        refused = await http.post(LOGIN, json={"username": "bob", "password": PASSWORD})
    assert refused.status_code == 429 and "set-cookie" not in refused.headers


@pytest.mark.asyncio
async def test_portal_writes_require_same_origin(tmp_path):
    harness = await build(tmp_path)
    account, token = await harness.add_account()
    async with harness.browser(origin=None) as http:
        activate = await http.post(
            ACTIVATE, json={"token": token, "password": PASSWORD}
        )
        login = await http.post(LOGIN, json={"username": "alice", "password": PASSWORD})
    for response in (activate, login):
        assert response.status_code == 403
        assert response.json()["detail"] == "Open the portal to sign in"
    async with harness.browser() as http:
        activated = await http.post(
            ACTIVATE, json={"token": token, "password": PASSWORD}
        )
        assert activated.status_code == 200
        cookies = {"router_portal": activated.cookies["router_portal"]}
    for origin in (None, "http://evil.test"):
        async with harness.browser(origin=origin, cookies=cookies) as http:
            assert (await http.get(ME)).status_code == 200
            writes = [
                await http.post(LOGOUT),
                await http.put(
                    PASSWORD_PATH, json={"current": PASSWORD, "new": NEW_PASSWORD}
                ),
                await http.post(KEYS, json={"name": "laptop"}),
                await http.delete(f"{KEYS}/nope"),
            ]
        for response in writes:
            assert response.status_code == 403
            assert (
                response.json()["detail"]
                == "Portal changes require a same-origin request"
            )
    assert harness.store.account_keys(account["id"]) == []
    assert len(harness.store.portal_sessions()) == 1


@pytest.mark.asyncio
async def test_operator_cookie_never_authorizes_portal_and_vice_versa(tmp_path):
    harness = await build(tmp_path)
    body = {"username": "bob", "level_id": harness.level.id}
    _account, token = await harness.add_account("alice")
    async with harness.browser(address="192.0.2.50") as session:
        activated = await session.post(
            ACTIVATE, json={"token": token, "password": PASSWORD}
        )
        assert activated.status_code == 200, activated.text
        assert (await session.get("/api/v1/state")).status_code == 401
        assert (await session.get("/api/v1/accounts")).status_code == 401
        assert (await session.post("/api/v1/accounts", json=body)).status_code == 401
        assert (await session.get(ME)).status_code == 200
    async with harness.operator() as admin:
        assert (await admin.get("/api/v1/accounts")).status_code == 200
        assert (await admin.get(ME)).status_code == 401
        assert (await admin.post(KEYS, json={"name": "laptop"})).status_code == 401
        assert (await admin.get(STATUS)).json()["signed_in"] is False
    assert harness.store.account_by_username("bob") is None


@pytest.mark.asyncio
async def test_password_change_revokes_other_sessions_only_and_keeps_keys(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (account, first):
        key = (await first.post(KEYS, json={"name": "laptop"})).json()["key"]
        async with harness.signed_in() as second:
            assert (await second.get(ME)).status_code == 200
            before = first.cookies["router_portal"]
            wrong = await first.put(
                PASSWORD_PATH, json={"current": "not it", "new": NEW_PASSWORD}
            )
            weak = await first.put(
                PASSWORD_PATH, json={"current": PASSWORD, "new": "x"}
            )
            changed = await first.put(
                PASSWORD_PATH, json={"current": PASSWORD, "new": NEW_PASSWORD}
            )
            assert wrong.status_code == 401
            assert wrong.json()["detail"] == "Current password is incorrect"
            assert weak.status_code == 422
            assert changed.status_code == 200 and changed.json() == {"ok": True}
            assert first.cookies["router_portal"] != before
            assert (await first.get(ME)).status_code == 200
            assert (await second.get(ME)).status_code == 401
        async with harness.browser() as http:
            old = await http.post(
                LOGIN, json={"username": "alice", "password": PASSWORD}
            )
            new = await http.post(
                LOGIN, json={"username": "alice", "password": NEW_PASSWORD}
            )
        assert old.status_code == 401 and new.status_code == 200
        async with harness.caller(key) as caller:
            assert (
                await caller.post("/v1/chat/completions", json=COMPLETION)
            ).status_code == 200
        signed_out = await first.post(LOGOUT)
        assert signed_out.status_code == 200
        assert (await first.get(ME)).status_code == 401
    assert harness.fleet.calls == 1
    assert [row["name"] for row in harness.store.account_keys(account["id"])] == [
        "laptop"
    ]


@pytest.mark.asyncio
async def test_reset_link_signs_out_every_session_until_it_is_consumed(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (account, first):
        async with harness.signed_in(address="192.0.2.20") as second:
            assert (await second.get(ME)).status_code == 200
            async with harness.operator() as operator:
                issued = await operator.post(
                    f"/api/v1/accounts/{account['id']}/activation"
                )
            assert issued.status_code == 200
            assert harness.store.portal_sessions() == {}
            assert harness.app.state.portal.sessions == {}
            assert (await first.get(ME)).status_code == 401
            assert (await second.get(ME)).status_code == 401
            assert (await first.post(KEYS, json={"name": "laptop"})).status_code == 401
        # The current password signs in again until the link is consumed.
        async with harness.browser() as http:
            again = await http.post(
                LOGIN, json={"username": "alice", "password": PASSWORD}
            )
        assert again.status_code == 200


@pytest.mark.asyncio
async def test_verification_in_flight_during_a_password_change_is_refused(
    tmp_path, monkeypatch
):
    """A sign-in or change whose argon2 check straddles a password change loses to it."""
    harness = await build(tmp_path)
    real = portal_module.verify
    gates: list[tuple[threading.Event, threading.Event]] = []

    def gated_verify(encoded, value):
        # The first call after each arm() blocks until the test releases it.
        if gates and not gates[-1][0].is_set():
            reached, release = gates[-1]
            reached.set()
            release.wait(5)
        return real(encoded, value)

    def arm():
        gates.append((threading.Event(), threading.Event()))
        return gates[-1]

    monkeypatch.setattr(portal_module, "verify", gated_verify)
    async with harness.activated() as (account, first):
        reached, release = arm()
        async with harness.browser("192.0.2.40") as late:
            attempt = asyncio.create_task(
                late.post(LOGIN, json={"username": "alice", "password": PASSWORD})
            )
            await asyncio.to_thread(reached.wait, 5)
            changed = await first.put(
                PASSWORD_PATH, json={"current": PASSWORD, "new": NEW_PASSWORD}
            )
            assert changed.status_code == 200
            release.set()
            refused = await attempt
        assert refused.status_code == 401
        assert refused.json()["detail"] == "Username or password is incorrect"
        assert "set-cookie" not in refused.headers
        assert len(harness.store.portal_sessions()) == 1
        assert len(harness.app.state.portal.sessions) == 1
        # Two changes against the same current password: only the first lands.
        verifier = harness.store.account_verifier(account["id"])
        reached, release = arm()
        async with harness.browser("192.0.2.41", cookies=first.cookies) as other:
            attempt = asyncio.create_task(
                other.put(
                    PASSWORD_PATH, json={"current": NEW_PASSWORD, "new": PASSWORD}
                )
            )
            await asyncio.to_thread(reached.wait, 5)
            winner = await first.put(
                PASSWORD_PATH,
                json={"current": NEW_PASSWORD, "new": "third password here"},
            )
            assert winner.status_code == 200
            release.set()
            loser = await attempt
        assert loser.status_code == 401
        assert loser.json()["detail"] == "Current password is incorrect"
        assert "set-cookie" not in loser.headers
        assert harness.store.account_verifier(account["id"]) not in (None, verifier)
        assert (await first.get(ME)).status_code == 200
        async with harness.browser() as http:
            assert (
                await http.post(
                    LOGIN, json={"username": "alice", "password": "third password here"}
                )
            ).status_code == 200


@pytest.mark.asyncio
async def test_password_change_current_check_is_rate_limited(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (account, browser):
        verifier = harness.store.account_verifier(account["id"])
        async with harness.browser("192.0.2.30", cookies=browser.cookies) as http:
            attempts = [
                await http.put(
                    PASSWORD_PATH, json={"current": "wrong", "new": NEW_PASSWORD}
                )
                for _ in range(11)
            ]
        assert [r.status_code for r in attempts] == [401] * 10 + [429]
        assert attempts[-1].headers["retry-after"] == "60"
        # The username budget is spent too, so a fresh source is refused as well.
        async with harness.browser("192.0.2.31", cookies=browser.cookies) as http:
            refused = await http.put(
                PASSWORD_PATH, json={"current": PASSWORD, "new": NEW_PASSWORD}
            )
        assert refused.status_code == 429 and "set-cookie" not in refused.headers
        assert harness.store.account_verifier(account["id"]) == verifier


@pytest.mark.asyncio
async def test_user_created_key_is_shown_once_and_works_on_v1(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (_, session):
        created = await session.post(KEYS, json={"name": "  laptop "})
        assert created.status_code == 201, created.text
        key, record = created.json()["key"], created.json()["record"]
        assert key.startswith("mru_") and record["name"] == "laptop"
        assert {"id", "name", "created"} <= set(record)
        me = (await session.get(ME)).json()
        assert [row["name"] for row in me["keys"]] == ["laptop"]
        assert key not in json.dumps(me)
        async with harness.caller(key) as caller:
            completed = await caller.post("/v1/chat/completions", json=COMPLETION)
        assert completed.status_code == 200
        revoked = await session.delete(f"{KEYS}/{record['id']}")
        again = await session.delete(f"{KEYS}/{record['id']}")
        assert revoked.status_code == 200 and again.status_code == 404
        async with harness.caller(key) as caller:
            refused = await caller.post("/v1/chat/completions", json=COMPLETION)
        assert refused.status_code == 401
        assert refused.json()["detail"] == "Account key is invalid or revoked"
        assert (await session.get(ME)).json()["keys"] == []
    assert harness.fleet.calls == 1


@pytest.mark.asyncio
async def test_key_caps_and_name_uniqueness(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated() as (account, session):
        for n in range(20):
            created = await session.post(KEYS, json={"name": f"key-{n}"})
            assert created.status_code == 201
        capped = await session.post(KEYS, json={"name": "one-more"})
        assert capped.status_code == 409
        assert (
            capped.json()["detail"]
            == "This account already has 20 keys. Revoke one first."
        )
        keys = (await session.get(ME)).json()["keys"]
        assert (await session.delete(f"{KEYS}/{keys[0]['id']}")).status_code == 200
        duplicate = await session.post(KEYS, json={"name": "key-1"})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"] == "A key with this name already exists"
        assert (await session.post(KEYS, json={"name": "   "})).status_code == 422
        assert (await session.post(KEYS, json={"name": "k" * 61})).status_code == 422
        assert (await session.post(KEYS, json={"name": "fresh"})).status_code == 201
    assert len(harness.store.account_keys(account["id"])) == 20


@pytest.mark.asyncio
async def test_me_reports_routes_usage_and_limits_matching_v1_models(tmp_path):
    harness = await build(tmp_path)
    async with harness.activated("bob", "Bob") as (bob, bob_session):
        phone = (await bob_session.post(KEYS, json={"name": "phone"})).json()["key"]
        async with harness.caller(phone) as caller:
            assert (await caller.get("/v1/models")).status_code == 200
    async with harness.activated() as (_, session):
        key = (await session.post(KEYS, json={"name": "laptop"})).json()["key"]
        first = (await session.get(ME)).json()
        async with harness.caller(key) as caller:
            catalog = (await caller.get("/v1/models")).json()
            completed = await caller.post("/v1/chat/completions", json=COMPLETION)
        assert completed.status_code == 200
        second = (await session.get(ME)).json()
    assert set(first["account"]) == {
        "id",
        "username",
        "name",
        "status",
        "created",
        "activated_at",
        "last_login",
    }
    assert first["level"]["name"] == "Standard"
    assert {route["name"] for route in first["level"]["routes"]} == {
        "free",
        "private",
        "cloud-only",
    }
    assert (
        {route["name"] for route in first["level"]["routes"] if route["ready"]}
        == {item["id"] for item in catalog["data"]}
        == {"free", "private"}
    )
    assert first["level"]["token_budget"] is None
    assert first["level"]["max_concurrency"] is None
    assert first["usage"]["used"] == 0 and first["usage"]["max_tokens"] is None
    assert (
        second["usage"]["used"] == USAGE["prompt_tokens"] + USAGE["completion_tokens"]
    )
    assert second["usage"]["active_requests"] == 0
    assert first["devices"] == {"observed": [], "registered": None}
    (observed,) = second["devices"]["observed"]
    assert observed["source_address"] == "192.0.2.50"
    assert observed["via"] == "key" and observed["credential_name"] == "laptop"
    assert observed["software"] == "python-requests/2.33.0"
    assert observed["last_path"] == "/v1/chat/completions"
    assert second["source_address"] == "127.0.0.1"
    assert second["base_url"] == "http://localhost/v1"
    assert "phone" not in json.dumps(second) and bob["id"] not in json.dumps(second)


@pytest.mark.asyncio
async def test_portal_is_locked_when_accounts_disabled_even_with_valid_cookie(
    tmp_path,
):
    harness = await build(tmp_path)
    async with harness.activated() as (account, session):
        assert (await session.get(STATUS)).json() == {
            "enabled": True,
            "device_registration_enabled": False,
            "signed_in": True,
        }
        harness.set_enabled(False)
        status = await session.get(STATUS)
        locked = await session.get(ME)
        create = await session.post(KEYS, json={"name": "laptop"})
        login = await harness.browser().post(
            LOGIN, json={"username": "alice", "password": PASSWORD}
        )
        assert status.json() == {
            "enabled": False,
            "device_registration_enabled": False,
            "signed_in": False,
        }
        for response in (locked, create, login):
            assert response.status_code == 403
            assert response.json()["detail"] == DISABLED
        harness.set_enabled(True)
        assert (await session.get(ME)).status_code == 200
        assert (await session.get(STATUS)).json()["signed_in"] is True
    assert harness.store.account_keys(account["id"]) == []


@pytest.mark.asyncio
async def test_portal_paths_serve_index_html_and_body_limits_apply(
    tmp_path, monkeypatch
):
    assets = tmp_path / "dist"
    assets.mkdir()
    (assets / "index.html").write_text("<html>portal</html>")
    monkeypatch.setenv("MODEL_ROUTER_UI", str(assets))
    app = create_app(str(tmp_path / "state"), background=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url=ORIGIN,
    ) as http:
        for path in ("/portal", "/portal/keys", "/portal/activate"):
            page = await http.get(path)
            assert page.status_code == 200 and page.text == "<html>portal</html>"
            assert page.headers["content-type"].startswith("text/html")
        (assets / "index.html").unlink()
        assert (await http.get("/portal")).status_code == 503
        assert (await http.get("/portal/keys")).status_code == 503
        large = b"x" * 9000
        assert (await http.post(ACTIVATE, content=large)).status_code == 413
        assert (await http.post(LOGIN, content=large)).status_code == 413
        assert (await http.put(PASSWORD_PATH, content=large)).status_code == 413
        assert (
            await http.post("/api/portal/activate", content=large)
        ).status_code == 413
        assert (await http.post(KEYS, content=large)).status_code != 413


@pytest.mark.asyncio
async def test_no_response_or_row_contains_secret_material(tmp_path):
    harness = await build(tmp_path)
    account, token = await harness.add_account()
    responses = []
    async with harness.browser() as session:
        responses.append(
            await session.post(ACTIVATE, json={"token": token, "password": PASSWORD})
        )
        created = await session.post(KEYS, json={"name": "laptop"})
        key = created.json()["key"]
        responses += [
            await session.get(ME),
            await session.get(STATUS),
            await session.put(
                PASSWORD_PATH, json={"current": PASSWORD, "new": NEW_PASSWORD}
            ),
        ]
        cookie = session.cookies["router_portal"]
    async with harness.operator() as admin:
        responses += [
            await admin.get("/api/v1/accounts"),
            await admin.get(f"/api/v1/accounts/{account['id']}"),
            await admin.get("/api/v1/state"),
        ]
    secrets = (token, key, PASSWORD, NEW_PASSWORD, cookie)
    for response in responses:
        assert response.status_code == 200, response.text
        for secret in secrets + ("$argon2id$",):
            assert secret not in response.text
    dump = harness.dump()
    for secret in secrets:
        assert secret not in dump
    assert hashlib.sha256(cookie.encode()).hexdigest() in dump
    verifier_tables = {"account_credentials", "account_keys", "operator_identity"}
    for line in dump.splitlines():
        if "$argon2id$" in line:
            assert line.startswith("INSERT INTO ")
            assert line.split('"')[1] in verifier_tables, line[:60]
