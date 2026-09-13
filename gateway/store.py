from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

from .caller_records import caller_id, merge_observations
from .caller_sources import ensure_source_identity, normalized_address
from .caller_sources import source_identity as caller_source_identity
from .engine_identity import merged_configuration
from .migration import configuration as migrate_configuration
from .network.caller_evidence import DiscoveryCallerEvidence
from .network.report import write_json
from .schema import Configuration
from .security.credentials import CredentialCipher, digest, verify

ACTIVATION_SECONDS = 72 * 3600
MAX_ACCOUNT_KEYS = 20
MAX_ACCOUNT_DEVICES = 10
KEY_TOUCH_SECONDS = 60
ACCOUNT_STATUSES = ("pending", "active", "suspended")
ACTIVATION_PURPOSES = ("activate", "reset")
USAGE_RETENTION_SECONDS = 35 * 86400
ACCOUNT_FIELDS = (
    "id",
    "username",
    "name",
    "level_id",
    "status",
    "created",
    "activated_at",
    "last_login",
)
DEVICE_FIELDS = (
    "id",
    "account_id",
    "address",
    "name",
    "enabled",
    "created",
    "last_matched",
)
USAGE_FIELDS = (
    "account_id",
    "window_start",
    "window_seconds",
    "prompt_tokens",
    "completion_tokens",
    "estimated_tokens",
    "requests",
)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        self._caller_source_evidence = DiscoveryCallerEvidence(
            self.discovery_directory
        )
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
            CREATE TABLE IF NOT EXISTS caller_source_names (source_key TEXT PRIMARY KEY, name TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
              id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
              level_id TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pending','active','suspended')),
              created REAL NOT NULL, activated_at REAL, last_login REAL);
            CREATE TABLE IF NOT EXISTS account_credentials (
              account_id TEXT PRIMARY KEY, verifier TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS account_activations (
              digest TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE, expires REAL NOT NULL,
              purpose TEXT NOT NULL CHECK (purpose IN ('activate','reset')));
            CREATE TABLE IF NOT EXISTS account_keys (
              id TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT NOT NULL,
              lookup TEXT NOT NULL UNIQUE, verifier TEXT NOT NULL, created REAL NOT NULL, last_used REAL);
            CREATE INDEX IF NOT EXISTS account_keys_account ON account_keys(account_id);
            CREATE TABLE IF NOT EXISTS portal_sessions (
              digest TEXT PRIMARY KEY, account_id TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS portal_sessions_account ON portal_sessions(account_id);
            CREATE TABLE IF NOT EXISTS registered_devices (
              id TEXT PRIMARY KEY, account_id TEXT NOT NULL, address TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), created REAL NOT NULL, last_matched REAL);
            CREATE INDEX IF NOT EXISTS registered_devices_account ON registered_devices(account_id);
            CREATE TABLE IF NOT EXISTS usage_windows (
              account_id TEXT NOT NULL, window_start REAL NOT NULL, window_seconds INTEGER NOT NULL,
              prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
              estimated_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (account_id, window_start, window_seconds));
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
        self._migrate_callers()
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
        self._key_touched: dict[str, float] = {}
        self._load_account_caches()

    def _load_account_caches(self):
        # Request paths read these dictionaries without I/O. Each reload
        # builds fresh dictionaries and swaps them in, never mutates in place.
        accounts = {
            row[0]: dict(zip(ACCOUNT_FIELDS, row))
            for row in self.db.execute(
                "SELECT id, username, name, level_id, status, created, activated_at, last_login FROM accounts"
            )
        }
        devices = {}
        for row in self.db.execute(
            "SELECT id, account_id, address, name, enabled, created, last_matched FROM registered_devices"
        ):
            device = dict(zip(DEVICE_FIELDS, row))
            device["enabled"] = bool(device["enabled"])
            devices[device["id"]] = device
        self._accounts = accounts
        self._accounts_by_username = {
            account["username"]: account["id"] for account in accounts.values()
        }
        self._devices = devices
        self._devices_by_address = {
            device["address"]: device for device in devices.values()
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
            config.validate_endpoint_changes(current)
            # Accounts reference levels by id from outside the revisioned blob,
            # so a level with accounts is refused rather than silently orphaned.
            kept_levels = {level.id for level in config.account_levels}
            counts = self.level_account_counts()
            for level in current.account_levels:
                if level.id not in kept_levels and counts.get(level.id):
                    raise ValueError(
                        f"Move {counts[level.id]} account(s) off level '{level.name}' before removing it"
                    )
            # Account ids double as principal ids (events.client_id,
            # callers.policy_id, route-map edges), so a client or level may
            # not take one without mis-attributing that account's traffic.
            principal_ids = {c.id for c in config.clients} | kept_levels
            if principal_ids & set(self._accounts):
                raise ValueError("Client and level ids must not reuse an account id")
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
        lookup = self.cipher.lookup(key)
        with self.lock:
            row = self.db.execute(
                "SELECT verifier, client_id FROM client_credentials WHERE lookup=?",
                (lookup,),
            ).fetchone()
        if row:
            if not verify(row[0], key):
                return None
            # Verification is expensive and must not hold up other requests.
            # Revocation or reassignment during that work invalidates this read.
            with self.lock:
                current = self.db.execute(
                    "SELECT verifier, client_id FROM client_credentials WHERE lookup=?",
                    (lookup,),
                ).fetchone()
                return row[1] if current == row else None
        # Existing high-entropy API keys remain valid. On their first successful
        # use, replace the legacy verifier without changing the caller's key.
        old_digest = hashlib.sha256(key.encode()).hexdigest()
        with self.lock:
            legacy = self.db.execute(
                "SELECT digest, client_id FROM client_keys WHERE digest=?",
                (old_digest,),
            ).fetchone()
        if legacy and hmac.compare_digest(legacy[0], old_digest):
            verifier = digest(key)
            with self.lock:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    current = self.db.execute(
                        "SELECT digest, client_id FROM client_keys WHERE digest=?",
                        (old_digest,),
                    ).fetchone()
                    modern = self.db.execute(
                        "SELECT lookup FROM client_credentials WHERE lookup=?",
                        (lookup,),
                    ).fetchone()
                    if current != legacy or modern:
                        return None
                    self.db.execute(
                        "INSERT INTO client_credentials VALUES (?, ?, ?)",
                        (lookup, verifier, legacy[1]),
                    )
                    self.db.execute("DELETE FROM client_keys WHERE digest=?", (old_digest,))
                self._key_owners.add(legacy[1])
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
            labels = self.caller_source_names()
            hostnames = self.discovered_source_hostnames()
            events = []
            for row in self.db.execute(
                "SELECT body FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ):
                event = json.loads(row[0])
                caller = event.get("caller")
                if isinstance(caller, dict):
                    event["caller"] = self._apply_caller_source_name(
                        ensure_source_identity(caller), labels, hostnames
                    )
                events.append(event)
            return events

    def _migrate_callers(self):
        """Consolidate old auth-dependent IDs without rewriting request events."""
        with self.lock, self.db:
            # Another process may open the store while requests are arriving.
            # Reserve the write transaction before reading so no row is lost.
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT id, body FROM callers ORDER BY seen, id"
            ).fetchall()
            merged = {}
            changed = False
            for old_id, body in rows:
                caller = json.loads(body)
                caller = ensure_source_identity(caller)
                identifier = caller_id(caller)
                changed |= old_id != identifier or caller.get("id") != identifier
                caller["id"] = identifier
                if identifier in merged:
                    changed = True
                    caller = merge_observations(merged[identifier], caller)
                merged[identifier] = caller
            if changed:
                self.db.execute("DELETE FROM callers")
                self.db.executemany(
                    "INSERT INTO callers VALUES (?, ?, ?)",
                    [
                        (row["id"], row["last_seen"], json.dumps(row))
                        for row in merged.values()
                    ],
                )

    def observe_caller(self, caller: dict):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            caller = ensure_source_identity(caller)
            previous_row = self.db.execute(
                "SELECT body FROM callers WHERE id=?", (caller["id"],)
            ).fetchone()
            if previous_row:
                stored = merge_observations(json.loads(previous_row[0]), caller)
            else:
                stored = dict(caller)
                source_port = caller.get("source_port")
                source_address = caller.get("source_address")
                stored["recent_source_ports"] = (
                    [source_port] if source_port is not None else []
                )
                stored["recent_source_addresses"] = (
                    [source_address] if source_address else []
                )
            # Events describe this request even if a later observation has
            # already arrived. Only aggregate history is shared back with it.
            for field in (
                "first_seen",
                "request_count",
                "recent_source_ports",
                "recent_source_addresses",
            ):
                caller[field] = stored[field]
            self.db.execute(
                "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
                (stored["id"], stored["last_seen"], json.dumps(stored)),
            )
            self.db.execute(
                "DELETE FROM callers WHERE seen < ?", (time.time() - 7 * 86400,)
            )
            self.db.execute(
                "DELETE FROM callers WHERE id NOT IN (SELECT id FROM callers ORDER BY seen DESC LIMIT 1000)"
            )

    def set_caller_source_name(self, source_key: str, name: str) -> dict:
        value = " ".join(name.split())[:100]
        with self.lock, self.db:
            records = [
                ensure_source_identity(json.loads(row[0]))
                for row in self.db.execute("SELECT body FROM callers")
            ]
            if not any(record["source_key"] == source_key for record in records):
                raise ValueError("Caller source no longer exists")
            if value:
                self.db.execute(
                    "INSERT OR REPLACE INTO caller_source_names VALUES (?, ?, ?)",
                    (source_key, value, time.time()),
                )
            else:
                self.db.execute(
                    "DELETE FROM caller_source_names WHERE source_key=?", (source_key,)
                )
            labels = self.caller_source_names()
            hostnames = self.discovered_source_hostnames()
            return next(
                self._apply_caller_source_name(record, labels, hostnames)
                for record in records
                if record["source_key"] == source_key
            )

    def caller_source_names(self) -> dict[str, str]:
        with self.lock:
            return {
                source_key: name
                for source_key, name in self.db.execute(
                    "SELECT source_key, name FROM caller_source_names"
                )
            }

    def source_identity(self, source_address: str, hints: dict[str, str]) -> dict[str, str]:
        """Build caller identity from request evidence and a saved discovery snapshot."""
        evidence = self.discovered_source_evidence().get(
            normalized_address(source_address), {}
        )
        return caller_source_identity(
            source_address,
            hints,
            hardware_address=evidence.get("hardware_address", ""),
        )

    def discovered_source_evidence(self) -> dict[str, dict[str, str]]:
        return self._caller_source_evidence.sources(
            self._config.discovery.network_interval_seconds
        )

    def discovered_source_hostnames(self) -> dict[str, str]:
        return {
            address: evidence["hostname"]
            for address, evidence in self.discovered_source_evidence().items()
            if evidence["hostname"] and evidence["hostname"] != address.casefold()
        }

    def callers(self) -> list[dict]:
        with self.lock:
            labels = self.caller_source_names()
            hostnames = self.discovered_source_hostnames()
            records = []
            for row in self.db.execute(
                "SELECT body FROM callers WHERE seen >= ? ORDER BY seen DESC LIMIT 1000",
                (time.time() - 7 * 86400,),
            ):
                caller = ensure_source_identity(json.loads(row[0]))
                records.append(self._apply_caller_source_name(caller, labels, hostnames))
            return records

    def _apply_caller_source_name(
        self, caller: dict, labels: dict[str, str], hostnames: dict[str, str]
    ) -> dict:
        caller = dict(caller)
        label = labels.get(caller["source_key"])
        if label:
            caller["source_label"] = label
            caller["source_label_source"] = "operator"
            return caller
        hostname = hostnames.get(normalized_address(str(caller.get("source_address", ""))))
        if hostname and caller.get("source_label_source") == "address":
            caller["source_label"] = hostname
            caller["source_label_source"] = "discovered_hostname"
            caller["source_hostname"] = hostname
        return caller

    # Accounts. Rows never carry verifiers; the request path reads caches only.

    def accounts(self) -> list[dict]:
        return sorted(
            (dict(account) for account in self._accounts.values()),
            key=lambda account: account["username"],
        )

    def account_snapshot(self, account_id: str) -> dict | None:
        account = self._accounts.get(account_id)
        return dict(account) if account else None

    def account_by_username(self, username: str) -> dict | None:
        account_id = self._accounts_by_username.get(username)
        return self.account_snapshot(account_id) if account_id else None

    def level_account_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for account in self._accounts.values():
            counts[account["level_id"]] = counts.get(account["level_id"], 0) + 1
        return counts

    def _require_level(self, level_id: str):
        if not any(level.id == level_id for level in self._config.account_levels):
            raise ValueError("Level does not exist")

    def _require_account(self, account_id: str):
        if account_id not in self._accounts:
            raise ValueError("Account does not exist")

    @staticmethod
    def _account_name(name: str) -> str:
        # The name becomes the derived Client's name, which NamedRecord bounds.
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("Choose a name of 1 to 100 characters")
        return name

    def create_account(self, username: str, name: str, level_id: str) -> dict:
        name = self._account_name(name)
        with self.lock:
            self._require_level(level_id)
            if username in self._accounts_by_username:
                raise ValueError("Username is already taken")
            # Account ids double as principal ids next to clients and levels.
            taken = (
                {client.id for client in self._config.clients}
                | {level.id for level in self._config.account_levels}
                | set(self._accounts)
            )
            account_id = uuid4().hex
            if account_id in taken:
                account_id = uuid4().hex
                if account_id in taken:
                    raise ValueError("Identifier collides with an existing record")
            with self.db:
                self.db.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL)",
                    (account_id, username, name, level_id, time.time()),
                )
            self._load_account_caches()
            return self.account_snapshot(account_id)

    def update_account(
        self, account_id: str, *, name=None, level_id=None, status=None
    ) -> dict:
        if status is not None and status not in ACCOUNT_STATUSES:
            raise ValueError("Unknown account status")
        if name is not None:
            name = self._account_name(name)
        with self.lock:
            self._require_account(account_id)
            if level_id is not None:
                self._require_level(level_id)
            with self.db:
                for column, value in (
                    ("name", name),
                    ("level_id", level_id),
                    ("status", status),
                ):
                    if value is not None:
                        self.db.execute(
                            f"UPDATE accounts SET {column}=? WHERE id=?",
                            (value, account_id),
                        )
                if status == "suspended":
                    self.db.execute(
                        "DELETE FROM portal_sessions WHERE account_id=?", (account_id,)
                    )
            self._load_account_caches()
            return self.account_snapshot(account_id)

    def delete_account(self, account_id: str) -> None:
        with self.lock:
            key_ids = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM account_keys WHERE account_id=?", (account_id,)
                )
            ]
            with self.db:
                self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                for table in (
                    "account_credentials",
                    "account_activations",
                    "account_keys",
                    "portal_sessions",
                    "registered_devices",
                    "usage_windows",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE account_id=?", (account_id,)
                    )
            for key_id in key_ids:
                self._key_touched.pop(key_id, None)
            self._load_account_caches()

    # Activation links and passwords.

    def issue_activation(self, account_id: str, purpose: str) -> tuple[str, float]:
        if purpose not in ACTIVATION_PURPOSES:
            raise ValueError("Unknown activation purpose")
        token = "mra_" + secrets.token_urlsafe(32)
        expires = time.time() + ACTIVATION_SECONDS
        with self.lock:
            self._require_account(account_id)
            with self.db:
                self.db.execute(
                    "DELETE FROM account_activations WHERE account_id=?", (account_id,)
                )
                self.db.execute(
                    "INSERT INTO account_activations VALUES (?, ?, ?, ?)",
                    (token_digest(token), account_id, expires, purpose),
                )
                self.db.execute(
                    "DELETE FROM portal_sessions WHERE account_id=?", (account_id,)
                )
        return token, expires

    def activate_account(self, token: str, verifier: str) -> str | None:
        """Consume a link and set the password in one transaction; None when unusable."""
        with self.lock:
            with self.db:
                row = self.db.execute(
                    "SELECT a.account_id FROM account_activations a JOIN accounts ON accounts.id = a.account_id"
                    " WHERE a.digest=? AND a.expires > ? AND accounts.status != 'suspended'",
                    (token_digest(token), time.time()),
                ).fetchone()
                if not row:
                    return None
                account_id = row[0]
                now = time.time()
                self.db.execute(
                    "INSERT OR REPLACE INTO account_credentials VALUES (?, ?, ?)",
                    (account_id, verifier, now),
                )
                self.db.execute(
                    "UPDATE accounts SET status='active', activated_at=COALESCE(activated_at, ?) WHERE id=?",
                    (now, account_id),
                )
                self.db.execute(
                    "DELETE FROM account_activations WHERE account_id=?", (account_id,)
                )
                self.db.execute(
                    "DELETE FROM portal_sessions WHERE account_id=?", (account_id,)
                )
            self._load_account_caches()
            return account_id

    def activation_for(self, account_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT expires, purpose FROM account_activations WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return {"expires": row[0], "purpose": row[1]} if row else None

    def clear_activation(self, account_id: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM account_activations WHERE account_id=?", (account_id,)
            )

    def account_verifier(self, account_id: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT verifier FROM account_credentials WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return row[0] if row else None

    def set_account_verifier(self, account_id: str, verifier: str) -> None:
        with self.lock:
            self._require_account(account_id)
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO account_credentials VALUES (?, ?, ?)",
                    (account_id, verifier, time.time()),
                )

    def touch_login(self, account_id: str) -> None:
        with self.lock:
            with self.db:
                self.db.execute(
                    "UPDATE accounts SET last_login=? WHERE id=?",
                    (time.time(), account_id),
                )
            self._load_account_caches()

    # Account keys use the client-key scheme: keyed lookup index, argon2 verifier.

    def create_account_key(self, account_id: str, name: str) -> tuple[str, dict]:
        key = "mru_" + secrets.token_urlsafe(32)
        verifier = digest(key)
        with self.lock:
            self._require_account(account_id)
            names = [
                row[0]
                for row in self.db.execute(
                    "SELECT name FROM account_keys WHERE account_id=?", (account_id,)
                )
            ]
            if len(names) >= MAX_ACCOUNT_KEYS:
                raise ValueError(
                    f"This account already has {MAX_ACCOUNT_KEYS} keys. Revoke one first."
                )
            if name in names:
                raise ValueError("A key with this name already exists")
            record = {
                "id": uuid4().hex,
                "name": name,
                "created": time.time(),
                "last_used": None,
            }
            with self.db:
                self.db.execute(
                    "INSERT INTO account_keys VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        record["id"],
                        account_id,
                        name,
                        self.cipher.lookup(key),
                        verifier,
                        record["created"],
                    ),
                )
        return key, record

    def account_key(self, key: str) -> dict | None:
        lookup = self.cipher.lookup(key)
        query = "SELECT id, account_id, name, verifier FROM account_keys WHERE lookup=?"
        with self.lock:
            row = self.db.execute(query, (lookup,)).fetchone()
        if not row or not verify(row[3], key):
            return None
        key_id = row[0]
        with self.lock:
            # Verification is expensive and must not hold up other requests.
            # Revocation or account deletion during that work invalidates this read.
            if self.db.execute(query, (lookup,)).fetchone() != row:
                return None
            # last_used is informational; one write per minute per key bounds
            # the storage cost of a busy key.
            touched = self._key_touched.get(key_id)
            now = time.monotonic()
            if touched is None or now - touched > KEY_TOUCH_SECONDS:
                self.db.execute(
                    "UPDATE account_keys SET last_used=? WHERE id=?",
                    (time.time(), key_id),
                )
                self.db.commit()
                self._key_touched[key_id] = now
        return {"account_id": row[1], "key_id": key_id, "key_name": row[2]}

    def account_keys(self, account_id: str) -> list[dict]:
        with self.lock:
            return [
                {"id": row[0], "name": row[1], "created": row[2], "last_used": row[3]}
                for row in self.db.execute(
                    "SELECT id, name, created, last_used FROM account_keys WHERE account_id=? ORDER BY created",
                    (account_id,),
                )
            ]

    def revoke_account_key(self, account_id: str, key_id: str) -> bool:
        with self.lock:
            with self.db:
                removed = self.db.execute(
                    "DELETE FROM account_keys WHERE id=? AND account_id=?",
                    (key_id, account_id),
                ).rowcount
            self._key_touched.pop(key_id, None)
            return removed > 0

    # Portal sessions: separate table from operator sessions, never shared.

    def portal_sessions(self) -> dict[str, tuple[str, float]]:
        with self.lock:
            with self.db:
                self.db.execute(
                    "DELETE FROM portal_sessions WHERE expires <= ?", (time.time(),)
                )
            return {
                row[0]: (row[1], row[2])
                for row in self.db.execute(
                    "SELECT digest, account_id, expires FROM portal_sessions ORDER BY expires DESC LIMIT 1024"
                )
            }

    def save_portal_session(self, digest: str, account_id: str, expires: float):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO portal_sessions VALUES (?, ?, ?, ?)",
                (digest, account_id, expires, time.time()),
            )

    def revoke_portal_session(self, digest: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM portal_sessions WHERE digest=?", (digest,))

    def revoke_account_sessions(self, account_id: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM portal_sessions WHERE account_id=?", (account_id,)
            )

    # Registered devices: one canonical host address each, globally unique.

    def register_device(self, account_id: str, address: str, name: str) -> dict:
        # An IPv4-mapped IPv6 literal names the same host as its IPv4 form;
        # storing the unmapped form keeps the address unique across both.
        parsed = ipaddress.ip_address(address)
        address = str(getattr(parsed, "ipv4_mapped", None) or parsed)
        with self.lock:
            self._require_account(account_id)
            if address in self._devices_by_address:
                raise ValueError("This address is already registered")
            owned = sum(
                1 for device in self._devices.values() if device["account_id"] == account_id
            )
            if owned >= MAX_ACCOUNT_DEVICES:
                raise ValueError(
                    f"This account already has {MAX_ACCOUNT_DEVICES} registered devices"
                )
            device_id = uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO registered_devices VALUES (?, ?, ?, ?, 1, ?, NULL)",
                    (device_id, account_id, address, name, time.time()),
                )
            self._load_account_caches()
            return self.device_snapshot(device_id)

    def devices(self) -> list[dict]:
        return sorted(
            (dict(device) for device in self._devices.values()),
            key=lambda device: device["created"],
        )

    def devices_for(self, account_id: str) -> list[dict]:
        return [
            device for device in self.devices() if device["account_id"] == account_id
        ]

    def device_at(self, address: str) -> dict | None:
        device = self._devices_by_address.get(address)
        return dict(device) if device else None

    def device_snapshot(self, device_id: str) -> dict | None:
        device = self._devices.get(device_id)
        return dict(device) if device else None

    def set_device_enabled(self, device_id: str, enabled: bool) -> dict | None:
        with self.lock:
            if device_id not in self._devices:
                return None
            with self.db:
                self.db.execute(
                    "UPDATE registered_devices SET enabled=? WHERE id=?",
                    (1 if enabled else 0, device_id),
                )
            self._load_account_caches()
            return self.device_snapshot(device_id)

    def remove_device(self, device_id: str, account_id: str | None = None) -> bool:
        with self.lock:
            with self.db:
                if account_id is None:
                    removed = self.db.execute(
                        "DELETE FROM registered_devices WHERE id=?", (device_id,)
                    ).rowcount
                else:
                    removed = self.db.execute(
                        "DELETE FROM registered_devices WHERE id=? AND account_id=?",
                        (device_id, account_id),
                    ).rowcount
            self._load_account_caches()
            return removed > 0

    def touch_device(self, device_id: str) -> None:
        with self.lock:
            with self.db:
                self.db.execute(
                    "UPDATE registered_devices SET last_matched=? WHERE id=?",
                    (time.time(), device_id),
                )
            self._load_account_caches()

    # Usage windows hold token counts only, never request or response content.

    def record_usage(
        self,
        account_id: str,
        window_start: float,
        window_seconds: int,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_tokens: int,
    ) -> None:
        with self.lock:
            # A request settling after its account was deleted must not
            # resurrect a usage row for it.
            if account_id not in self._accounts:
                return
            with self.db:
                self.db.execute(
                    "INSERT INTO usage_windows VALUES (?, ?, ?, ?, ?, ?, 1)"
                    " ON CONFLICT(account_id, window_start, window_seconds) DO UPDATE SET"
                    " prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                    " completion_tokens = completion_tokens + excluded.completion_tokens,"
                    " estimated_tokens = estimated_tokens + excluded.estimated_tokens,"
                    " requests = requests + 1",
                    (
                        account_id,
                        window_start,
                        window_seconds,
                        prompt_tokens,
                        completion_tokens,
                        estimated_tokens,
                    ),
                )
                self.db.execute(
                    "DELETE FROM usage_windows WHERE window_start + window_seconds < ?",
                    (time.time() - USAGE_RETENTION_SECONDS,),
                )

    def usage_windows(self, account_id: str, days: int = 7) -> list[dict]:
        with self.lock:
            return [
                dict(zip(USAGE_FIELDS, row))
                for row in self.db.execute(
                    "SELECT account_id, window_start, window_seconds, prompt_tokens, completion_tokens, estimated_tokens, requests"
                    " FROM usage_windows WHERE account_id=? AND window_start + window_seconds > ? ORDER BY window_start DESC",
                    (account_id, time.time() - days * 86400),
                )
            ]

    def open_usage_windows(self, now: float) -> list[dict]:
        with self.lock:
            return [
                dict(zip(USAGE_FIELDS, row))
                for row in self.db.execute(
                    "SELECT account_id, window_start, window_seconds, prompt_tokens, completion_tokens, estimated_tokens, requests"
                    " FROM usage_windows WHERE window_start + window_seconds > ?",
                    (now,),
                )
            ]
