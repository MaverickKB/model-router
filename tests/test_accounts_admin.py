"""Operator endpoints manage accounts and activation links; secrets are issued once."""

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from gateway.accounts.api import USERNAME_RULE
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

USAGE = {"prompt_tokens": 12, "completion_tokens": 30}
PASSWORD = "correct horse battery"
TABLES = (
    "accounts",
    "account_credentials",
    "account_activations",
    "account_keys",
    "portal_sessions",
    "registered_devices",
    "usage_windows",
)


class Fleet:
    def __init__(self):
        self.calls = 0
        self.hold: tuple[asyncio.Event, asyncio.Event] | None = None

    async def handle(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        self.calls += 1
        if self.hold:
            # Park the completion so a test can act while it is in flight.
            entered, release = self.hold
            entered.set()
            await release.wait()
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
    def __init__(self, app, fleet, level, cloud):
        self.app, self.fleet, self.level, self.cloud = app, fleet, level, cloud
        self.store = app.state.store

    def browser(self, origin=True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"} if origin else {},
        )

    @asynccontextmanager
    async def operator(self, origin=True):
        token = (self.store.directory / "operator-bootstrap.key").read_text().strip()
        async with self.browser(origin) as http:
            login = await http.post(
                "/api/login",
                json={"token": token},
                headers={"Origin": "http://localhost"},
            )
            assert login.status_code == 200
            yield http

    def caller(self, key):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("192.0.2.50", 1)),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        )

    async def add_account(self, http, username="alice", name="Alice"):
        created = await http.post(
            "/api/v1/accounts",
            json={"username": username, "name": name, "level_id": self.level.id},
        )
        assert created.status_code == 201, created.text
        return created.json()["account"], created.json()["activation"]

    def activate(self, token) -> str | None:
        return self.store.activate_account(token, digest(PASSWORD))

    def rows(self, account_id) -> dict[str, int]:
        return {
            table: self.store.db.execute(
                f"SELECT count(*) FROM {table} WHERE {column}=?", (account_id,)
            ).fetchone()[0]
            for table, column in (
                (table, "id" if table == "accounts" else "account_id")
                for table in TABLES
            )
        }

    def dump(self) -> str:
        return "\n".join(self.store.db.iterdump())


async def build(tmp_path, *, enabled=True, levels=True) -> Harness:
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
    level = AccountLevel(name="Standard", route_names=["free", "private"])
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
            accounts=AccountsSettings(enabled=enabled),
            account_levels=[level] if levels else [],
        )
    )
    app.state.store.set_secret(cloud.id, "demo-provider-key")
    await app.state.discovery.refresh()
    return Harness(app, fleet, level, cloud)


@pytest.mark.asyncio
async def test_admin_can_prepare_levels_and_accounts_while_accounts_are_disabled(
    tmp_path,
):
    harness = await build(tmp_path, enabled=False, levels=False)
    async with harness.operator() as http:
        config = harness.store.config().model_dump()
        config["account_levels"] = [harness.level.model_dump()]
        saved = await http.put("/api/v1/config", json=config)
        assert saved.status_code == 200, saved.text
        account, activation = await harness.add_account(http)
        listed = await http.get("/api/v1/accounts")
    assert harness.store.config().accounts.enabled is False
    assert account["status"] == "pending" and account["activation_pending"] is True
    assert activation["url"].startswith("http://localhost/portal/activate#token=mra_")
    assert activation["expires"] > time.time() + 71 * 3600
    assert [row["username"] for row in listed.json()["accounts"]] == ["alice"]
    assert listed.json()["accounts"][0]["usage"]["max_tokens"] is None


@pytest.mark.asyncio
async def test_create_account_returns_activation_link_once_and_nothing_else_carries_it(
    tmp_path,
):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        token = activation["token"]
        assert activation["url"].endswith(f"#token={token}")
        responses = [
            await http.get("/api/v1/accounts"),
            await http.get(f"/api/v1/accounts/{account['id']}"),
            await http.get("/api/v1/state"),
        ]
    for response in responses:
        assert response.status_code == 200
        assert token not in response.text
    detail = responses[1].json()
    assert detail["account"]["activation_pending"] is True
    assert detail["account"]["activation_expires"] == pytest.approx(
        activation["expires"]
    )
    assert detail["keys"] == [] and detail["devices"] == []
    assert detail["usage_windows"] == []
    dump = harness.dump()
    assert token not in dump
    assert hashlib.sha256(token.encode()).hexdigest() in dump


@pytest.mark.asyncio
async def test_username_validation_and_duplicate(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        outcomes = {}
        for username in ("Alice!", "a", "alice", "ALICE", " Bob.Smith-2 "):
            response = await http.post(
                "/api/v1/accounts",
                json={"username": username, "level_id": harness.level.id},
            )
            outcomes[username] = (response.status_code, response.json())
        unknown_level = await http.post(
            "/api/v1/accounts", json={"username": "carol", "level_id": "missing"}
        )
        extra_field = await http.post(
            "/api/v1/accounts",
            json={"username": "dave", "level_id": harness.level.id, "status": "active"},
        )
    assert outcomes["Alice!"] == (422, {"detail": USERNAME_RULE})
    assert outcomes["a"] == (422, {"detail": USERNAME_RULE})
    assert outcomes["alice"][0] == 201
    assert outcomes["alice"][1]["account"]["name"] == "alice"
    assert outcomes["ALICE"] == (409, {"detail": "Username is already taken"})
    assert outcomes[" Bob.Smith-2 "][0] == 201
    assert outcomes[" Bob.Smith-2 "][1]["account"]["username"] == "bob.smith-2"
    assert unknown_level.status_code == 422
    assert unknown_level.json()["detail"] == "Level does not exist"
    assert extra_field.status_code == 422


@pytest.mark.asyncio
async def test_reissued_activation_replaces_previous_and_suspended_accounts_cannot_get_one(
    tmp_path,
):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, first = await harness.add_account(http)
        path = f"/api/v1/accounts/{account['id']}/activation"
        second = (await http.post(path)).json()
        assert harness.activate(first["token"]) is None
        assert harness.activate(second["token"]) == account["id"]
        assert harness.store.account_snapshot(account["id"])["status"] == "active"
        reset = await http.post(path)
        assert reset.status_code == 200
        assert harness.store.activation_for(account["id"])["purpose"] == "reset"
        with_reset = (await http.get(f"/api/v1/accounts/{account['id']}")).json()
        assert with_reset["account"]["status"] == "active"
        assert with_reset["account"]["activation_pending"] is False
        assert with_reset["account"]["activation_expires"] == reset.json()["expires"]
        assert harness.store.activate_account(reset.json()["token"], digest("new one"))
        suspended = await http.put(
            f"/api/v1/accounts/{account['id']}", json={"status": "suspended"}
        )
        refused = await http.post(path)
        missing = await http.post("/api/v1/accounts/nope/activation")
    assert suspended.status_code == 200
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Enable the account before issuing a link"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_expired_activation_link_is_not_reported_as_pending(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, _ = await harness.add_account(http)
        listed = (await http.get("/api/v1/accounts")).json()["accounts"][0]
        assert listed["activation_pending"] is True
        with harness.store.db:
            harness.store.db.execute(
                "UPDATE account_activations SET expires=? WHERE account_id=?",
                (time.time() - 1, account["id"]),
            )
        lapsed = (await http.get("/api/v1/accounts")).json()["accounts"][0]
    assert lapsed["status"] == "pending"
    assert lapsed["activation_pending"] is False
    assert lapsed["activation_expires"] < time.time()


@pytest.mark.asyncio
async def test_suspended_pending_account_returns_to_pending_and_activates(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, first = await harness.add_account(http)
        path = f"/api/v1/accounts/{account['id']}"
        suspended = await http.put(path, json={"status": "suspended"})
        assert suspended.status_code == 200
        assert harness.activate(first["token"]) is None
        refused = await http.post(f"{path}/activation")
        assert refused.status_code == 409
        restored = await http.put(path, json={"status": "active"})
        assert restored.status_code == 200
        assert restored.json()["account"]["status"] == "pending"
        # The link issued before suspension stays dead; a fresh one is required.
        assert restored.json()["account"]["activation_pending"] is False
        assert harness.store.activation_for(account["id"]) is None
        assert harness.activate(first["token"]) is None
        link = await http.post(f"{path}/activation")
        assert link.status_code == 200
        assert harness.store.activation_for(account["id"])["purpose"] == "activate"
        assert harness.activate(link.json()["token"]) == account["id"]
        assert harness.store.account_snapshot(account["id"])["status"] == "active"


@pytest.mark.asyncio
async def test_put_status_active_on_pending_is_422_and_suspend_drops_sessions(
    tmp_path,
):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        path = f"/api/v1/accounts/{account['id']}"
        premature = await http.put(path, json={"status": "active"})
        assert premature.status_code == 422
        assert (
            premature.json()["detail"]
            == "Activate the account with its link before enabling it"
        )
        assert harness.activate(activation["token"]) == account["id"]
        key, _ = harness.store.create_account_key(account["id"], "laptop")
        harness.store.save_portal_session("digest-a", account["id"], time.time() + 3600)
        harness.store.save_portal_session("digest-b", account["id"], time.time() + 3600)
        async with harness.caller(key) as caller:
            assert (await caller.get("/v1/models")).status_code == 200
        suspended = await http.put(
            path, json={"status": "suspended", "name": "Alice S"}
        )
        assert suspended.status_code == 200
        assert suspended.json()["account"]["status"] == "suspended"
        assert suspended.json()["account"]["name"] == "Alice S"
        assert harness.store.portal_sessions() == {}
        async with harness.caller(key) as caller:
            refused = await caller.get("/v1/models")
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Account is suspended"
        restored = await http.put(path, json={"status": "active"})
        assert restored.status_code == 200
        async with harness.caller(key) as caller:
            assert (await caller.get("/v1/models")).status_code == 200
        bad_level = await http.put(path, json={"level_id": "missing"})
        assert bad_level.status_code == 422
        assert bad_level.json()["detail"] == "Level does not exist"
        assert (await http.put("/api/v1/accounts/nope", json={})).status_code == 404


@pytest.mark.asyncio
async def test_delete_account_cascades_via_api(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        account_id = account["id"]
        assert harness.activate(activation["token"]) == account_id
        harness.store.issue_activation(account_id, "reset")
        harness.store.create_account_key(account_id, "laptop")
        harness.store.save_portal_session("digest-a", account_id, time.time() + 3600)
        harness.store.register_device(account_id, "192.0.2.9", "bench")
        harness.store.record_usage(account_id, time.time(), 3600, 1, 1, 0)
        assert all(harness.rows(account_id).values())
        deleted = await http.delete(f"/api/v1/accounts/{account_id}")
        assert deleted.status_code == 200 and deleted.json() == {"ok": True}
        assert not any(harness.rows(account_id).values())
        assert (await http.get(f"/api/v1/accounts/{account_id}")).status_code == 404
        assert (await http.delete(f"/api/v1/accounts/{account_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_during_in_flight_request_records_no_usage(tmp_path):
    harness = await build(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    harness.fleet.hold = (entered, release)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        account_id = account["id"]
        assert harness.activate(activation["token"]) == account_id
        key, _ = harness.store.create_account_key(account_id, "laptop")
        async with harness.caller(key) as caller:
            pending = asyncio.create_task(
                caller.post(
                    "/v1/chat/completions",
                    json={
                        "model": "private",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            deleted = await http.delete(f"/api/v1/accounts/{account_id}")
            assert deleted.status_code == 200
            release.set()
            completed = await pending
    assert completed.status_code == 200
    assert not any(harness.rows(account_id).values())
    assert harness.store.open_usage_windows(time.time()) == []


@pytest.mark.asyncio
async def test_account_summary_usage_reflects_ledger(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        assert harness.activate(activation["token"]) == account["id"]
        key, record = harness.store.create_account_key(account["id"], "laptop")
        async with harness.caller(key) as caller:
            completed = await caller.post(
                "/v1/chat/completions",
                json={
                    "model": "private",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert completed.status_code == 200
        listed = (await http.get("/api/v1/accounts")).json()["accounts"][0]
        detail = (await http.get(f"/api/v1/accounts/{account['id']}")).json()
        revoked = await http.delete(
            f"/api/v1/accounts/{account['id']}/keys/{record['id']}"
        )
        again = await http.delete(
            f"/api/v1/accounts/{account['id']}/keys/{record['id']}"
        )
    used = USAGE["prompt_tokens"] + USAGE["completion_tokens"]
    assert listed["usage"]["used"] == used
    assert listed["usage"]["window_seconds"] == 86400
    assert listed["usage"]["active_requests"] == 0
    assert listed["key_count"] == 1 and listed["device_count"] == 0
    assert listed["activation_pending"] is False
    assert [row["name"] for row in detail["keys"]] == ["laptop"]
    assert key not in json.dumps(detail)
    (window,) = detail["usage_windows"]
    assert window["prompt_tokens"] == USAGE["prompt_tokens"]
    assert window["completion_tokens"] == USAGE["completion_tokens"]
    assert window["requests"] == 1
    assert revoked.status_code == 200 and again.status_code == 404
    assert harness.store.account_keys(account["id"]) == []


@pytest.mark.asyncio
async def test_state_carries_level_counts_not_account_rows(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        before = (await http.get("/api/v1/state")).json()
        await harness.add_account(http)
        await harness.add_account(http, "bob", "Bob")
        state = (await http.get("/api/v1/state")).json()
    assert before["account_levels_in_use"] == {}
    assert state["account_levels_in_use"] == {harness.level.id: 2}
    assert "accounts" not in state
    assert "alice" not in json.dumps(state)


@pytest.mark.asyncio
async def test_admin_endpoints_require_operator_and_same_origin(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, _ = await harness.add_account(http)
    body = {"username": "bob", "level_id": harness.level.id}
    async with harness.browser() as anonymous:
        assert (await anonymous.get("/api/v1/accounts")).status_code == 401
        assert (await anonymous.post("/api/v1/accounts", json=body)).status_code == 401
        deleted = await anonymous.delete(f"/api/v1/accounts/{account['id']}")
        assert deleted.status_code == 401
    async with harness.operator(origin=False) as no_origin:
        assert (await no_origin.get("/api/v1/accounts")).status_code == 200
        create = await no_origin.post("/api/v1/accounts", json=body)
        assert create.status_code == 403
        suspend = await no_origin.put(
            f"/api/v1/accounts/{account['id']}", json={"status": "suspended"}
        )
        assert suspend.status_code == 403
        alias = await no_origin.get("/api/accounts")
        assert alias.status_code == 200
        assert [row["id"] for row in alias.json()["accounts"]] == [account["id"]]
    assert harness.store.account_snapshot(account["id"])["status"] == "pending"
    assert harness.store.account_by_username("bob") is None


@pytest.mark.asyncio
async def test_explain_and_accounts_share_the_derived_principal(tmp_path):
    harness = await build(tmp_path)
    async with harness.operator() as http:
        account, activation = await harness.add_account(http)
        assert harness.activate(activation["token"]) == account["id"]
        key, _ = harness.store.create_account_key(account["id"], "laptop")
        explained = {}
        for route in ("free", "private", "cloud-only"):
            response = await http.post(
                "/api/v1/explain",
                json={"account_id": account["id"], "payload": {"model": route}},
            )
            assert response.status_code == 200
            explained[route] = bool(response.json()["candidates"])
    async with harness.caller(key) as caller:
        catalog = await caller.get("/v1/models")
    assert catalog.status_code == 200
    assert (
        {item["id"] for item in catalog.json()["data"]}
        == {route for route, ready in explained.items() if ready}
        == {"free", "private"}
    )
