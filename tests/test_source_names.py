"""A caller row and a scanned host must be able to say who they are.

These tests exist because the previous attempt called an honest blank a fix:
when no name arrived, the router printed the address and labelled the row
"No agent identity reported". The router should resolve known fleet addresses
without requiring the caller to volunteer a name.
"""

import json
import time

from gateway.network.report import Phase, publish
from gateway.network.views import network_view
from gateway.source_names import (
    SourceNames,
    engine_names,
    mdns_name,
    tailnet_names,
)
from gateway.store import Store


def fake_runner(answers):
    """A subprocess stand-in: argv -> stdout bytes, empty for anything unlisted."""

    def run(argv, timeout=2.0):
        return answers.get(tuple(argv), b"")

    return run


TAILSCALE_STATUS = json.dumps(
    {
        "Self": {
            "DNSName": "router-host.example.ts.net.",
            "HostName": "router-host",
            "TailscaleIPs": ["203.0.113.10", "fd7a:115c:a1e0::1"],
        },
        "Peer": {
            "a": {
                "DNSName": "example-laptop.example.ts.net.",
                "HostName": "Example Laptop",
                "TailscaleIPs": ["192.0.2.50"],
            },
            "b": {
                "DNSName": "",
                "HostName": "grok-bot",
                "TailscaleIPs": ["100.90.114.127"],
            },
        },
    }
).encode()


def test_tailnet_names_reads_one_call_and_uses_the_hostname_not_the_display_name():
    seen = []

    def run(argv, timeout=2.0):
        seen.append(tuple(argv))
        return TAILSCALE_STATUS

    names = tailnet_names(run)
    assert seen == [("tailscale", "status", "--json")]
    # The display name contains an apostrophe and spaces: not a hostname.
    assert names["192.0.2.50"] == "example-laptop"
    assert names["203.0.113.10"] == "router-host"
    assert names["100.90.114.127"] == "grok-bot"
    assert "fd7a:115c:a1e0::1" not in names


def test_tailnet_names_is_empty_when_the_cli_is_absent_or_fails():
    assert tailnet_names(fake_runner({})) == {}
    assert tailnet_names(lambda argv, timeout=2.0: b"not json") == {}


def test_mdns_name_reads_the_avahi_answer():
    run = fake_runner({("avahi-resolve-address", "192.0.2.91"): b"192.0.2.91\tspark-host.local\n"})
    assert mdns_name("192.0.2.91", run) == "spark-host.local"
    assert mdns_name("192.0.2.33", run) == ""


def test_engine_names_uses_the_operator_table_and_skips_dns_names():
    engines = [
        {"base_url": "http://192.0.2.91:8888/v1", "name": "Spark 1"},
        {"base_url": "http://127.0.0.1:8149", "name": "System One"},
        {"base_url": "https://api.x.ai/v1", "name": "Cairn voice"},
        {"base_url": "http://engine.internal:8000/v1", "name": "Already named"},
        {"base_url": "", "name": "No address"},
    ]
    assert engine_names(engines) == {"192.0.2.91": "Spark 1", "127.0.0.1": "System One"}


def test_refresh_resolves_tailnet_then_mdns_then_the_engine_table():
    run = fake_runner(
        {
            ("tailscale", "status", "--json"): TAILSCALE_STATUS,
            ("avahi-resolve-address", "192.0.2.91"): b"192.0.2.91\tspark-host.local\n",
        }
    )
    names = SourceNames(ttl=60, runner=run)
    names.set_engines([{"base_url": "http://192.0.2.91:8888/v1", "name": "Spark 1"}])
    names.refresh(["192.0.2.50", "192.0.2.91", "100.68.124.39"])
    # Tailnet answers first; a host that announces over mDNS answers next;
    # an address no source knows stays an honest blank.
    assert names.name("192.0.2.50") == "example-laptop"
    assert names.name("192.0.2.91") == "spark-host.local"
    assert names.name("100.68.124.39") == ""


def test_loopback_is_never_named_and_no_lookup_is_made_for_it():
    calls = []

    def run(argv, timeout=2.0):
        calls.append(tuple(argv))
        return TAILSCALE_STATUS

    names = SourceNames(ttl=60, runner=run)
    names.refresh(["127.0.0.1", "::1"])
    assert names.name("127.0.0.1") == ""
    assert names.name("::1") == ""
    # Only the single tailnet table call; no per-address lookups at all.
    assert calls == [("tailscale", "status", "--json")]


def test_a_read_never_runs_a_lookup_and_an_expired_entry_stops_answering():
    calls = []

    def run(argv, timeout=2.0):
        calls.append(tuple(argv))
        return TAILSCALE_STATUS

    names = SourceNames(ttl=0.01, runner=run)
    names.refresh(["192.0.2.50"])
    assert names.name("192.0.2.50") == "example-laptop"
    before = len(calls)
    # Unknown address: answered from the cache, nothing spawned.
    assert names.name("100.90.114.127") == ""
    assert len(calls) == before
    time.sleep(0.05)
    assert names.name("192.0.2.50") == ""
    assert len(calls) == before


def test_a_store_names_a_caller_from_the_cache_not_from_the_caller(tmp_path):
    store = Store(str(tmp_path))
    store.source_names = SourceNames(
        ttl=60,
        runner=fake_runner({("tailscale", "status", "--json"): TAILSCALE_STATUS}),
    )
    store.source_names.refresh(["192.0.2.50"])
    store.source_names.set_engines([])
    # A caller that reported nothing at all, exactly like the live rows.
    with store.lock:
        store.db.execute(
            "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
            (
                "x",
                time.time(),
                json.dumps(
                    {
                        "id": "x",
                        "seen": time.time(),
                        "source_address": "192.0.2.50",
                        "source_key": "addr:192.0.2.50",
                        "source_label": "192.0.2.50",
                        "source_label_source": "address",
                        "source_hostname": "",
                        "reported_name": "",
                    }
                ),
            ),
        )
        store.db.commit()
    caller = store.callers()[0]
    assert caller["source_label"] == "example-laptop"
    assert caller["source_label_source"] == "discovered_hostname"
    assert caller["source_hostname"] == "example-laptop"


def test_an_operator_label_still_outranks_a_resolved_name(tmp_path):
    store = Store(str(tmp_path))
    store.source_names = SourceNames(
        ttl=60,
        runner=fake_runner({("tailscale", "status", "--json"): TAILSCALE_STATUS}),
    )
    store.source_names.refresh(["192.0.2.50"])
    with store.lock:
        store.db.execute(
            "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
            (
                "x",
                time.time(),
                json.dumps(
                    {
                        "id": "x",
                        "seen": time.time(),
                        "source_address": "192.0.2.50",
                        "source_key": "addr:192.0.2.50",
                        "source_label": "192.0.2.50",
                        "source_label_source": "address",
                        "source_hostname": "",
                    }
                ),
            ),
        )
        store.db.commit()
    # The name is set only for a source the router has actually seen, so the
    # record has to exist first: that is the route's own contract.
    store.set_caller_source_name("addr:192.0.2.50", "Example laptop")
    caller = store.callers()[0]
    assert caller["source_label"] == "Example laptop"
    assert caller["source_label_source"] == "operator"


def test_a_caller_reported_hostname_is_not_overwritten_by_resolution(tmp_path):
    store = Store(str(tmp_path))
    store.source_names = SourceNames(
        ttl=60,
        runner=fake_runner({("tailscale", "status", "--json"): TAILSCALE_STATUS}),
    )
    store.source_names.refresh(["192.0.2.50"])
    with store.lock:
        store.db.execute(
            "INSERT OR REPLACE INTO callers VALUES (?, ?, ?)",
            (
                "x",
                time.time(),
                json.dumps(
                    {
                        "id": "x",
                        "seen": time.time(),
                        "source_address": "192.0.2.50",
                        "source_key": "addr:192.0.2.50",
                        "source_label": "reported-by-caller",
                        "source_label_source": "reported_hostname",
                        "source_hostname": "reported-by-caller",
                    }
                ),
            ),
        )
        store.db.commit()
    assert store.callers()[0]["source_label"] == "reported-by-caller"


def test_network_view_names_scanned_hosts_from_the_cache(tmp_path):
    publish(
        tmp_path,
        {
            "phase": Phase.COMPLETE,
            "completed_at": time.time(),
            "hosts": [
                {"address": "192.0.2.91", "name": "", "status": "up", "scope": "network", "services": []},
                {"address": "192.0.2.33", "name": "", "status": "up", "scope": "network", "services": []},
                {"address": "192.0.2.20", "name": "already-named", "status": "up", "scope": "network", "services": []},
            ],
        },
    )
    view = network_view(
        tmp_path,
        [],
        resolved_names=lambda: {"192.0.2.91": "spark-host.local"},
    )
    by_address = {h["address"]: h for h in view["hosts"]}
    assert by_address["192.0.2.91"]["name"] == "spark-host.local"
    assert by_address["192.0.2.91"]["name_source"] == "resolved"
    # Nothing known about this one: still an address, not an invented name.
    assert by_address["192.0.2.33"]["name"] == ""
    assert "name_source" not in by_address["192.0.2.33"]
    # A name the scan already carried is left alone.
    assert by_address["192.0.2.20"]["name"] == "already-named"


def test_network_view_without_a_resolver_leaves_names_untouched(tmp_path):
    publish(
        tmp_path,
        {
            "phase": Phase.COMPLETE,
            "completed_at": time.time(),
            "hosts": [{"address": "192.0.2.91", "name": "", "status": "up", "scope": "network", "services": []}],
        },
    )
    view = network_view(tmp_path, [])
    assert view["hosts"][0]["name"] == ""
