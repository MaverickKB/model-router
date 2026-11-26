"""Bounded, operator-only request and response capture for diagnostics.

The debug store lives in its own SQLite file next to the router database.
Capture is off by default. A session has a time limit and a byte limit, and
the store purges the session's entries when the time limit expires.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

MAX_BODY_BYTES = 256 * 1024
DEFAULT_TTL_SECONDS = 15 * 60
DEFAULT_MAX_BYTES = 100 * 1024 * 1024

SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-router-key",
}

SECRET_JSON_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "secret",
    "access_token",
    "refresh_token",
    "client_secret",
}


def redact_headers(headers) -> dict:
    """Return a header map with credentials and tokens replaced."""
    result = {}
    for key, value in dict(headers).items():
        lower = key.lower()
        if lower in SECRET_HEADERS:
            result[key] = "[redacted]"
            continue
        if "token" in lower or "secret" in lower or "key" in lower or "password" in lower:
            result[key] = "[redacted]"
            continue
        result[key] = value
    return result


def redact_json(value):
    """Recursively replace known credential fields in a JSON value."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.lower() in SECRET_JSON_KEYS:
                result[key] = "[redacted]"
            else:
                result[key] = redact_json(item)
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.append(redact_json(item))
        return result
    return value


def redact_body(body: bytes, content_type: str) -> bytes:
    """Redact known credential fields from a JSON body."""
    if not body:
        return body
    if "json" not in content_type.lower():
        return body
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return body
    redacted = redact_json(value)
    return json.dumps(redacted, ensure_ascii=False).encode("utf-8")


@dataclass
class DebugSession:
    id: str
    started: float
    expires: float
    max_bytes: int
    scope: list[str]
    operator: str
    status: str = "active"
    stopped: float | None = None
    stop_reason: str | None = None

    def as_dict(self) -> dict:
        remaining = self.expires - time.time()
        if remaining < 0:
            remaining = 0
        return {
            "id": self.id,
            "started": self.started,
            "expires": self.expires,
            "ttl_remaining": remaining,
            "max_bytes": self.max_bytes,
            "scope": self.scope,
            "operator": self.operator,
            "status": self.status,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }


class DebugBuffer:
    """Collect stream bytes up to the body limit for one request."""

    def __init__(self, headers):
        self.headers = headers
        self.body = bytearray()
        self.truncated = False
        self.flushed = False

    def append(self, chunk: bytes) -> None:
        if len(self.body) >= MAX_BODY_BYTES:
            self.truncated = True
            return
        remaining = MAX_BODY_BYTES - len(self.body)
        if len(chunk) > remaining:
            self.body.extend(chunk[:remaining])
            self.truncated = True
            return
        self.body.extend(chunk)

    def flush(self) -> bytes:
        body = bytes(self.body)
        self.body.clear()
        return body


class DebugStore:
    """Own one active debug session and its bounded capture entries."""

    def __init__(self, state_dir):
        self.directory = Path(state_dir)
        self.path = self.directory / "debug.db"
        self.lock = threading.Lock()
        self.session: DebugSession | None = None
        self._init_db()
        self._load_active_session()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _checkpoint(self, conn) -> None:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def _init_db(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS debug_sessions (
                    id TEXT PRIMARY KEY,
                    started REAL NOT NULL,
                    expires REAL NOT NULL,
                    max_bytes INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stopped REAL,
                    stop_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS debug_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    request_id TEXT,
                    direction TEXT NOT NULL,
                    headers TEXT,
                    body TEXT,
                    body_bytes INTEGER NOT NULL,
                    truncated INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS debug_entries_session
                    ON debug_entries(session_id, ts);
                """
            )

    def _load_active_session(self) -> None:
        now = time.time()
        with self.lock:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT * FROM debug_sessions WHERE status='active' "
                    "ORDER BY started DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return
                # A restart must not resume a capture window. Close it and
                # purge its payloads before serving begins.
                conn.execute(
                    "UPDATE debug_sessions SET status='stopped', stopped=?, "
                    "stop_reason='restart' WHERE id=?",
                    (now, row["id"]),
                )
                conn.execute(
                    "DELETE FROM debug_entries WHERE session_id=?",
                    (row["id"],),
                )
                conn.commit()
                self._checkpoint(conn)
                self.session = None

    def start(self, ttl_seconds: int, max_bytes: int, scope, operator: str) -> DebugSession:
        now = time.time()
        session = DebugSession(
            id=uuid4().hex,
            started=now,
            expires=now + ttl_seconds,
            max_bytes=max_bytes,
            scope=list(scope),
            operator=operator,
        )
        with self.lock:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM debug_entries")
                conn.execute("DELETE FROM debug_sessions")
                conn.commit()
                conn.execute("VACUUM")
                conn.execute(
                    "INSERT INTO debug_sessions "
                    "(id, started, expires, max_bytes, scope, operator, status, stopped, stop_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session.id,
                        session.started,
                        session.expires,
                        session.max_bytes,
                        json.dumps(session.scope),
                        session.operator,
                        session.status,
                        None,
                        None,
                    ),
                )
                conn.commit()
                self._checkpoint(conn)
            self.session = session
        return session

    def stop(self, reason: str = "operator") -> None:
        with self.lock:
            self._stop_locked(reason)

    def _stop_locked(self, reason: str) -> None:
        session = self.session
        if session is None:
            return
        self.session = None
        if reason in {"operator", "shutdown", "max_bytes"}:
            status = "stopped"
        else:
            status = "expired"
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE debug_sessions SET status=?, stopped=?, stop_reason=? WHERE id=?",
                (status, now, reason, session.id),
            )
            if status == "expired":
                conn.execute(
                    "DELETE FROM debug_entries WHERE session_id=?",
                    (session.id,),
                )
            conn.commit()
            if status == "expired":
                self._checkpoint(conn)

    def active(self) -> DebugSession | None:
        with self.lock:
            session = self.session
            if session is None:
                return None
            if session.expires <= time.time():
                self._stop_locked("expired")
                return None
            return session

    def expire(self) -> None:
        """Stop and purge a session whose time limit has passed."""
        self.active()

    def record(self, direction: str, request_id: str, headers, body: bytes,
               content_type: str, truncated: bool = False) -> None:
        session = self.active()
        if session is None:
            return
        if direction == "request" and "request" not in session.scope:
            return
        if direction == "response" and "response" not in session.scope:
            return
        if direction == "stream" and "stream" not in session.scope:
            return

        session_id = session.id
        header_map = {}
        if "headers" in session.scope:
            header_map = redact_headers(headers)
        headers_json = json.dumps(header_map, ensure_ascii=False)

        body_bytes = redact_body(body, content_type)
        was_truncated = bool(truncated)
        if len(body_bytes) > MAX_BODY_BYTES:
            body_bytes = body_bytes[:MAX_BODY_BYTES]
            was_truncated = True
        stored_bytes = len(body_bytes)
        body_text = body_bytes.decode("utf-8", errors="replace")
        header_bytes = len(headers_json.encode("utf-8"))

        with self.lock:
            current = self.session
            if current is None or current.id != session_id:
                return
            try:
                with closing(self._connect()) as conn:
                    used = conn.execute(
                        "SELECT COALESCE(SUM(body_bytes + LENGTH(COALESCE(headers, ''))), 0) "
                        "FROM debug_entries WHERE session_id=?",
                        (session_id,),
                    ).fetchone()[0]
                    if used + stored_bytes + header_bytes > session.max_bytes:
                        self._stop_locked("max_bytes")
                        return
                    conn.execute(
                        "INSERT INTO debug_entries "
                        "(session_id, ts, request_id, direction, headers, body, body_bytes, truncated) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            time.time(),
                            request_id,
                            direction,
                            headers_json,
                            body_text,
                            stored_bytes,
                            1 if was_truncated else 0,
                        ),
                    )
                    conn.commit()
            except sqlite3.Error:
                # Capture must never fail a proxied request.
                return

    def capture_request(self, request_id: str, headers, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.record("request", request_id, headers, body, "application/json")

    def capture_response(self, request_id: str, headers, body: bytes,
                         content_type: str) -> None:
        self.record("response", request_id, headers, body, content_type)

    def capture_stream(self, request_id: str, headers, body: bytes,
                       content_type: str, truncated: bool) -> None:
        self.record("stream", request_id, headers, body, content_type, truncated)

    def status(self) -> dict:
        session = self.active()
        with self.lock:
            with closing(self._connect()) as conn:
                entries = conn.execute(
                    "SELECT COUNT(*) FROM debug_entries"
                ).fetchone()[0]
                used = conn.execute(
                    "SELECT COALESCE(SUM(body_bytes + LENGTH(COALESCE(headers, ''))), 0) "
                    "FROM debug_entries"
                ).fetchone()[0]
                sessions = conn.execute(
                    "SELECT COUNT(*) FROM debug_sessions"
                ).fetchone()[0]
                last_row = conn.execute(
                    "SELECT * FROM debug_sessions ORDER BY started DESC LIMIT 1"
                ).fetchone()
        last_session = None
        if last_row is not None:
            last_session = {
                "id": last_row["id"],
                "started": last_row["started"],
                "expires": last_row["expires"],
                "max_bytes": last_row["max_bytes"],
                "scope": json.loads(last_row["scope"]),
                "operator": last_row["operator"],
                "status": last_row["status"],
                "stopped": last_row["stopped"],
                "stop_reason": last_row["stop_reason"],
            }
        db_bytes = self.path.stat().st_size if self.path.exists() else 0
        return {
            "active": session is not None,
            "session": session.as_dict() if session is not None else None,
            "last_session": last_session,
            "entries": entries,
            "bytes_used": used,
            "db_bytes": db_bytes,
            "sessions": sessions,
            "path": str(self.path),
            "defaults": {
                "ttl_seconds": DEFAULT_TTL_SECONDS,
                "max_bytes": DEFAULT_MAX_BYTES,
                "scope": ["headers", "request", "response", "stream"],
            },
        }

    def entries(self, limit: int = 100, offset: int = 0, preview: bool = False) -> dict:
        with self.lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT id, session_id, ts, request_id, direction, headers, body, "
                    "body_bytes, truncated FROM debug_entries "
                    "ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM debug_entries"
                ).fetchone()[0]
        items = []
        for row in rows:
            item = dict(row)
            header_text = item.get("headers") or "{}"
            try:
                item["headers"] = json.loads(header_text)
            except ValueError:
                item["headers"] = {}
            if preview:
                body_text = item.get("body") or ""
                item["body_preview"] = body_text[:200]
                item.pop("body", None)
            items.append(item)
        return {"items": items, "total": total}

    def entry(self, entry_id: int) -> dict | None:
        with self.lock:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT id, session_id, ts, request_id, direction, headers, body, "
                    "body_bytes, truncated FROM debug_entries WHERE id=?",
                    (entry_id,),
                ).fetchone()
        if row is None:
            return None
        item = dict(row)
        header_text = item.get("headers") or "{}"
        try:
            item["headers"] = json.loads(header_text)
        except ValueError:
            item["headers"] = {}
        return item

    def purge(self) -> None:
        with self.lock:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM debug_entries")
                conn.execute("DELETE FROM debug_sessions")
                conn.commit()
                conn.execute("VACUUM")
                self._checkpoint(conn)
            self.session = None

    def close(self) -> None:
        self.stop("shutdown")
