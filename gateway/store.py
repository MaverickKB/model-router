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

from .caller_records import caller_id, merge_observations
from .caller_sources import ensure_source_identity, normalized_address
from .caller_sources import source_identity as caller_source_identity
from .engine_identity import merged_configuration
from .migration import configuration as migrate_configuration
from .network.caller_evidence import DiscoveryCallerEvidence
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
