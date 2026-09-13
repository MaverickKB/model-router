"""Account storage: schema v5 migration, tables, caches and credential hygiene."""

import json
import sqlite3
import time

import httpx
import pytest
from pydantic import ValidationError

from gateway.app import create_app
from gateway.engine_identity import MergeEngines, merged_configuration
from gateway.migration import configuration as migrate_configuration
from gateway.schema import (
    AccountLevel,
    Client,
    Configuration,
    Engine,
    Route,
    Selector,
    TokenBudget,
)
from gateway.security.credentials import digest, verify
from gateway.store import Store

ACCOUNT_TABLES = (
    "accounts",
    "account_credentials",
    "account_activations",
    "account_keys",
    "portal_sessions",
    "registered_devices",
    "usage_windows",
)


def v4_blob(**overrides) -> dict:
    raw = Configuration().model_dump()
    raw.pop("accounts")
    raw.pop("account_levels")
    raw["schema_version"] = 4
    raw.update(overrides)
    return raw


def tables(store: Store) -> set[str]:
    return {
        row[0]
        for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def with_levels(store: Store, *levels: AccountLevel) -> Configuration:
    config = store.config().model_copy(update={"account_levels": list(levels)})
    return store.save(config)


def count(store: Store, table: str, account_id: str) -> int:
    column = "id" if table == "accounts" else "account_id"
    return store.db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (account_id,)
    ).fetchone()[0]


def populate(store: Store, level_id: str, username: str) -> tuple[dict, str, dict]:
    """Create an active account with a key, device, session and usage row."""
    account = store.create_account(username, username.title(), level_id)
    token, _ = store.issue_activation(account["id"], "activate")
    assert store.activate_account(token, digest("correct horse battery")) == account["id"]
    key, _record = store.create_account_key(account["id"], "laptop")
    device = store.register_device(account["id"], f"192.0.2.{len(username)}", "box")
    # Issuing a link revokes sessions, so the session is saved afterwards.
    store.issue_activation(account["id"], "reset")
    store.save_portal_session(f"session-{username}", account["id"], time.time() + 60)
    now = time.time()
    store.record_usage(account["id"], now - now % 3600, 3600, 5, 7, 0)
    return store.account_snapshot(account["id"]), key, device


def test_v4_blob_upgrades_to_v5_with_accounts_disabled(tmp_path):
    with sqlite3.connect(tmp_path / "router.db") as db:
        db.execute("CREATE TABLE config(id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO config VALUES(1,?)", (json.dumps(v4_blob()),))
    store = Store(str(tmp_path))
    config = store.config()
    assert config.schema_version == 5
    assert config.accounts.enabled is False
    assert config.accounts.device_registration_enabled is False
    assert config.accounts.session_hours == 168
    assert config.account_levels == []
    assert config.upgraded_from_schema is None
    stored = json.loads(store.db.execute("SELECT body FROM config").fetchone()[0])
    assert stored["schema_version"] == 5
    assert set(ACCOUNT_TABLES) <= tables(store)
    assert store.accounts() == [] and store.devices() == []


@pytest.mark.parametrize("provenance", [None, 0, 3])
def test_upgraded_from_schema_is_preserved_through_v5(provenance):
    migrated = migrate_configuration(v4_blob(upgraded_from_schema=provenance))
    assert migrated["schema_version"] == 5
    assert migrated["upgraded_from_schema"] == provenance
    assert Configuration.model_validate(migrated).upgraded_from_schema == provenance


def test_level_ids_and_names_are_unique():
    shared = AccountLevel(name="Standard").id
    with pytest.raises(ValidationError, match="Record IDs must be unique"):
        Configuration(
            account_levels=[
                AccountLevel(id=shared, name="Standard"),
                AccountLevel(id=shared, name="Other"),
            ]
        )
    with pytest.raises(ValidationError, match="Level names must be unique"):
        Configuration(
            account_levels=[AccountLevel(name="Standard"), AccountLevel(name="standard")]
        )
    level = AccountLevel(
        name="Standard",
        token_budget=TokenBudget(max_tokens=1000, window_seconds=3600),
        max_concurrency=2,
    )
    assert level.route_names == ["auto"] and level.model_patterns == ["*"]
    assert AccountLevel(name="Free").token_budget is None
    with pytest.raises(ValidationError):
        AccountLevel(name="Bad", max_concurrency=0)
    with pytest.raises(ValidationError):
        TokenBudget(max_tokens=1, window_seconds=59)


def test_account_lifecycle_validates_levels_usernames_and_suspension(tmp_path):
    store = Store(str(tmp_path))
    with pytest.raises(ValueError, match="Level does not exist"):
        store.create_account("alice", "Alice", "missing")
    standard, premium = AccountLevel(name="Standard"), AccountLevel(name="Premium")
    with_levels(store, standard, premium)
    account = store.create_account("alice", "Alice", standard.id)
    assert account["status"] == "pending" and account["activated_at"] is None
    assert set(account) == {
        "id",
        "username",
        "name",
        "level_id",
        "status",
        "created",
        "activated_at",
        "last_login",
    }
    assert store.account_by_username("alice") == account
    assert store.account_snapshot(account["id"]) == account
    with pytest.raises(ValueError, match="Username is already taken"):
        store.create_account("alice", "Again", standard.id)
    with pytest.raises(ValueError, match="Level does not exist"):
        store.update_account(account["id"], level_id="missing")
    with pytest.raises(ValueError, match="Account does not exist"):
        store.update_account("missing", name="Nobody")
    with pytest.raises(ValueError, match="Unknown account status"):
        store.update_account(account["id"], status="bogus")
    assert store.account_snapshot(account["id"])["status"] == "pending"
    store.save_portal_session("session", account["id"], time.time() + 60)
    updated = store.update_account(
        account["id"], name="Alice B", level_id=premium.id, status="suspended"
    )
    assert updated["name"] == "Alice B"
    assert updated["level_id"] == premium.id
    assert updated["status"] == "suspended"
    assert store.portal_sessions() == {}
    assert store.level_account_counts() == {premium.id: 1}
    store.touch_login(account["id"])
    assert store.account_snapshot(account["id"])["last_login"] is not None


def test_account_key_is_argon2_with_hmac_lookup_and_returned_once(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    key, record = store.create_account_key(account["id"], "laptop")
    assert key.startswith("mru_") and key not in json.dumps(record)
    assert set(record) == {"id", "name", "created", "last_used"}
    lookup, verifier = store.db.execute(
        "SELECT lookup, verifier FROM account_keys WHERE id=?", (record["id"],)
    ).fetchone()
    assert lookup == store.cipher.lookup(key)
    assert verifier.startswith("$argon2id$")
    assert store.account_key(key) == {
        "account_id": account["id"],
        "key_id": record["id"],
        "key_name": "laptop",
    }
    assert store.account_key("mru_wrong") is None
    assert store.account_key(key[:-1] + ("A" if key[-1] != "A" else "B")) is None
    assert store.account_keys(account["id"])[0]["id"] == record["id"]
    assert key not in json.dumps(store.account_keys(account["id"]))
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert key.encode() not in (tmp_path / "router.db").read_bytes()
    assert store.revoke_account_key("someone-else", record["id"]) is False
    assert store.revoke_account_key(account["id"], record["id"]) is True
    assert store.account_key(key) is None


def test_account_keys_are_capped_and_named_uniquely(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    store.create_account_key(account["id"], "laptop")
    with pytest.raises(ValueError, match="A key with this name already exists"):
        store.create_account_key(account["id"], "laptop")
    for index in range(19):
        store.create_account_key(account["id"], f"key-{index}")
    with pytest.raises(ValueError, match="already has 20 keys"):
        store.create_account_key(account["id"], "one-too-many")
    with pytest.raises(ValueError, match="Account does not exist"):
        store.create_account_key("missing", "orphan")


def test_last_used_write_is_throttled(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    key, record = store.create_account_key(account["id"], "laptop")

    def last_used():
        return store.account_keys(account["id"])[0]["last_used"]

    assert last_used() is None
    store.account_key(key)
    first = last_used()
    assert first is not None
    store.account_key(key)
    assert last_used() == first
    store._key_touched[record["id"]] -= 61
    store.account_key(key)
    assert last_used() > first


def test_account_key_revoked_during_verification_is_refused(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    key, record = store.create_account_key(account["id"], "laptop")
    other = store.create_account("bob", "Bob", level.id)
    other_key, _ = store.create_account_key(other["id"], "phone")

    def revoke_then_verify(verifier, presented):
        store.revoke_account_key(account["id"], record["id"])
        return verify(verifier, presented)

    monkeypatch.setattr("gateway.store.verify", revoke_then_verify)
    assert store.account_key(key) is None
    assert record["id"] not in store._key_touched
    assert store.account_keys(account["id"]) == []

    def delete_then_verify(verifier, presented):
        store.delete_account(other["id"])
        return verify(verifier, presented)

    monkeypatch.setattr("gateway.store.verify", delete_then_verify)
    assert store.account_key(other_key) is None
    assert store.account_snapshot(other["id"]) is None


def test_activation_token_is_one_time_and_expires(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    store.save_portal_session("stale", account["id"], time.time() + 60)
    with pytest.raises(ValueError, match="Unknown activation purpose"):
        store.issue_activation(account["id"], "bogus")
    assert store.activation_for(account["id"]) is None
    token, expires = store.issue_activation(account["id"], "activate")
    assert token.startswith("mra_")
    assert abs(expires - (time.time() + 72 * 3600)) < 5
    assert store.portal_sessions() == {}
    assert store.activation_for(account["id"]) == {
        "expires": expires,
        "purpose": "activate",
    }
    assert token not in json.dumps(store.db.execute(
        "SELECT * FROM account_activations"
    ).fetchall())
    assert store.activate_account("mra_wrong", digest("pw")) is None
    assert store.activate_account(token, digest("correct horse battery")) == account["id"]
    activated = store.account_snapshot(account["id"])
    assert activated["status"] == "active" and activated["activated_at"] is not None
    assert verify(store.account_verifier(account["id"]), "correct horse battery")
    assert store.activate_account(token, digest("again")) is None
    assert store.activation_for(account["id"]) is None

    old_token, _ = store.issue_activation(account["id"], "reset")
    new_token, _ = store.issue_activation(account["id"], "reset")
    assert store.activate_account(old_token, digest("replaced")) is None
    store.db.execute(
        "UPDATE account_activations SET expires=? WHERE account_id=?",
        (time.time() - 1, account["id"]),
    )
    store.db.commit()
    assert store.activate_account(new_token, digest("expired")) is None
    assert verify(store.account_verifier(account["id"]), "correct horse battery")

    reset_token, _ = store.issue_activation(account["id"], "reset")
    assert store.activate_account(reset_token, digest("new password")) == account["id"]
    assert verify(store.account_verifier(account["id"]), "new password")
    assert store.account_snapshot(account["id"])["activated_at"] == (
        activated["activated_at"]
    )
    store.set_account_verifier(account["id"], digest("changed"))
    assert verify(store.account_verifier(account["id"]), "changed")
    assert store.account_verifier("missing") is None


def test_activation_is_refused_while_suspended(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    account = store.create_account("alice", "Alice", level.id)
    token, _ = store.issue_activation(account["id"], "activate")
    store.update_account(account["id"], status="suspended")
    assert store.activate_account(token, digest("pw")) is None
    assert store.account_snapshot(account["id"])["status"] == "suspended"
    store.update_account(account["id"], status="pending")
    assert store.activate_account(token, digest("pw")) == account["id"]


def test_removing_a_referenced_level_is_refused_with_count(tmp_path):
    store = Store(str(tmp_path))
    standard, premium = AccountLevel(name="Standard"), AccountLevel(name="Premium")
    saved = with_levels(store, standard, premium)
    for username in ("alice", "bob"):
        store.create_account(username, username.title(), standard.id)
    carol = store.create_account("carol", "Carol", premium.id)
    revision = saved.revision
    with pytest.raises(
        ValueError, match=r"Move 2 account\(s\) off level 'Standard' before removing it"
    ):
        store.save(saved.model_copy(update={"account_levels": [premium]}))
    assert [level.id for level in store.config().account_levels] == [
        standard.id,
        premium.id,
    ]
    assert store.config().revision == revision
    store.update_account(carol["id"], level_id=standard.id)
    kept = store.save(saved.model_copy(update={"account_levels": [standard]}))
    assert [level.id for level in kept.account_levels] == [standard.id]
    assert kept.revision == revision + 1


@pytest.mark.asyncio
async def test_put_config_maps_value_error_to_422(tmp_path):
    app = create_app(str(tmp_path), background=False)
    store = app.state.store
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    store.create_account("alice", "Alice", level.id)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        ) as browser,
    ):
        config = (await browser.get("/api/v1/state")).json()["config"]
        assert config["accounts"] == {
            "enabled": False,
            "device_registration_enabled": False,
            "session_hours": 168,
        }
        assert [item["id"] for item in config["account_levels"]] == [level.id]
        refused = await browser.put(
            "/api/v1/config", json={**config, "account_levels": []}
        )
        assert refused.status_code == 422
        assert refused.json()["detail"] == (
            "Move 1 account(s) off level 'Standard' before removing it"
        )
        assert store.config().account_levels[0].id == level.id
        accepted = await browser.put("/api/v1/config", json=config)
        assert accepted.status_code == 200
        assert accepted.json()["revision"] == config["revision"] + 1


def test_delete_account_cascades_every_table(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    alice, alice_key, alice_device = populate(store, level.id, "alice")
    bob, bob_key, bob_device = populate(store, level.id, "bob")
    for table in ACCOUNT_TABLES:
        assert count(store, table, alice["id"]) == 1, table
    alice_key_id = store.account_key(alice_key)["key_id"]
    assert alice_key_id in store._key_touched
    store.delete_account(alice["id"])
    for table in ACCOUNT_TABLES:
        assert count(store, table, alice["id"]) == 0, table
        assert count(store, table, bob["id"]) == 1, table
    assert alice_key_id not in store._key_touched
    assert store.account_snapshot(alice["id"]) is None
    assert store.account_by_username("alice") is None
    assert store.account_key(alice_key) is None
    assert store.device_at(alice_device["address"]) is None
    assert store.level_account_counts() == {level.id: 1}
    assert store.account_key(bob_key)["account_id"] == bob["id"]
    assert store.device_at(bob_device["address"])["account_id"] == bob["id"]


def test_store_restart_preserves_accounts_keys_devices_sessions_and_usage(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    alice, key, device = populate(store, level.id, "alice")
    store.db.close()
    restored = Store(str(tmp_path))
    assert restored.config().account_levels[0].id == level.id
    assert restored.accounts() == [alice]
    assert restored.account_key(key)["account_id"] == alice["id"]
    assert restored.device_at(device["address"]) == device
    assert restored.device_snapshot(device["id"]) == device
    assert restored.devices_for(alice["id"]) == [device]
    assert restored.portal_sessions() == {
        "session-alice": (alice["id"], pytest.approx(time.time() + 60, abs=5))
    }
    assert restored.activation_for(alice["id"])["purpose"] == "reset"
    assert verify(restored.account_verifier(alice["id"]), "correct horse battery")
    assert restored.usage_windows(alice["id"], days=10**6)[0]["requests"] == 1


def test_usage_windows_upsert_add_and_prune(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()
    start = now - (now % 3600)
    store.record_usage("acct", start, 3600, 10, 20, 0)
    store.record_usage("acct", start, 3600, 5, 5, 2)
    (row,) = store.usage_windows("acct")
    assert row == {
        "account_id": "acct",
        "window_start": start,
        "window_seconds": 3600,
        "prompt_tokens": 15,
        "completion_tokens": 25,
        "estimated_tokens": 2,
        "requests": 2,
    }
    stale = now - 36 * 86400
    store.db.execute(
        "INSERT INTO usage_windows VALUES ('acct', ?, 3600, 1, 1, 0, 1)", (stale,)
    )
    store.db.commit()
    store.record_usage("other", start, 86400, 1, 1, 0)
    starts = [r[0] for r in store.db.execute("SELECT window_start FROM usage_windows")]
    assert stale not in starts and len(starts) == 2
    assert {w["account_id"] for w in store.open_usage_windows(now)} == {"acct", "other"}
    assert store.open_usage_windows(now + 86400 * 2) == []
    store.record_usage("acct", start - 8 * 86400, 3600, 1, 1, 0)
    assert len(store.usage_windows("acct", days=7)) == 1
    assert len(store.usage_windows("acct", days=9)) == 2


def test_device_address_is_canonical_and_globally_unique(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    with_levels(store, level)
    alice = store.create_account("alice", "Alice", level.id)
    bob = store.create_account("bob", "Bob", level.id)
    device = store.register_device(alice["id"], "::FFFF:192.0.2.1", "Desk")
    # The IPv4-mapped form names the same host as the plain IPv4 literal.
    canonical = "192.0.2.1"
    assert device["address"] == canonical and device["enabled"] is True
    assert set(device) == {
        "id",
        "account_id",
        "address",
        "name",
        "enabled",
        "created",
        "last_matched",
    }
    assert store.device_at(canonical) == device
    for same_host in ("::ffff:192.0.2.1", "192.0.2.1"):
        with pytest.raises(ValueError, match="This address is already registered"):
            store.register_device(bob["id"], same_host, "Same host")
    assert store.register_device(alice["id"], "2001:DB8::1", "v6")["address"] == (
        "2001:db8::1"
    )
    with pytest.raises(ValueError):
        store.register_device(bob["id"], "192.0.2.0/24", "Not a host")
    with pytest.raises(ValueError, match="Account does not exist"):
        store.register_device("missing", "192.0.2.9", "Orphan")
    for index in range(8):
        store.register_device(alice["id"], f"10.0.0.{index}", f"box-{index}")
    with pytest.raises(ValueError, match="already has 10 registered devices"):
        store.register_device(alice["id"], "10.0.1.1", "one-too-many")
    assert len(store.devices_for(alice["id"])) == 10
    assert store.set_device_enabled(device["id"], False)["enabled"] is False
    assert store.device_at(canonical)["enabled"] is False
    assert store.set_device_enabled("missing", True) is None
    store.touch_device(device["id"])
    assert store.device_snapshot(device["id"])["last_matched"] is not None
    assert store.remove_device(device["id"], bob["id"]) is False
    assert store.remove_device(device["id"], alice["id"]) is True
    assert store.device_at(canonical) is None
    other = store.register_device(bob["id"], "10.0.2.2", "Bob box")
    assert store.remove_device(other["id"]) is True
    assert store.devices_for(bob["id"]) == []


def test_account_ids_never_collide_with_clients_or_levels(tmp_path):
    store = Store(str(tmp_path))
    level = AccountLevel(name="Standard")
    client = Client(name="Configured caller")
    store.save(
        store.config().model_copy(
            update={"account_levels": [level], "clients": [client]}
        )
    )
    account = store.create_account("alice", "Alice", level.id)
    assert account["id"] not in {client.id, level.id}


def test_engine_merge_relinks_level_engine_ids(tmp_path):
    store = Store(str(tmp_path))
    target = Engine(name="Kept", base_url="http://model.test:8000/v1")
    source = Engine(name="Removed", base_url="http://192.0.2.3:8000/v1")
    level = AccountLevel(name="Standard", engine_ids=[source.id])
    config = Configuration(
        engines=[target, source],
        clients=[Client(name="Shared", kind="shared", engine_ids=[source.id])],
        routes=[Route(name="auto", primary=Selector(engine_ids=[source.id]))],
        account_levels=[level],
    )
    merge = MergeEngines(
        revision=config.revision,
        source_id=source.id,
        target_id=target.id,
        preferred_url=target.base_url,
        credential_source="target",
    )
    assert merged_configuration(config, merge).account_levels[0].engine_ids == [
        target.id
    ]
    store.save(config)
    merge = merge.model_copy(update={"revision": store.config().revision})
    saved = store.merge_engines(merge)
    assert [engine.id for engine in saved.engines] == [target.id]
    assert saved.account_levels[0].engine_ids == [target.id]
    assert saved.clients[0].engine_ids == [target.id]
    assert store.config().account_levels[0].engine_ids == [target.id]
