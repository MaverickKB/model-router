from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from .engine_identity import merged_configuration
from .migration import configuration as migrate_configuration
from .network.report import write_json
from .schema import Configuration
from .security.credentials import CredentialCipher, digest, verify


class Conflict(Exception):
    pass


class Store:
    """Atomic operator configuration and request metadata. Credentials never enter views."""

    def __init__(self, directory: str):
        root = Path(directory)
        self.directory = root
        self.discovery_directory = Path(
            os.environ.get("MODEL_ROUTER_DISCOVERY_STATE", str(root / "discovery"))
        )
        self.discovery_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous_report = root / "network.json"
        if (
            previous_report.exists()
            and not (self.discovery_directory / "network.json").exists()
        ):
            shutil.copy2(previous_report, self.discovery_directory / "network.json")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        path = root / "router.db"
        fresh_install = not path.exists()
        self.cipher = CredentialCipher(root)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS client_keys (digest TEXT PRIMARY KEY, client_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS client_credentials (lookup TEXT PRIMARY KEY, verifier TEXT NOT NULL, client_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operator_sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operator_identity (id INTEGER PRIMARY KEY, verifier TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS installation (id INTEGER PRIMARY KEY CHECK (id=1), setup_complete INTEGER NOT NULL CHECK (setup_complete IN (0,1)));
            CREATE TABLE IF NOT EXISTS callers (id TEXT PRIMARY KEY, seen REAL NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL);
        """)
        self.db.execute(
            "INSERT OR IGNORE INTO installation VALUES (1, ?)",
            (0 if fresh_install else 1,),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO config VALUES (1, ?)",
            (Configuration().model_dump_json(),),
        )
        self.db.commit()
        raw = json.loads(
            self.db.execute("SELECT body FROM config WHERE id=1").fetchone()[0]
        )
        self._config = Configuration.model_validate(migrate_configuration(raw))
        self.db.execute(
            "UPDATE config SET body=? WHERE id=1", (self._config.model_dump_json(),)
        )
        self.db.commit()
        self._setup_complete = bool(
            self.db.execute(
                "SELECT setup_complete FROM installation WHERE id=1"
            ).fetchone()[0]
        )
        self.publish_discovery_policy()
        self._secrets = {}
        migrated = False
        for owner, value in self.db.execute("SELECT id, value FROM secrets").fetchall():
            plain = self.cipher.decrypt(value) if value.startswith("fernet:") else value
            if owner == "operator":
                self.db.execute(
                    "INSERT OR IGNORE INTO operator_identity VALUES (1, ?)",
                    (digest(plain),),
                )
                self.db.execute("DELETE FROM secrets WHERE id=?", (owner,))
                migrated = True
            else:
                self._secrets[owner] = plain
                if not value.startswith("fernet:"):
                    self.db.execute(
                        "UPDATE secrets SET value=? WHERE id=?",
                        (self.cipher.encrypt(plain), owner),
                    )
                    migrated = True
        self.db.commit()
        if migrated:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("VACUUM")
        self._key_owners = {
            row[0]
            for row in self.db.execute(
                "SELECT client_id FROM client_credentials UNION SELECT client_id FROM client_keys"
            )
        }

    def publish_discovery_policy(self):
        write_json(
            self.discovery_directory / "policy.json",
            self._config.discovery.model_dump(),
        )

    def operator_verifier(self) -> str:
        with self.lock:
            row = self.db.execute(
                "SELECT verifier FROM operator_identity WHERE id=1"
            ).fetchone()
            return row[0] if row else ""

    def set_operator_verifier(self, verifier: str):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO operator_identity VALUES (1, ?)", (verifier,)
            )
            self.db.commit()

    def clear_operator_sessions(self):
        with self.lock:
            self.db.execute("DELETE FROM operator_sessions")
            self.db.commit()

    def operator_sessions(self) -> dict[str, float]:
        with self.lock:
            self.db.execute(
                "DELETE FROM operator_sessions WHERE expires <= ?", (time.time(),)
            )
            self.db.commit()
            return dict(
                self.db.execute(
                    "SELECT digest, expires FROM operator_sessions ORDER BY expires LIMIT 128"
                )
            )

    def save_operator_session(self, key: str, expires: float):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO operator_sessions VALUES (?, ?)", (key, expires)
            )
            self.db.commit()

    def revoke_operator_session(self, key: str):
        with self.lock:
            self.db.execute("DELETE FROM operator_sessions WHERE digest=?", (key,))
            self.db.commit()

    def interrupt_unfinished(self):
        with self.lock:
            for event in self.events(limit=100000):
                if event.get("status") in {"routing", "waiting", "running"}:
                    event.update(
                        status="interrupted",
                        elapsed_ms=round((time.time() - event["ts"]) * 1000),
                    )
                    self.event(event)

    def config(self) -> Configuration:
        # Policy reads on the request path do not perform SQLite I/O.
        return self._config.model_copy(deep=True)

    @property
    def setup_required(self) -> bool:
        """Whether this state has not completed its first owner setup action."""
        return not self._setup_complete

    def complete_setup(self):
        with self.lock:
            if self._setup_complete:
                return
            with self.db:
                self.db.execute(
                    "UPDATE installation SET setup_complete=1 WHERE id=1"
                )
            self._setup_complete = True

    def save(
        self, config: Configuration, *, engine_secrets: dict[str, str] | None = None
    ) -> Configuration:
        with self.lock:
            setup_required = not self._setup_complete
            current = Configuration.model_validate_json(
                self.db.execute("SELECT body FROM config WHERE id=1").fetchone()[0]
            )
            if config.revision != current.revision:
                raise Conflict("Settings changed elsewhere. Refresh before saving.")
            config = config.model_copy(
                update={
                    "revision": current.revision + 1,
                    "upgraded_from_schema": current.upgraded_from_schema,
                }
            )
            next_secrets = dict(self._secrets)
            next_key_owners = set(self._key_owners)
            # Commit every identity change together. Publish cache changes only
            # after commit so a failed write cannot leave a half-merged engine.
            with self.db:
                for removed in {e.id for e in current.engines} - {
                    e.id for e in config.engines
                }:
                    self.db.execute("DELETE FROM secrets WHERE id=?", (removed,))
                    next_secrets.pop(removed, None)
                for removed in {c.id for c in current.clients} - {
                    c.id for c in config.clients
                }:
                    self.db.execute(
                        "DELETE FROM client_keys WHERE client_id=?", (removed,)
                    )
                    self.db.execute(
                        "DELETE FROM client_credentials WHERE client_id=?", (removed,)
                    )
                    next_key_owners.discard(removed)
                self.db.execute(
                    "UPDATE config SET body=? WHERE id=1", (config.model_dump_json(),)
                )
                if setup_required:
                    self.db.execute(
                        "UPDATE installation SET setup_complete=1 WHERE id=1"
                    )
                for owner, value in (engine_secrets or {}).items():
                    if value:
                        self.db.execute(
                            "INSERT OR REPLACE INTO secrets VALUES (?, ?)",
                            (owner, self.cipher.encrypt(value)),
                        )
                        next_secrets[owner] = value
                    else:
                        self.db.execute("DELETE FROM secrets WHERE id=?", (owner,))
                        next_secrets.pop(owner, None)
            self._secrets = next_secrets
            self._key_owners = next_key_owners
            self._setup_complete = True
            self._config = config.model_copy(deep=True)
            self.publish_discovery_policy()
            return config

    def merge_engines(self, request):
        with self.lock:
            config = merged_configuration(self.config(), request)
            owner = (
                request.target_id
                if request.credential_source == "target"
                else request.source_id
            )
            secret = "" if request.credential_source == "none" else self.secret(owner)
            return self.save(config, engine_secrets={request.target_id: secret})

    def secret(self, engine_id: str) -> str:
        return self._secrets.get(engine_id, "")

    def set_secret(self, engine_id: str, value: str):
        if engine_id == "operator":
            raise ValueError("Operator identity is separate from provider credentials")
        with self.lock:
            if value:
                self.db.execute(
                    "INSERT OR REPLACE INTO secrets VALUES (?, ?)",
                    (engine_id, self.cipher.encrypt(value)),
                )
                self._secrets[engine_id] = value
            else:
                self.db.execute("DELETE FROM secrets WHERE id=?", (engine_id,))
                self._secrets.pop(engine_id, None)
            self.db.commit()

    def issue_key(self, client_id: str) -> str:
        key = "mr_" + secrets.token_urlsafe(32)
        self.add_key(client_id, key)
        return key

    def add_key(self, client_id: str, key: str):
        verifier = digest(key)
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO client_credentials VALUES (?, ?, ?)",
                (self.cipher.lookup(key), verifier, client_id),
            )
            self.db.commit()
            self._key_owners.add(client_id)

    def key_client(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT verifier, client_id FROM client_credentials WHERE lookup=?",
                (self.cipher.lookup(key),),
            ).fetchone()
        if row:
            return row[1] if verify(row[0], key) else None
        # Existing high-entropy API keys remain valid. On their first successful
        # use, replace the legacy verifier without changing the caller's key.
        old_digest = hashlib.sha256(key.encode()).hexdigest()
        with self.lock:
            legacy = self.db.execute(
                "SELECT digest, client_id FROM client_keys WHERE digest=?",
                (old_digest,),
            ).fetchone()
        if legacy and hmac.compare_digest(legacy[0], old_digest):
            self.add_key(legacy[1], key)
            with self.lock:
                self.db.execute("DELETE FROM client_keys WHERE digest=?", (old_digest,))
                self.db.commit()
            return legacy[1]
        return None

    def revoke_keys(self, client_id: str):
        with self.lock:
            self.db.execute(
                "DELETE FROM client_credentials WHERE client_id=?", (client_id,)
            )
            self.db.execute("DELETE FROM client_keys WHERE client_id=?", (client_id,))
            self.db.commit()
            self._key_owners.discard(client_id)

    def has_key(self, client_id: str) -> bool:
        return client_id in self._key_owners

    def event(self, event: dict):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO events VALUES (?, ?, ?)",
                (event["id"], event["ts"], json.dumps(event)),
            )
            self.db.execute(
                "DELETE FROM events WHERE ts < ?", (time.time() - 7 * 86400,)
            )
            self.db.commit()

    def events(self, limit=100) -> list[dict]:
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM events ORDER BY ts DESC LIMIT ?", (limit,)
                )
            ]

    def observe_caller(self, caller: dict):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
                (caller["id"], caller["last_seen"], json.dumps(caller)),
            )
            self.db.execute(
                "DELETE FROM callers WHERE seen < ?", (time.time() - 7 * 86400,)
            )
            self.db.execute(
                "DELETE FROM callers WHERE id NOT IN (SELECT id FROM callers ORDER BY seen DESC LIMIT 1000)"
            )

    def callers(self) -> list[dict]:
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM callers WHERE seen >= ? ORDER BY seen DESC LIMIT 1000",
                    (time.time() - 7 * 86400,),
                )
            ]
