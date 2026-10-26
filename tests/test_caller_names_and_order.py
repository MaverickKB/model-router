"""A caller list has to answer three questions without the caller's help.

Ken, 2026-10-26, reading the live Routes page: "last seen 4 days ago. right at
the top and no clue who that is."

Three separate faults were measured on the running router that day:

 1. rows were sorted by ``(name, source_address, id)`` and every unkeyed row's
    name was the same placeholder, so the sort collapsed to address order and
    recency never entered into it;
 2. a row's name was written when the caller was first seen, before any lookup
    had run, so it kept the placeholder for the life of the row;
 3. the resolver had no reverse-DNS source, so it could not name the router's
    own host -- ``192.0.2.75`` resolves to ``router-host`` through NSS and avahi
    answers nothing for it.

Each test below fails on the tree that shipped those faults.
"""

import json
import time

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Security
from gateway.source_names import SourceNames, dns_name
from gateway.store import Store


def fake_runner(answers):
    def run(argv, timeout=2.0):
        return answers.get(tuple(argv), b"")

    return run


def insert_caller(store, caller_id, body):
    row = {
        "id": caller_id,
        "seen": body.pop("seen", time.time()),
        "source_label": "",
        "source_label_source": "address",
        "source_hostname": "",
        "reported_name": "",
        **body,
    }
    with store.lock:
        store.db.execute(
            "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
            (caller_id, row["seen"], json.dumps(row)),
        )
        store.db.commit()


def cold_store(tmp_path, runner):
    store = Store(str(tmp_path))
    store.source_names = SourceNames(ttl=60, runner=runner)
    return store


# 1. reverse DNS is a resolver source, and it names the router's own host.
def test_reverse_dns_names_an_address_that_tailnet_and_mdns_do_not(tmp_path):
    assert dns_name(
        "192.0.2.75",
        fake_runner({("getent", "hosts", "192.0.2.75"): b"192.0.2.75   router-host\n"}),
    ) == "router-host"


def test_a_pointer_back_at_the_address_carries_no_name(tmp_path):
    # Some resolvers answer "<ip> <ip>". That is not a name and must not be one.
    assert dns_name(
        "192.0.2.75",
        fake_runner({("getent", "hosts", "192.0.2.75"): b"192.0.2.75   192.0.2.75\n"}),
    ) == ""


def test_a_public_address_is_never_named_from_reverse_dns(tmp_path):
    """A public address reverse-resolves to an ISP pointer, which is worse than blank."""
    asked = []

    def runner(argv, timeout=2.0):
        asked.append(tuple(argv))
        if argv[:2] == ["getent", "hosts"]:
            return b"8.8.8.8   dns.google\n"
        return b""

    names = SourceNames(ttl=60, runner=runner)
    names.refresh(["8.8.8.8"])
    assert names.name("8.8.8.8") == ""
    assert not any(a[:2] == ("getent", "hosts") for a in asked)


# 2. the placeholder name is replaced on the read path, once a lookup has run.
def test_an_unnamed_row_gets_its_name_when_the_lookup_lands(tmp_path):
    store = cold_store(
        tmp_path,
        fake_runner({("getent", "hosts", "192.0.2.75"): b"192.0.2.75   router-host\n"}),
    )
    insert_caller(
        store,
        "x",
        {
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "Unidentified caller",
        },
    )
    # Before the refresh the row is honestly unnamed.
    assert store.callers()[0]["name"] == "Unidentified caller"

    store.source_names.refresh(["192.0.2.75"])

    caller = store.callers()[0]
    assert caller["name"] == "router-host"
    assert caller["source_label"] == "router-host"


def test_a_name_the_caller_reported_is_never_overwritten(tmp_path):
    store = cold_store(
        tmp_path,
        fake_runner({("getent", "hosts", "192.0.2.75"): b"192.0.2.75   router-host\n"}),
    )
    insert_caller(
        store,
        "x",
        {
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "build-runner-7",
            "reported_name": "build-runner-7",
        },
    )
    store.source_names.refresh(["192.0.2.75"])
    assert store.callers()[0]["name"] == "build-runner-7"


# 3. the list is ordered by recency, so a stale row cannot sit at the top.
def test_one_host_running_two_clients_does_not_put_the_stale_row_on_top(tmp_path):
    """A single address legitimately makes one row per client program.

    Those rows tie on address, so the tiebreak decides what the operator reads
    first. It has to be recency.
    """
    store = cold_store(tmp_path, fake_runner({}))
    now = time.time()
    insert_caller(
        store,
        "aaa-stale",
        {
            "seen": now - 4 * 86400,
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "Unidentified caller",
            "software": "Python-urllib/3.12",
            "last_seen": now - 4 * 86400,
        },
    )
    insert_caller(
        store,
        "zzz-fresh",
        {
            "seen": now,
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "Unidentified caller",
            "software": "python-httpx/0.28.1",
            "last_seen": now,
        },
    )
    rows = store.callers()
    assert [r["id"] for r in rows] == ["zzz-fresh", "aaa-stale"]


@pytest.mark.asyncio
async def test_the_served_state_puts_the_recent_row_first(tmp_path):
    """The fault was in the endpoint's own sort, not in the store.

    ``store.callers()`` was already newest-first, so a test over the store alone
    would pass on the broken tree. This goes through /api/state, where the sort
    by a constant placeholder name used to discard recency entirely.
    """
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
    store = app.state.store
    store.source_names = SourceNames(ttl=60, runner=fake_runner({}))
    now = time.time()
    insert_caller(
        store,
        "aaa-stale",
        {
            "seen": now - 4 * 86400,
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "Unidentified caller",
            "software": "Python-urllib/3.12",
            "last_seen": now - 4 * 86400,
        },
    )
    insert_caller(
        store,
        "zzz-fresh",
        {
            "seen": now,
            "source_address": "192.0.2.75",
            "source_key": "addr:192.0.2.75",
            "name": "Unidentified caller",
            "software": "python-httpx/0.28.1",
            "last_seen": now,
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url="http://localhost",
    ) as http:
        payload = (await http.get("/api/state")).json()

    served = [row["id"] for row in payload["observed_callers"]]
    assert served == ["zzz-fresh", "aaa-stale"]


# 4. the two values that govern this view are settings, not literals.
#
# Ken, 2026-10-26: "there are settings that are governing this view and i have
# no controls for them in the Settings UI either." He was right: how long a
# departed source stayed on the page, and the hard ceiling on stored rows, were
# both literals in store.py. Nobody could see them, let alone change them.
#
# These tests fail if either value goes back to being hardcoded, because they
# set a non-default and require the read path and the prune to honour it.


def test_retention_days_decides_how_long_a_departed_source_stays_visible(tmp_path):
    store = cold_store(tmp_path, fake_runner({}))
    now = time.time()
    insert_caller(
        store,
        "gone-days-ago",
        {
            "seen": now - 3 * 86400,
            "last_seen": now - 3 * 86400,
            "source_address": "192.0.2.77",
            "source_key": "addr:192.0.2.77",
        },
    )

    # Default of 7 days: a 3-day-old source is still listed.
    assert [r["id"] for r in store.callers()] == ["gone-days-ago"]

    # Lower it to 2 days and the same row leaves the view, with no code change.
    config = store.config()
    config.discovery.caller_retention_days = 2
    store.save(config)
    assert store.callers() == []


def test_retention_days_drives_the_prune_not_just_the_read(tmp_path):
    """The page and the stored rows must not disagree about the window."""
    store = cold_store(tmp_path, fake_runner({}))
    now = time.time()
    insert_caller(
        store,
        "departed",
        {
            "seen": now - 3 * 86400,
            "last_seen": now - 3 * 86400,
            "source_address": "192.0.2.78",
            "source_key": "addr:192.0.2.78",
        },
    )
    config = store.config()
    config.discovery.caller_retention_days = 2
    store.save(config)
    # A new observation runs the prune under the configured window. The
    # aggregate fields are what observe_caller carries forward from the prior
    # row, so supply them the way a real observation does.
    store.observe_caller(
        {
            "id": "fresh",
            "seen": now,
            "last_seen": now,
            "source_address": "192.0.2.79",
            "source_key": "addr:192.0.2.79",
            "first_seen": now,
            "request_count": 1,
            "recent_source_ports": [],
            "recent_source_addresses": ["192.0.2.79"],
        }
    )
    stored = [row[0] for row in store.db.execute("SELECT id FROM callers")]
    assert "departed" not in stored
    assert "fresh" in stored


def test_row_limit_is_a_setting_and_caps_the_listed_rows(tmp_path):
    store = cold_store(tmp_path, fake_runner({}))
    now = time.time()
    for index in range(12):
        insert_caller(
            store,
            "row-%02d" % index,
            {
                "seen": now - index,
                "last_seen": now - index,
                "source_address": "192.0.2.%d" % (100 + index),
                "source_key": "addr:192.0.2.%d" % (100 + index),
            },
        )
    assert len(store.callers()) == 12

    config = store.config()
    config.discovery.caller_row_limit = 5
    store.save(config)
    rows = store.callers()
    assert len(rows) == 5
    # The five most recent survive; the cap trims the tail, never the head.
    assert [r["id"] for r in rows] == ["row-00", "row-01", "row-02", "row-03", "row-04"]
