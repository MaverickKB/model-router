"""Credential changes win over an in-flight verification or legacy conversion."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from gateway.store import Store


def pause_credential_work(monkeypatch, name, result):
    started, release = Event(), Event()

    def paused(*args):
        started.set()
        assert release.wait(timeout=5), "credential work was not released"
        return result

    monkeypatch.setattr(f"gateway.store.{name}", paused)
    return started, release


@pytest.mark.parametrize("change", ["revoke", "owner", "verifier"])
def test_modern_key_rechecks_current_record_after_verification(
    tmp_path, monkeypatch, change
):
    store = Store(str(tmp_path))
    key = "concurrency-test-key"
    store.add_key("original-owner", key)
    started, release = pause_credential_work(monkeypatch, "verify", True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.key_client, key)
        try:
            assert started.wait(timeout=5)
            if change == "revoke":
                store.revoke_keys("original-owner")
            else:
                column, value = (
                    ("client_id", "replacement-owner")
                    if change == "owner"
                    else ("verifier", "replacement-verifier")
                )
                with store.lock, store.db:
                    store.db.execute(
                        f"UPDATE client_credentials SET {column}=?", (value,)
                    )
        finally:
            release.set()
        assert pending.result(timeout=5) is None


@pytest.mark.parametrize("change", ["revoke", "owner", "modern"])
def test_legacy_conversion_does_not_restore_or_overwrite_changed_credentials(
    tmp_path, monkeypatch, change
):
    store = Store(str(tmp_path))
    key = "legacy-concurrency-key"
    legacy_digest = hashlib.sha256(key.encode()).hexdigest()
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO client_keys VALUES (?, ?)", (legacy_digest, "original-owner")
        )
    store._key_owners.add("original-owner")
    started, release = pause_credential_work(
        monkeypatch, "digest", "converted-verifier"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.key_client, key)
        try:
            assert started.wait(timeout=5)
            if change == "revoke":
                store.revoke_keys("original-owner")
            elif change == "owner":
                with store.lock, store.db:
                    store.db.execute(
                        "UPDATE client_keys SET client_id='replacement-owner'"
                    )
            else:
                with store.lock, store.db:
                    store.db.execute(
                        "INSERT INTO client_credentials VALUES (?, ?, ?)",
                        (store.cipher.lookup(key), "new-verifier", "replacement-owner"),
                    )
        finally:
            release.set()
        assert pending.result(timeout=5) is None
    modern = store.db.execute(
        "SELECT verifier, client_id FROM client_credentials"
    ).fetchall()
    legacy = store.db.execute("SELECT client_id FROM client_keys").fetchall()
    if change == "revoke":
        assert modern == legacy == []
        assert not store.has_key("original-owner")
    elif change == "owner":
        assert modern == []
        assert legacy == [("replacement-owner",)]
    else:
        assert modern == [("new-verifier", "replacement-owner")]
        assert legacy == [("original-owner",)]


def test_legacy_conversion_installs_and_removes_verifiers_atomically(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path))
    key = "legacy-transaction-key"
    legacy_digest = hashlib.sha256(key.encode()).hexdigest()
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO client_keys VALUES (?, ?)", (legacy_digest, "original-owner")
        )
        store.db.execute(
            "CREATE TRIGGER conversion_failure BEFORE DELETE ON client_keys BEGIN SELECT RAISE(ABORT, 'conversion failed'); END"
        )
    monkeypatch.setattr("gateway.store.digest", lambda value: "converted-verifier")
    with pytest.raises(sqlite3.IntegrityError, match="conversion failed"):
        store.key_client(key)
    assert store.db.execute("SELECT * FROM client_credentials").fetchall() == []
    assert store.db.execute("SELECT * FROM client_keys").fetchall() == [
        (legacy_digest, "original-owner")
    ]
