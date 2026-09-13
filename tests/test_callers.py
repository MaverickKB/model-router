import json
import sqlite3
import time
from urllib.parse import quote

import httpx
import pytest

from gateway.app import create_app
from gateway.caller_records import caller_id
from gateway.network.report import write_json
from gateway.schema import Client, Configuration, Security
from gateway.store import Store
from gateway.topology import route_map


@pytest.mark.asyncio
async def test_observed_sources_are_distinct_from_shared_permission_policy(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Default access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    assert app.state.store.callers() == []
    empty_map = route_map(app.state.store.config(), [], [])
    assert empty_map["caller_routes"] == []
    assert empty_map["policy_routes"][0]["policy_id"] == policy.id
    assert empty_map["policies"][0]["observed_callers"] == []
    for address in ["192.0.2.10", "192.0.2.11"]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(address, 9000)),
            base_url="http://router.test",
        ) as http:
            response = await http.get(
                "/v1/models",
                headers={
                    "User-Agent": "AgentSDK/1",
                    "X-Forwarded-For": "spoofed",
                    "X-Router-Caller": "Unverified label",
                },
            )
            assert response.status_code == 200
    callers = Store(str(tmp_path)).callers()
    assert len(callers) == 2
    assert {c["source_address"] for c in callers} == {"192.0.2.10", "192.0.2.11"}
    assert all(
        c["name"] == "Unverified label"
        and c["identity_basis"] == "shared_access"
        and c["policy_id"] == policy.id
        for c in callers
    )
    assert "spoofed" not in json.dumps(callers)
    edges = route_map(app.state.store.config(), [], callers)["caller_routes"]
    assert {edge["caller_id"] for edge in edges} == {caller["id"] for caller in callers}
    assert all(edge["policy_id"] == policy.id for edge in edges)


@pytest.mark.asyncio
async def test_key_identity_uses_assigned_name_without_changing_optional_auth(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Operator-named test agent", kind="agent")
    shared = Client(name="Default permission policy")
    cfg = app.state.store.save(
        Configuration(
            clients=[policy, shared],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=shared.id,
            ),
        )
    )
    key = app.state.store.issue_key(policy.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.12", 1234)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "SDK/2",
                    "X-Router-Caller": "someone else",
                },
            )
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["name"] == policy.name and caller["identity_basis"] == "api_key"
    assert caller["reported_name"] == "someone else"
    assert key not in json.dumps(caller)
    assert app.state.store.config().security == cfg.security


@pytest.mark.asyncio
async def test_generic_transport_records_identifying_client_evidence_without_guessing_name(
    tmp_path,
):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    headers = {
        "User-Agent": "python-requests/2.33.0",
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "1.4.2",
        "X-Stainless-Runtime": "CPython",
        "X-Stainless-Runtime-Version": "3.12.5",
        "X-Stainless-OS": "macOS",
        "X-Stainless-Arch": "arm64",
        "X-Forwarded-For": "spoofed.example",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.30", 51001)),
        base_url="http://router.test",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200
    first = app.state.store.callers()[0]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.30", 51002)),
        base_url="http://router.test",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200
    caller = app.state.store.callers()[0]

    assert caller["name"] == "Unidentified caller"
    assert caller["identity_quality"] == "runtime_hints"
    assert caller["client_family"] == "Python requests"
    assert caller["client_version"] == "2.33.0"
    assert caller["client_runtime"] == "CPython"
    assert caller["client_os"] == "macOS"
    assert caller["client_arch"] == "arm64"
    assert caller["source_address"] == "192.0.2.30"
    assert caller["source_port"] == 51002
    assert caller["recent_source_ports"] == [51001, 51002]
    assert caller["request_count"] == 2
    assert caller["first_seen"] == first["first_seen"]
    assert "spoofed.example" not in json.dumps(caller)


@pytest.mark.asyncio
async def test_unknown_user_agent_is_not_promoted_to_a_client_library(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.31", 51003)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6)"},
            )
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["name"] == "Unidentified caller"
    assert caller["client_family"] == ""
    assert caller["client_version"] == ""
    assert caller["identity_quality"] == "transport_only"


@pytest.mark.asyncio
async def test_bare_python_user_agent_is_not_promoted_to_a_client_library(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.33", 51005)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get("/v1/models", headers={"User-Agent": "Python/3.12.5"})
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["client_family"] == ""
    assert caller["client_version"] == ""


@pytest.mark.asyncio
async def test_openai_json_scans_past_unknown_tokens(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.34", 51006)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "X-OpenAI-Client-User-Agent": '{"runtime":"foo/1.0","user_agent":"AsyncOpenAI/Python 1.54.0"}',
                    "X-Stainless-Package-Version": "1.54.0",
                },
            )
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["client_family"] == "OpenAI Python"
    assert caller["client_version"] == "1.54.0"


@pytest.mark.asyncio
async def test_async_openai_user_agent_uses_real_package_version(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.32", 51004)),
        base_url="http://router.test",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "User-Agent": "OpenAI/Python 1.54.0",
                    "X-OpenAI-Client-User-Agent": '{"user_agent":"AsyncOpenAI/Python 1.54.0"}',
                    "X-Stainless-Package-Version": "1.54.0",
                },
            )
        ).status_code == 200
    caller = app.state.store.callers()[0]
    assert caller["client_family"] == "OpenAI Python"
    assert caller["client_version"] == "1.54.0"


@pytest.mark.asyncio
async def test_console_preview_is_not_attributed_to_agent(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="An agent")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(operator_auth_enabled=False, client_auth_enabled=False),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    ) as http:
        response = await http.post(
            "/api/v1/try-route",
            json={
                "client_id": policy.id,
                "route": "auto",
                "message": "Do not retain this text",
            },
        )
        assert response.status_code == 503
    caller = app.state.store.callers()[0]
    event = app.state.store.events()[0]
    assert (
        caller["name"] == "Console route test"
        and caller["identity_basis"] == "operator_test"
    )
    assert event["caller"]["id"] == caller["id"]
    assert "Do not retain this text" not in json.dumps([caller, event])


@pytest.mark.asyncio
async def test_one_observation_survives_authentication_and_software_changes(tmp_path):
    app = create_app(str(tmp_path), background=False)
    shared = Client(name="Default permission policy")
    keyed = Client(name="Named key policy")
    config = app.state.store.save(
        Configuration(
            clients=[shared, keyed],
            security=Security(
                operator_auth_enabled=False, anonymous_client_id=shared.id
            ),
        )
    )
    key = app.state.store.issue_key(keyed.id)
    identifier = None
    first_seen = None
    for index, authorization in enumerate(
        (None, "Bearer placeholder", f"Bearer {key}", f"Bearer {key}")
    ):
        if index == 3:
            app.state.store.revoke_keys(keyed.id)
        headers = {
            "User-Agent": f"ExampleAgent/{index + 1}.0",
            "X-Router-Caller": "Writing agent",
        }
        if authorization:
            headers["Authorization"] = authorization
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app, client=("192.0.2.50", 52000 + index)
            ),
            base_url="http://router.test",
        ) as client:
            assert (await client.get("/v1/models", headers=headers)).status_code == 200
        records = app.state.store.callers()
        assert len(records) == 1
        caller = records[0]
        identifier = identifier or caller["id"]
        first_seen = first_seen or caller["first_seen"]
        assert caller["id"] == identifier
        assert caller["first_seen"] == first_seen
        assert caller["request_count"] == index + 1
        assert caller["software"] == f"ExampleAgent/{index + 1}.0"
        assert caller["source_port"] == 52000 + index
        if index == 2:
            assert caller["policy_id"] == keyed.id
            assert caller["identity_basis"] == "api_key"
            assert caller["name"] == keyed.name
        elif index in (1, 3):
            assert caller["policy_id"] is None
            assert caller["identity_basis"] == "unassigned"
            assert caller["name"] == "Writing agent"
    reopened = Store(str(tmp_path))
    assert reopened.callers() == records
    assert reopened.config() == config
    assert records[0]["recent_source_ports"] == [52000, 52001, 52002, 52003]
    assert key not in json.dumps(records)


@pytest.mark.asyncio
async def test_distinct_reported_applications_and_software_remain_distinct(tmp_path):
    app = create_app(str(tmp_path), background=False)
    app.state.store.save(Configuration(security=Security(operator_auth_enabled=False)))
    for label, software in (
        ("Writing", "ExampleAgent/1"),
        ("Coding", "ExampleAgent/1"),
        ("Writing", "DifferentAgent/1"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.51", 52010)),
            base_url="http://router.test",
        ) as client:
            assert (
                await client.get(
                    "/v1/models",
                    headers={"User-Agent": software, "X-Router-Caller": label},
                )
            ).status_code == 200
    assert len(app.state.store.callers()) == 3


def legacy_caller(identifier, seen, *, basis, policy_id, count, port, version="1.0"):
    return {
        "id": identifier,
        "source_address": "192.0.2.52",
        "software": f"python-requests/{version}",
        "reported_name": "",
        "name": "Policy name" if policy_id else "Unidentified caller",
        "identity_basis": basis,
        "policy_id": policy_id,
        "first_seen": seen - 10,
        "last_seen": seen,
        "request_count": count,
        "source_port": port,
    }


def test_existing_auth_dependent_rows_merge_once_without_rewriting_events(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()
    old = legacy_caller(
        "old-shared",
        now - 30,
        basis="shared_access",
        policy_id="shared",
        count=3,
        port=52020,
    )
    keyed = legacy_caller(
        "old-keyed", now - 20, basis="api_key", policy_id="keyed", count=4, port=52021
    )
    latest = legacy_caller(
        "old-invalid",
        now - 10,
        basis="unassigned",
        policy_id=None,
        count=2,
        port=52022,
        version="2.0",
    )
    other = dict(latest, id="different-source", source_address="192.0.2.53")
    keyed["recent_source_ports"] = [52020, 52021]
    event = {"id": "historical", "ts": now, "caller": old}
    store.event(event)
    with store.db:
        store.db.executemany(
            "INSERT INTO callers VALUES (?, ?, ?)",
            [
                (row["id"], row["last_seen"], json.dumps(row))
                for row in (old, keyed, latest, other)
            ],
        )
    config_before = store.db.execute("SELECT body FROM config").fetchall()
    reopened = Store(str(tmp_path))
    records = reopened.callers()
    assert len(records) == 2
    merged = next(row for row in records if row["source_address"] == "192.0.2.52")
    assert merged["id"] == caller_id(merged)
    assert merged["first_seen"] == old["first_seen"]
    assert merged["last_seen"] == latest["last_seen"]
    assert merged["request_count"] == 9
    assert merged["recent_source_ports"] == [52020, 52021, 52022]
    assert merged["identity_basis"] == "unassigned"
    assert merged["policy_id"] is None
    assert merged["name"] == "Unidentified caller"
    assert merged["software"] == "python-requests/2.0"
    raw_event = json.loads(
        reopened.db.execute("SELECT body FROM events WHERE id='historical'").fetchone()[
            0
        ]
    )
    assert raw_event == event
    presented_event = reopened.events()[0]
    assert presented_event["caller"]["source_key"] == "addr:192.0.2.52"
    assert presented_event["caller"]["source_label"] == "192.0.2.52"
    assert reopened.db.execute("SELECT body FROM config").fetchall() == config_before
    assert Store(str(tmp_path)).callers() == records


@pytest.mark.asyncio
async def test_operator_source_name_follows_reported_device_id_across_addresses(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    headers = {
        "User-Agent": "ExampleAgent/1.0",
        "X-Router-Device-Id": "workstation-device-key",
        "X-Router-Hostname": "workstation.local",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.60", 52100)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200
        source_key = app.state.store.callers()[0]["source_key"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 52199)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.put(
                f"/api/v1/caller-sources/{quote(source_key, safe='')}/name",
                json={"name": "Lab workstation"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 200

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.61", 52101)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200

    records = app.state.store.callers()
    assert len(records) == 1
    caller = records[0]
    assert caller["source_key"] == source_key
    assert caller["source_label"] == "Lab workstation"
    assert caller["source_label_source"] == "operator"
    assert caller["source_hostname"] == "workstation.local"
    assert caller["source_identity_quality"] == "reported_device"
    assert caller["recent_source_addresses"] == ["192.0.2.60", "192.0.2.61"]
    raw_caller = json.loads(
        app.state.store.db.execute("SELECT body FROM callers").fetchone()[0]
    )
    app.state.store.event(
        {"id": "device-event", "ts": time.time(), "caller": raw_caller}
    )
    presented_event = app.state.store.events()[0]
    assert presented_event["caller"]["source_label"] == "Lab workstation"
    stored_event = app.state.store.db.execute(
        "SELECT body FROM events WHERE id=?", (presented_event["id"],)
    ).fetchone()[0]
    assert "Lab workstation" not in stored_event


@pytest.mark.asyncio
async def test_address_only_source_name_does_not_follow_dhcp_change(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    headers = {"User-Agent": "ExampleAgent/1.0"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.62", 52102)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200
        source_key = app.state.store.callers()[0]["source_key"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 52200)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.put(
                f"/api/v1/caller-sources/{quote(source_key, safe='')}/name",
                json={"name": "Workshop tablet"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 200

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.63", 52103)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200

    records = sorted(app.state.store.callers(), key=lambda row: row["source_address"])
    assert len(records) == 2
    assert records[0]["source_address"] == "192.0.2.62"
    assert records[0]["source_label"] == "Workshop tablet"
    assert records[1]["source_address"] == "192.0.2.63"
    assert records[1]["source_label"] == "192.0.2.63"
    assert records[1]["source_label_source"] == "address"


@pytest.mark.asyncio
async def test_reported_hostname_is_display_evidence_not_dhcp_identity(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    headers = {
        "User-Agent": "ExampleAgent/1.0",
        "X-Router-Hostname": "shared-name.local",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.66", 52106)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200
        source_key = app.state.store.callers()[0]["source_key"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 52201)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.put(
                f"/api/v1/caller-sources/{quote(source_key, safe='')}/name",
                json={"name": "First source"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.67", 52107)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models", headers=headers)).status_code == 200

    records = sorted(app.state.store.callers(), key=lambda row: row["source_address"])
    assert [row["source_key"] for row in records] == [
        "addr:192.0.2.66",
        "addr:192.0.2.67",
    ]
    assert records[0]["source_label"] == "First source"
    assert records[1]["source_label"] == "shared-name.local"
    assert records[1]["source_label_source"] == "reported_hostname"


@pytest.mark.asyncio
async def test_discovered_hostname_labels_address_without_merging_sources(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    write_json(
        tmp_path / "discovery" / "network.json",
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.64",
                    "name": "workstation.local",
                    "status": "up",
                    "scope": "network",
                    "services": [],
                },
                {
                    "address": "192.0.2.65",
                    "name": "tablet.local",
                    "status": "up",
                    "scope": "network",
                    "services": [],
                },
            ],
            "error": "",
            "completed_at": time.time(),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.64", 52104)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models")).status_code == 200
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.65", 52105)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models")).status_code == 200

    records = sorted(app.state.store.callers(), key=lambda row: row["source_address"])
    assert len(records) == 2
    assert records[0]["source_key"] == "addr:192.0.2.64"
    assert records[0]["source_label"] == "workstation.local"
    assert records[0]["source_label_source"] == "discovered_hostname"
    assert records[0]["source_identity_quality"] == "address"
    assert records[1]["source_key"] == "addr:192.0.2.65"
    assert records[1]["source_label"] == "tablet.local"
    assert records[1]["source_label_source"] == "discovered_hostname"


@pytest.mark.asyncio
async def test_discovered_hostname_can_label_a_device_key_without_becoming_identity(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    write_json(
        tmp_path / "discovery" / "network.json",
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.68",
                    "name": "lab-device.local",
                    "status": "up",
                    "scope": "network",
                    "services": [],
                }
            ],
            "error": "",
            "completed_at": time.time(),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.68", 52108)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "User-Agent": "ExampleAgent/1.0",
                    "X-Router-Device-Id": "lab-device-id",
                },
            )
        ).status_code == 200

    caller = app.state.store.callers()[0]
    assert caller["source_key"].startswith("device:")
    assert caller["source_label"] == "lab-device.local"
    assert caller["source_label_source"] == "discovered_hostname"
    assert caller["source_identity_quality"] == "reported_device"


@pytest.mark.asyncio
async def test_network_hardware_identity_carries_name_across_dhcp_change(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    report_path = tmp_path / "discovery" / "network.json"
    write_json(
        report_path,
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.70",
                    "hardware_address": "00:11:22:33:44:55",
                    "name": "studio-mac.local",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                    "services": [],
                }
            ],
            "error": "",
            "completed_at": time.time(),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.70", 52110)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.get(
                "/v1/models",
                headers={
                    "X-Router-Device-Id": "caller-device-id",
                    "X-Router-Hostname": "studio-mac.local",
                    "X-Stainless-OS": "MacOS",
                },
            )
        ).status_code == 200
    source_key = app.state.store.callers()[0]["source_key"]
    assert source_key.startswith("hardware:")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 52111)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.put(
                f"/api/v1/caller-sources/{quote(source_key, safe='')}/name",
                json={"name": "Studio Mac"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 200

    write_json(
        report_path,
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.71",
                    "hardware_address": "00-11-22-33-44-55",
                    "name": "studio-mac.local",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                    "services": [],
                },
                {
                    "address": "192.0.2.70",
                    "hardware_address": "66:77:88:99:AA:BB",
                    "name": "studio-mac.local",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                    "services": [],
                },
            ],
            "error": "",
            "completed_at": time.time(),
        },
    )
    for address, port in (("192.0.2.71", 52112), ("192.0.2.70", 52113)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(address, port)),
            base_url="http://localhost",
        ) as http:
            assert (
                await http.get(
                    "/v1/models",
                    headers={
                        "X-Router-Device-Id": "caller-device-id",
                        "X-Router-Hostname": "studio-mac.local",
                        "X-Stainless-OS": "MacOS",
                    },
                )
            ).status_code == 200

    records = app.state.store.callers()
    named = next(record for record in records if record["source_key"] == source_key)
    other = next(record for record in records if record["source_key"] != source_key)
    assert named["source_label"] == "Studio Mac"
    assert named["source_identity_quality"] == "network_hardware"
    assert named["recent_source_addresses"] == ["192.0.2.70", "192.0.2.71"]
    assert other["source_key"].startswith("hardware:")
    assert other["source_key"] != source_key
    assert other["source_label"] == "studio-mac.local"
    assert other["source_label_source"] == "reported_hostname"
    assert not (tmp_path / "discovery" / "request.json").exists()


@pytest.mark.asyncio
async def test_hardware_identity_never_inherits_an_old_address_bound_name(tmp_path):
    app = create_app(str(tmp_path), background=False)
    policy = Client(name="Shared access")
    app.state.store.save(
        Configuration(
            clients=[policy],
            security=Security(
                operator_auth_enabled=False,
                client_auth_enabled=False,
                anonymous_client_id=policy.id,
            ),
        )
    )
    address = "192.0.2.78"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(address, 52114)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models")).status_code == 200
    address_source_key = app.state.store.callers()[0]["source_key"]
    assert address_source_key == f"addr:{address}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 52115)),
        base_url="http://localhost",
    ) as http:
        assert (
            await http.put(
                f"/api/v1/caller-sources/{quote(address_source_key, safe='')}/name",
                json={"name": "Old IP label"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 200

    write_json(
        tmp_path / "discovery" / "network.json",
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": address,
                    "hardware_address": "00:11:22:33:44:99",
                    "name": "replacement-device.local",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                }
            ],
            "completed_at": time.time(),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(address, 52116)),
        base_url="http://localhost",
    ) as http:
        assert (await http.get("/v1/models")).status_code == 200

    records = {record["source_key"]: record for record in app.state.store.callers()}
    assert records[address_source_key]["source_label"] == "Old IP label"
    hardware = next(
        record for key, record in records.items() if key != address_source_key
    )
    assert hardware["source_key"].startswith("hardware:")
    assert hardware["source_label"] == "replacement-device.local"
    assert hardware["source_label_source"] == "discovered_hostname"


def test_discovery_hardware_identity_rejects_ambiguous_invalid_and_stale_evidence(
    tmp_path,
):
    store = Store(str(tmp_path))
    report_path = tmp_path / "discovery" / "network.json"
    write_json(
        report_path,
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.73",
                    "hardware_address": "not-a-mac-001122334455",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                },
                {
                    "address": "192.0.2.74",
                    "hardware_address": "00:11:22:33:44:66",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                },
                {
                    "address": "192.0.2.75",
                    "hardware_address": "00:11:22:33:44:66",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                },
            ],
            "completed_at": time.time(),
        },
    )
    assert store.source_identity("192.0.2.73", {})["source_key"] == "addr:192.0.2.73"
    assert store.source_identity("192.0.2.74", {})["source_key"] == "addr:192.0.2.74"
    assert store.source_identity("192.0.2.75", {})["source_key"] == "addr:192.0.2.75"

    write_json(
        report_path,
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.76",
                    "hardware_address": "00:11:22:33:44:77",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                }
            ],
            "completed_at": time.time()
            - store.config().discovery.network_interval_seconds
            - 1,
        },
    )
    assert store.source_identity("192.0.2.76", {})["source_key"] == "addr:192.0.2.76"
    assert not (tmp_path / "discovery" / "request.json").exists()


def test_discovery_hardware_identity_expires_without_a_new_snapshot(tmp_path, monkeypatch):
    store = Store(str(tmp_path))
    completed_at = time.time()
    write_json(
        tmp_path / "discovery" / "network.json",
        {
            "phase": "complete",
            "hosts": [
                {
                    "address": "192.0.2.77",
                    "hardware_address": "00:11:22:33:44:88",
                    "status": "up",
                    "scope": "network",
                    "scan_complete": True,
                }
            ],
            "completed_at": completed_at,
        },
    )
    assert store.source_identity("192.0.2.77", {})["source_key"].startswith(
        "hardware:"
    )

    monkeypatch.setattr(
        "gateway.network.caller_evidence.time.time",
        lambda: completed_at + store.config().discovery.network_interval_seconds + 1,
    )
    assert store.source_identity("192.0.2.77", {})["source_key"] == "addr:192.0.2.77"


def test_caller_migration_rolls_back_as_one_transaction(tmp_path):
    store = Store(str(tmp_path))
    row = legacy_caller(
        "legacy-id",
        time.time(),
        basis="shared_access",
        policy_id=None,
        count=1,
        port=52023,
    )
    with store.db:
        store.db.execute(
            "INSERT INTO callers VALUES (?, ?, ?)",
            (row["id"], row["last_seen"], json.dumps(row)),
        )
        store.db.execute(
            "CREATE TRIGGER migration_failure BEFORE INSERT ON callers BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        Store(str(tmp_path))
    assert json.loads(store.db.execute("SELECT body FROM callers").fetchone()[0]) == row
    assert store.callers()[0]["source_key"] == "addr:192.0.2.52"


def test_late_observation_preserves_latest_auth_and_its_own_event_evidence(tmp_path):
    store = Store(str(tmp_path))
    now = time.time()
    keyed = legacy_caller(
        "unused", now - 10, basis="api_key", policy_id="old-key", count=1, port=52024
    )
    unkeyed = legacy_caller(
        "unused", now, basis="unassigned", policy_id=None, count=1, port=52025
    )
    for row in (keyed, unkeyed):
        row["id"] = caller_id(row)
    store.observe_caller(unkeyed)
    store.observe_caller(keyed)
    stored = store.callers()[0]
    assert stored["identity_basis"] == "unassigned"
    assert stored["policy_id"] is None
    assert stored["name"] == "Unidentified caller"
    assert stored["request_count"] == 2
    assert stored["recent_source_ports"] == [52024, 52025]
    assert keyed["identity_basis"] == "api_key"
    assert keyed["policy_id"] == "old-key"


@pytest.mark.parametrize(
    "older,newer",
    [
        ("python-requests/2.31.0", "python-requests/2.33.0"),
        ("OpenAI/Python 1.54.0", "OpenAI/Python 2.24.0"),
        ("ExampleAgent/1.0", "ExampleAgent/2.0"),
    ],
)
def test_caller_software_upgrades_keep_identity(older, newer):
    row = {"source_address": "192.0.2.54", "reported_name": "", "software": older}
    assert caller_id(row) == caller_id(dict(row, software=newer))
    assert caller_id(row) == caller_id(
        dict(row, identity_hints={"stainless_os": "updated"})
    )


def test_composite_application_strings_do_not_merge_on_the_shared_library():
    row = {
        "source_address": "192.0.2.54",
        "reported_name": "",
        "software": "WritingApp/1.0 python-requests/2.0",
    }
    assert caller_id(row) != caller_id(
        dict(row, software="CodingApp/1.0 python-requests/2.0")
    )
