"""Authentication probes model the actual tunnel's TCP peer and Host header."""

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Discovery
from gateway.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer,origin",
    [
        ("127.0.0.1", "http://localhost:18790"),
        ("::1", "http://[::1]:18790"),
        ("192.0.2.10", "http://router.test"),
    ],
)
async def test_authenticated_mode_covers_tunnels_with_a_separate_canonical_url(
    tmp_path, peer, origin, monkeypatch
):
    monkeypatch.setenv("MODEL_ROUTER_PUBLIC_URL", "https://canonical.test:8690")
    calls = []

    async def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    app.state.store.save(Configuration(discovery=Discovery(targets=["192.0.2.0/24"])))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 5000)), base_url=origin
        ) as client,
    ):
        for path in ("/api/state", "/api/discover", "/v1/gateway/register"):
            result = (
                await client.get(path)
                if path.endswith("state")
                else await client.post(
                    path, json={"base_url": "http://192.0.2.11:8000/v1"}
                )
            )
            assert result.status_code == 401
        assert not calls
        token = (tmp_path / "operator-bootstrap.key").read_text().strip()
        assert (
            await client.post(
                "/api/login", json={"token": token}, headers={"Origin": origin}
            )
        ).status_code == 200
        assert (await client.get("/api/state")).status_code == 200
        assert (await client.get("/api/state")).json()["operator_url"] == (
            "https://canonical.test:8690"
        )
        assert (await client.post("/api/discover")).status_code == 403
        assert (
            await client.post("/api/logout", headers={"Origin": origin})
        ).status_code == 200
        assert (await client.get("/api/state")).status_code == 401
        health = (await client.get("/health")).json()
        assert set(health) == {"ok", "service"}


@pytest.mark.asyncio
async def test_login_has_a_bounded_rate_and_rotates_sessions(tmp_path):
    app = create_app(str(tmp_path), background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers={"Origin": "http://router.test"},
        ) as client,
    ):
        token = (tmp_path / "operator-bootstrap.key").read_text().strip()
        await client.post("/api/login", json={"token": token})
        old = client.cookies.get("router_operator")
        await client.post("/api/login", json={"token": token})
        current = client.cookies.get("router_operator")
        assert current != old
        assert (
            await client.get("/api/state", headers={"Cookie": f"router_operator={old}"})
        ).status_code == 401
        for _ in range(8):
            assert (
                await client.post("/api/login", json={"token": "wrong"})
            ).status_code == 401
        assert (
            await client.post("/api/login", json={"token": "wrong"})
        ).status_code == 429
        assert len(app.state.identity.sessions) == 1


def test_database_contains_ciphertext_and_salted_key_verifiers(tmp_path):
    store = Store(str(tmp_path / "router"))
    store.set_secret("provider", "unique-provider-secret")
    client = Client(name="Caller")
    store.save(Configuration(clients=[client]))
    key = store.issue_key(client.id)
    assert store.key_client(key) == client.id
    assert store.key_client("invalid") is None
    encoded = store.db.execute("SELECT value FROM secrets").fetchone()[0]
    verifier = store.db.execute("SELECT verifier FROM client_credentials").fetchone()[0]
    assert encoded.startswith("fernet:") and "unique-provider-secret" not in encoded
    assert verifier.startswith("$argon2id$") and key not in verifier
    assert store.cipher.key_file.parent != store.directory
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert b"unique-provider-secret" not in (store.directory / "router.db").read_bytes()
    assert Store(str(store.directory)).secret("provider") == "unique-provider-secret"
    store.revoke_keys(client.id)
    assert store.key_client(key) is None


@pytest.mark.asyncio
async def test_network_client_authentication_requires_explicit_opt_in(tmp_path):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(
        Configuration(
            clients=[Client(name="Restricted source", source_networks=["127.0.0.0/8"])]
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
        ) as client,
    ):
        assert (await client.get("/v1/models")).status_code == 401


@pytest.mark.asyncio
async def test_access_toggles_preserve_agent_keys_and_browser_across_restart(tmp_path):
    from gateway.schema import Security

    app = create_app(str(tmp_path), background=False)
    caller = Client(name="Existing agent")
    shared = Client(name="Shared policy")
    store = app.state.store
    store.save(
        Configuration(
            clients=[caller, shared],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=shared.id,
            ),
        )
    )
    key = store.issue_key(caller.id)
    origin = "http://localhost:18790"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url=origin,
            headers={"Origin": origin},
        ) as browser,
    ):
        assert (await browser.get("/api/state")).status_code == 200
        assert (await browser.get("/v1/models")).status_code == 200
        assert (
            await browser.get("/v1/models", headers={"Authorization": "Bearer invalid"})
        ).status_code == 401
        config = store.config()
        config.security.operator_auth_enabled = True
        config.security.client_auth_enabled = True
        assert (
            await browser.put("/api/config", json=config.model_dump())
        ).status_code == 200
        cookie = browser.cookies.get("router_operator")
        assert cookie
        assert (await browser.get("/api/state")).status_code == 200
        assert (await browser.get("/v1/models")).status_code == 401
        assert (
            await browser.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        ).status_code == 200
    restarted = create_app(str(tmp_path), background=False)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted, client=("127.0.0.1", 1)),
            base_url=origin,
            headers={"Origin": origin, "Cookie": f"router_operator={cookie}"},
        ) as browser,
    ):
        assert (await browser.get("/api/state")).status_code == 200
        assert (
            await browser.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        ).status_code == 200
        config = restarted.state.store.config()
        config.security.operator_auth_enabled = False
        config.security.client_auth_enabled = False
        assert (
            await browser.put("/api/config", json=config.model_dump())
        ).status_code == 200
        assert (
            await browser.get("/v1/models", headers={"Cookie": ""})
        ).status_code == 200
        assert restarted.state.store.key_client(key) == caller.id


def test_legacy_installation_upgrades_without_changing_keys_or_source_policies(
    tmp_path,
):
    import hashlib
    import json
    import sqlite3

    tmp_path.mkdir(exist_ok=True)
    caller = Client(name="Installed caller", source_networks=["192.0.2.0/24"])
    raw = Configuration(clients=[caller]).model_dump()
    raw["compatibility"] = {"legacy_observers": True}
    for field in ("schema_version", "security"):
        raw.pop(field)
    raw["clients"][0].pop("allow_network_auth")
    raw["discovery"].update(
        enabled=True,
        targets=["192.0.2.0/24"],
        port_range="1-65535",
        auto_register=True,
    )
    for field in ("scanner", "inspect_all_open_ports", "include_loopback"):
        raw["discovery"].pop(field)
    key = "existing-high-entropy-agent-key"
    with sqlite3.connect(tmp_path / "router.db") as db:
        db.executescript(
            "CREATE TABLE config(id INTEGER PRIMARY KEY, body TEXT); CREATE TABLE client_keys(digest TEXT PRIMARY KEY, client_id TEXT);"
        )
        db.execute("INSERT INTO config VALUES(1,?)", (json.dumps(raw),))
        db.execute(
            "INSERT INTO client_keys VALUES(?,?)",
            (hashlib.sha256(key.encode()).hexdigest(), caller.id),
        )
    store = Store(str(tmp_path))
    assert store.has_key(caller.id)
    assert not store.config().security.client_auth_enabled
    assert not store.config().security.operator_auth_enabled
    assert store.config().clients[0].allow_network_auth
    assert not hasattr(store.config(), "compatibility")
    assert store.config().upgraded_from_schema == 0
    assert store.config().discovery.enabled
    assert store.config().discovery.auto_register
    assert store.config().discovery.inspect_all_open_ports
    assert store.config().discovery.include_loopback
    assert store.config().discovery.scanner == "nmap"
    assert store.config().discovery.port_range == "1-65535"
    assert store.key_client(key) == caller.id
    assert not store.db.execute("SELECT * FROM client_keys").fetchall()
    assert (
        store.db.execute("SELECT verifier FROM client_credentials")
        .fetchone()[0]
        .startswith("$argon2id$")
    )
    # The installation notice survives ordinary saves and restart, even for an
    # older editor that omits provenance. It never changes the saved policy.
    edited = store.config()
    edited.upgraded_from_schema = None
    store.save(edited)
    restarted = Store(str(tmp_path))
    assert restarted.config().upgraded_from_schema == 0
    assert restarted.config().discovery == store.config().discovery
    assert not restarted.config().security.operator_auth_enabled
    assert restarted.key_client(key) == caller.id


@pytest.mark.asyncio
async def test_operator_bearer_polling_and_rotation_do_not_change_client_keys(tmp_path):
    app = create_app(str(tmp_path), background=False)
    caller = Client(name="Agent")
    app.state.store.save(Configuration(clients=[caller]))
    client_key = app.state.store.issue_key(caller.id)
    token = (tmp_path / "operator-bootstrap.key").read_text().strip()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {token}"},
        ) as browser,
    ):
        for _ in range(30):
            assert (await browser.get("/api/state")).status_code == 200
        result = await browser.post("/api/operator/key")
        assert result.status_code == 200
        assert (await browser.get("/api/state")).status_code == 401
        browser.headers["Authorization"] = f"Bearer {result.json()['key']}"
        assert (await browser.get("/api/state")).status_code == 200
        assert app.state.store.key_client(client_key) == caller.id
        assert not (tmp_path / "operator-bootstrap.key").exists()


async def test_versioned_api_bounds_json_and_paginates_network(tmp_path):
    from gateway.network.report import publish
    from gateway.schema import Security

    app = create_app(str(tmp_path), background=False)
    app.state.store.save(Configuration(security=Security(operator_auth_enabled=False)))
    publish(
        app.state.store.discovery_directory,
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": f"192.0.2.{n}",
                    "name": "",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                    "ports": [],
                    "services": [],
                }
                for n in range(1, 81)
            ],
            "completed_at": 1,
        },
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
            base_url="http://localhost",
        ) as browser,
    ):
        state = (await browser.get("/api/v1/state")).json()
        assert state["network"]["hosts"] == []
        assert state["network"]["counts"]["addresses"] == 80
        page = (await browser.get("/api/v1/network?offset=25&limit=25")).json()
        assert len(page["hosts"]) == 25 and page["hosts"][0]["address"] == "192.0.2.26"
        assert (await browser.get("/api/state")).status_code == 200
        assert (
            await browser.post("/api/v1/login", content=b"x" * 9000)
        ).status_code == 413
        spec = (await browser.get("/openapi.json")).json()
        assert "/api/v1/config" in spec["paths"] and "/api/config" not in spec["paths"]
