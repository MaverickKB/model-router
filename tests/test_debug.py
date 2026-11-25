"""Debug capture is operator-only, bounded, redacted, and off by default."""

import asyncio
import json
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.debug_capture import (
    DebugStore,
    MAX_BODY_BYTES,
    redact_body,
    redact_headers,
)
from gateway.schema import (
    Client,
    Configuration,
    Discovery,
    Engine,
    Route,
    Security,
    Selector,
)


def test_redacts_secret_headers_and_json_fields():
    headers = {
        "Authorization": "Bearer ***",
        "Content-Type": "application/json",
        "X-Test-Secret": "***",
        "X-Router-Model": "work",
    }
    redacted = redact_headers(headers)
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["X-Test-Secret"] == "[redacted]"
    assert redacted["Content-Type"] == "application/json"
    assert redacted["X-Router-Model"] == "work"

    body = json.dumps(
        {
            "model": "work",
            "messages": [{"role": "user", "content": "hi"}],
            "api_key": "***",
            "max_tokens": 8,
        }
    ).encode()
    result = json.loads(redact_body(body, "application/json"))
    assert result["api_key"] == "[redacted]"
    assert result["max_tokens"] == 8
    assert result["messages"][0]["content"] == "hi"


def test_debug_store_start_record_stop_and_purge(tmp_path):
    store = DebugStore(tmp_path)
    session = store.start(
        300, 1024 * 1024, ["headers", "request", "response"], "operator"
    )
    assert session.status == "active"
    store.capture_request(
        "req-1",
        {"Authorization": "Bearer ***", "Content-Type": "application/json"},
        {"model": "work", "messages": [], "api_key": "***"},
    )
    store.capture_response(
        "req-1",
        {"Content-Type": "application/json"},
        b'{"ok": true}',
        "application/json",
    )

    status = store.status()
    assert status["active"] is True
    assert status["entries"] == 2
    assert status["session"]["id"] == session.id
    assert status["last_session"]["status"] == "active"

    entries = store.entries()
    assert entries["total"] == 2
    request_entry = next(
        item for item in entries["items"] if item["direction"] == "request"
    )
    assert request_entry["headers"]["Authorization"] == "[redacted]"
    assert json.loads(request_entry["body"])["api_key"] == "[redacted]"

    preview_entries = store.entries(preview=True)
    preview_request = next(
        item for item in preview_entries["items"] if item["direction"] == "request"
    )
    assert "body" not in preview_request
    assert preview_request["body_preview"].startswith("{")
    detail = store.entry(request_entry["id"])
    assert detail is not None
    assert "api_key" in detail["body"]

    store.stop()
    status = store.status()
    assert status["active"] is False
    assert status["entries"] == 2
    assert status["last_session"]["status"] == "stopped"
    assert status["last_session"]["stop_reason"] == "operator"

    store.purge()
    status = store.status()
    assert status["active"] is False
    assert status["entries"] == 0
    assert status["sessions"] == 0
    assert status["last_session"] is None


def test_debug_store_expiry_purges(tmp_path):
    store = DebugStore(tmp_path)
    store.start(1, 1024 * 1024, ["request"], "operator")
    store.capture_request("req-1", {}, {"model": "work"})
    assert store.status()["entries"] == 1

    time.sleep(1.1)
    status = store.status()
    assert status["active"] is False
    assert status["entries"] == 0
    assert status["last_session"]["status"] == "expired"
    assert status["last_session"]["stop_reason"] == "expired"


def test_debug_store_size_cap_stops_and_keeps_entries(tmp_path):
    store = DebugStore(tmp_path)
    store.start(300, 1024, ["request"], "operator")
    store.capture_request(
        "req-1", {}, {"model": "work", "messages": ["x" * 700]}
    )
    store.capture_request(
        "req-2", {}, {"model": "work", "messages": ["x" * 700]}
    )

    status = store.status()
    assert status["active"] is False
    assert status["entries"] == 1
    assert status["last_session"]["status"] == "stopped"
    assert status["last_session"]["stop_reason"] == "max_bytes"


def test_debug_store_truncates_oversized_body(tmp_path):
    store = DebugStore(tmp_path)
    store.start(300, 2 * 1024 * 1024, ["request"], "operator")
    body = b"x" * (MAX_BODY_BYTES + 100)
    store.record("request", "req-1", {}, body, "text/plain")

    entry = store.entries()["items"][0]
    assert entry["body_bytes"] == MAX_BODY_BYTES
    assert entry["truncated"] == 1


async def build_engine_app(tmp_path, transport):
    app = create_app(str(tmp_path), background=False, transport=transport)
    engine = Engine(name="Debug engine", base_url="http://engine.test/v1")
    client = Client(name="Debug test")
    app.state.store.save(
        Configuration(
            engines=[engine],
            clients=[client],
            routes=[
                Route(
                    name="auto",
                    primary=Selector(engine_ids=[engine.id]),
                )
            ],
            discovery=Discovery(enabled=False, mdns=False),
        )
    )
    key = app.state.store.issue_key(client.id)
    await app.state.discovery.refresh()
    return app, key


def json_transport():
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "work"}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "model": "work",
                    "choices": [{"message": {"content": "ok"}}],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def stream_transport():
    async def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "work"}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    b"data: [DONE]\n\n"
                ),
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_debug_api_is_operator_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_ADMIN_TOKEN", "test-token")
    app = create_app(str(tmp_path), background=False, transport=json_transport())
    app.state.store.save(
        Configuration(security=Security(operator_auth_enabled=True))
    )
    app.state.store.complete_setup()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as http,
    ):
        denied = await http.get("/api/v1/debug/status")
        assert denied.status_code == 401
        allowed = await http.get(
            "/api/v1/debug/status",
            headers={"Authorization": "Bearer test-token"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["active"] is False

        started = await http.post(
            "/api/v1/debug/start",
            headers={"Authorization": "Bearer test-token"},
            json={"ttl_seconds": 300, "max_bytes": 1048576},
        )
        assert started.status_code == 200
        assert started.json()["session"]["status"] == "active"

        stopped = await http.post(
            "/api/v1/debug/stop",
            headers={"Authorization": "Bearer test-token"},
        )
        assert stopped.status_code == 200

        purged = await http.post(
            "/api/v1/debug/purge",
            headers={"Authorization": "Bearer test-token"},
        )
        assert purged.status_code == 200


@pytest.mark.asyncio
async def test_debug_api_captures_request_and_response(tmp_path):
    app, key = await build_engine_app(tmp_path, json_transport())
    origin = {"Origin": "http://127.0.0.1"}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as http,
    ):
        started = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={
                "ttl_seconds": 300,
                "max_bytes": 1048576,
                "scope": ["headers", "request", "response"],
            },
        )
        assert started.status_code == 200

        chat = await http.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert chat.status_code == 200

        await asyncio.sleep(0.1)
        entries = await http.get("/api/v1/debug/entries")
        assert entries.status_code == 200
        data = entries.json()
        assert data["total"] >= 2
        request_entry = next(
            item for item in data["items"] if item["direction"] == "request"
        )
        response_entry = next(
            item for item in data["items"] if item["direction"] == "response"
        )
        assert request_entry["headers"]["authorization"] == "[redacted]"
        assert "body" not in request_entry
        assert request_entry["body_preview"].startswith("{")
        assert "ok" in response_entry["body_preview"]

        detail = await http.get(f"/api/v1/debug/entries/{request_entry['id']}")
        assert detail.status_code == 200
        assert "model" in detail.json()["body"]

        status = await http.get("/api/v1/debug/status")
        assert status.json()["active"] is True
        assert status.json()["entries"] >= 2

        await http.post("/api/v1/debug/purge", headers=origin)
        status = await http.get("/api/v1/debug/status")
        assert status.json()["entries"] == 0


@pytest.mark.asyncio
async def test_debug_api_captures_stream(tmp_path):
    app, key = await build_engine_app(tmp_path, stream_transport())
    origin = {"Origin": "http://127.0.0.1"}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as http,
    ):
        started = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={
                "ttl_seconds": 300,
                "max_bytes": 1048576,
                "scope": ["request", "stream"],
            },
        )
        assert started.status_code == 200

        chat = await http.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert chat.status_code == 200
        assert "data:" in chat.text

        await asyncio.sleep(0.1)
        entries = await http.get("/api/v1/debug/entries")
        data = entries.json()
        directions = [item["direction"] for item in data["items"]]
        assert "request" in directions
        assert "stream" in directions
        stream_entry = next(
            item for item in data["items"] if item["direction"] == "stream"
        )
        assert "data:" in stream_entry["body_preview"]
        detail = await http.get(f"/api/v1/debug/entries/{stream_entry['id']}")
        assert detail.status_code == 200
        assert "data:" in detail.json()["body"]


@pytest.mark.asyncio
async def test_debug_api_validation(tmp_path):
    app, _key = await build_engine_app(tmp_path, json_transport())
    origin = {"Origin": "http://127.0.0.1"}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as http,
    ):
        bad_scope = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={"ttl_seconds": 300, "scope": ["headers", "cookies"]},
        )
        assert bad_scope.status_code == 422

        short_ttl = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={"ttl_seconds": 30},
        )
        assert short_ttl.status_code == 422

        small_cap = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={"max_bytes": 1024},
        )
        assert small_cap.status_code == 422

        defaults = await http.post(
            "/api/v1/debug/start",
            headers=origin,
            json={},
        )
        assert defaults.status_code == 200
        session = defaults.json()["session"]
        assert session["ttl_remaining"] <= 900
        assert session["max_bytes"] == 100 * 1024 * 1024
        assert session["scope"] == [
            "headers",
            "request",
            "response",
            "stream",
        ]
