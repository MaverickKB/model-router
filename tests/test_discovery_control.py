"""Discovery protocol fixtures never send traffic to a LAN."""

import asyncio
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from gateway.discovery import DiscoveryService
from gateway.network import scanner
from gateway.network.announcements import endpoint
from gateway.network.collector import Collector
from gateway.network.portable import addresses
from gateway.network.report import Phase, publish, read_report, request_scan, write_json
from gateway.network.sweep import Sweep, catalog_refresh_targets, merge_mdns_evidence
from gateway.schema import Configuration, Discovery
from gateway.store import Store


@pytest.mark.parametrize(
    "fixture,target,privileged,ports,expected",
    [
        ("ipv4.xml", "192.0.2.0/24", True, "1-65535", "-sS"),
        ("ipv6.xml", "2001:db8::17", False, "8000-8100", "-6"),
        ("ipv4.xml", "192.0.2.17", False, "top1000", "--top-ports"),
        ("ipv4.xml", "192.0.2.17", False, None, "-sn"),
    ],
)
async def test_nmap_xml_and_exact_scan_arguments(
    monkeypatch, fixture, target, privileged, ports, expected
):
    calls = []
    content = (Path(__file__).parent / "fixtures/nmap" / fixture).read_bytes()
    install_process(monkeypatch, content, calls)
    rows = [
        row
        async for row in scanner.scan_nmap([target], ports=ports, privileged=privileged)
    ]
    assert expected in calls[0] and target in calls[0]
    hosts = [row["host"] for row in rows if "host" in row]
    assert len(hosts) == 1
    if fixture == "ipv4.xml":
        assert hosts[0]["ports"] == [51387]
        assert hosts[0]["filtered_ports"] == 99
        assert hosts[0]["hardware_address"] == "00:11:22:33:44:55"
    else:
        assert hosts[0]["address"] == "2001:db8::17"
        assert endpoint(hosts[0]["address"], 65535) == "http://[2001:db8::17]:65535"


def install_process(monkeypatch, content, calls):
    class Process:
        def __init__(self):
            self.returncode = None
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            for offset in range(0, len(content), 13):
                self.stdout.feed_data(content[offset : offset + 13])
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    async def start(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(scanner.shutil, "which", lambda value: "/fixture/nmap")
    monkeypatch.setattr(scanner, "is_local_address", lambda value: False)
    monkeypatch.setattr(scanner.asyncio, "create_subprocess_exec", start)


@pytest.mark.parametrize(
    "content,error",
    [
        (b"<nmaprun><host>", ET.ParseError),
        (
            (Path(__file__).parent / "fixtures/nmap/failed.xml").read_bytes(),
            RuntimeError,
        ),
    ],
)
async def test_incomplete_and_failed_xml_are_errors(monkeypatch, content, error):
    install_process(monkeypatch, content, [])
    with pytest.raises(error):
        _ = [row async for row in scanner.scan_nmap(["192.0.2.1"], ports="8000")]


async def test_defaults_inventory_without_http_payloads_and_explicit_inspection(
    tmp_path,
):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/v1/chat/completions": {
                            "post": {
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "model": {"type": "string"},
                                                    "messages": {"type": "array"},
                                                },
                                            }
                                        }
                                    }
                                },
                                "responses": {
                                    "200": {
                                        "description": "completion",
                                        "content": {
                                            "application/json": {
                                                "schema": {
                                                    "type": "object",
                                                    "properties": {
                                                        "choices": {"type": "array"}
                                                    },
                                                }
                                            }
                                        },
                                    }
                                },
                            }
                        }
                    }
                },
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "observed"}]})
        return httpx.Response(404)

    sweep = Sweep(tmp_path)
    host = {"address": "192.0.2.1", "ports": [49187], "services": []}
    sweep.report["hosts"] = [host]
    sweep.policy = Discovery().model_dump()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await sweep.probe(http, host, 49187)
        assert not calls
        assert host["services"][0]["status"] == "inspection_required"
        sweep.policy["http_ports"] = [49187]
        await sweep.probe(http, host, 49187)
        assert calls
        assert {service["status"] for service in host["services"]} == {
            "model_service"
        }
    assert (
        not Discovery().enabled
        and not Discovery().auto_register
        and not Discovery().mdns
    )
    with pytest.raises(ValueError, match="budget"):
        await addresses(["192.0.2.0/24"], 8)
    assert await addresses(["2001:db8::1/128"], 1) == ["2001:db8::1"]


async def test_sweep_keeps_https_catalog_and_each_same_port_api_surface(
    tmp_path, monkeypatch
):
    """A plaintext response cannot mask HTTPS or another API base."""
    inspected = []

    async def classify(http, origin):
        inspected.append(origin)
        if origin.startswith("http://"):
            return [
                {
                    "origin": origin,
                    "surface_id": origin + "#http",
                    "status": "http_service",
                    "protocol": "http",
                    "base_url": "",
                    "compatible_base_url": "",
                    "models": [],
                    "capabilities": [],
                    "detail": "Plain HTTP response",
                }
            ]
        return [
            {
                "origin": origin,
                "surface_id": origin + "/api",
                "status": "model_service",
                "protocol": "openai",
                "base_url": origin + "/api",
                "compatible_base_url": origin + "/api",
                "models": [{"id": "test-model"}],
                "capabilities": ["text"],
                "completion_paths": ["/chat/completions"],
                "registration_eligible": True,
                "detail": "",
            },
            {
                "origin": origin,
                "surface_id": origin + "/preview",
                "status": "model_surface",
                "protocol": "openai",
                "base_url": "",
                "compatible_base_url": origin + "/preview",
                "models": [],
                "capabilities": ["text"],
                "registration_eligible": False,
                "detail": "Completion surface without a catalog",
            },
        ]

    monkeypatch.setattr("gateway.network.transports.inspect_services", classify)
    sweep = Sweep(tmp_path)
    host = {"address": "192.0.2.44", "ports": [47123], "services": []}
    sweep.report["hosts"] = [host]
    sweep.policy = Discovery(http_ports=[47123]).model_dump()
    async with httpx.AsyncClient() as http:
        await sweep.probe(http, host, 47123)

    assert inspected == [
        "http://192.0.2.44:47123",
        "https://192.0.2.44:47123",
    ]
    services = {service["surface_id"]: service for service in host["services"]}
    assert set(services) == {
        "http://192.0.2.44:47123#http",
        "https://192.0.2.44:47123/api",
        "https://192.0.2.44:47123/preview",
    }
    assert services["https://192.0.2.44:47123/api"]["registration_eligible"] is True
    assert services["https://192.0.2.44:47123/preview"]["status"] == "model_surface"


async def test_sweep_refreshes_a_previously_classified_unusual_port(
    tmp_path, monkeypatch
):
    """Observed evidence remains refreshable outside the broad port policy."""
    inspected = []

    async def classify(http, origin):
        inspected.append(origin)
        return [
            {
                "origin": origin,
                "surface_id": origin,
                "status": "model_surface",
                "protocol": "openai",
                "base_url": "",
                "compatible_base_url": origin,
                "models": [],
                "capabilities": ["text"],
                "registration_eligible": False,
                "detail": "Completion operation found without a readable catalog",
            }
        ]

    monkeypatch.setattr("gateway.network.transports.inspect_services", classify)
    sweep = Sweep(tmp_path)
    host = {
        "address": "192.0.2.47",
        "ports": [47126],
        "services": [
            {
                "origin": "http://192.0.2.47:47126",
                "port": 47126,
                "status": "model_surface",
                "catalog_tracked": True,
            }
        ],
    }
    sweep.report["hosts"] = [host]
    sweep.policy = Discovery().model_dump()
    async with httpx.AsyncClient() as http:
        await sweep.probe(http, host, 47126)

    assert inspected == [
        "http://192.0.2.47:47126",
        "https://192.0.2.47:47126",
    ]
    assert len(host["services"]) == 2
    assert {service["status"] for service in host["services"]} == {"model_surface"}
    assert all(service["catalog_tracked"] is True for service in host["services"])


def test_catalog_refresh_targets_probe_a_multi_surface_port_once():
    host = {
        "address": "192.0.2.48",
        "services": [
            {"port": 47127, "surface_id": "http", "catalog_tracked": True},
            {"port": 47127, "surface_id": "https-api", "catalog_tracked": True},
            {"port": 47127, "surface_id": "https-preview", "catalog_tracked": True},
            {"port": 47128, "surface_id": "other", "catalog_tracked": True},
        ],
    }

    assert [(row["address"], port) for row, port in catalog_refresh_targets({"hosts": [host]})] == [
        ("192.0.2.48", 47127),
        ("192.0.2.48", 47128),
    ]


async def test_mdns_retains_a_catalogless_completion_surface_for_review(
    tmp_path, monkeypatch
):
    """mDNS evidence remains visible even when it cannot be auto-registered."""
    store = Store(str(tmp_path))
    store.save(
        Configuration(
            discovery=Discovery(
                enabled=True,
                mdns=True,
                targets=["192.0.2.0/24"],
                auto_register=True,
            )
        )
    )
    candidate = "http://192.0.2.45:47124"

    inspected = []

    async def classify(http, origin):
        inspected.append(origin)
        return [
            {
                "origin": origin,
                "surface_id": origin,
                "status": "model_surface",
                "protocol": "openai",
                "base_url": "",
                "compatible_base_url": origin,
                "models": [],
                "capabilities": ["text"],
                "registration_eligible": False,
                "needs_model_identity": True,
                "detail": "Completion operation found without a readable catalog",
            }
        ]

    async def trusted(hostname):
        return hostname == "192.0.2.45"

    async def must_not_register(*args, **kwargs):
        pytest.fail("A catalogless mDNS completion surface must not auto-register")

    monkeypatch.setattr("gateway.network.transports.inspect_services", classify)
    async with httpx.AsyncClient() as http:
        discovery = DiscoveryService(store, http)
        discovery.mdns_candidates.add(candidate)
        monkeypatch.setattr(discovery, "trusted_host", trusted)
        monkeypatch.setattr(discovery, "discover_url", must_not_register)
        await discovery.scan()

    report = read_report(store.discovery_directory)
    host = report["hosts"][0]
    assert host["address"] == "192.0.2.45"
    assert host["scope"] == "mdns"
    assert inspected == [candidate, candidate.replace("http://", "https://", 1)]
    service = next(
        service
        for service in host["services"]
        if service["origin"] == candidate and service["status"] == "model_surface"
    )
    assert service.pop("checked_at") > 0
    assert service == {
        "origin": candidate,
        "surface_id": candidate,
        "status": "model_surface",
        "protocol": "openai",
        "base_url": "",
        "compatible_base_url": candidate,
        "models": [],
        "capabilities": ["text"],
        "registration_eligible": False,
        "needs_model_identity": True,
        "detail": "Completion operation found without a readable catalog",
        "port": 47124,
        "discovery_source": "mdns",
        "catalog_tracked": True,
    }
    assert {
        service["origin"] for service in host["services"]
    } == {candidate, candidate.replace("http://", "https://", 1)}
    assert not store.config().engines


async def test_mdns_uses_the_classified_base_for_a_verified_catalog(
    tmp_path, monkeypatch
):
    """mDNS never manufactures `/v1`; only inspect_services supplies a base."""
    store = Store(str(tmp_path))
    store.save(
        Configuration(
            discovery=Discovery(
                enabled=True,
                mdns=True,
                targets=["192.0.2.0/24"],
                auto_register=True,
            )
        )
    )
    candidate = "http://192.0.2.46:47125"
    secure_origin = "https://192.0.2.46:47125"
    classified_base = secure_origin + "/api"
    registrations = []
    inspected = []

    async def classify(http, origin):
        inspected.append(origin)
        if origin == candidate:
            return [
                {
                    "origin": origin,
                    "surface_id": origin + "#http",
                    "status": "http_service",
                    "protocol": "http",
                    "base_url": "",
                    "compatible_base_url": "",
                    "models": [],
                    "capabilities": [],
                    "registration_eligible": False,
                    "detail": "Plain HTTP response",
                }
            ]
        assert origin == secure_origin
        return [
            {
                "origin": origin,
                "surface_id": classified_base,
                "status": "model_service",
                "protocol": "openai",
                "base_url": classified_base,
                "compatible_base_url": classified_base,
                "models": [{"id": "test-model"}],
                "capabilities": ["text"],
                "completion_paths": ["/chat/completions"],
                "registration_eligible": True,
                "detail": "",
            }
        ]

    async def trusted(hostname):
        return hostname == "192.0.2.46"

    async def register(url, source, capabilities, **kwargs):
        registrations.append((url, source, capabilities, kwargs))
        report = read_report(store.discovery_directory)
        assert any(
            service["base_url"] == classified_base
            for service in report["hosts"][0]["services"]
        )
        return "registered-engine"

    monkeypatch.setattr("gateway.network.transports.inspect_services", classify)
    async with httpx.AsyncClient() as http:
        discovery = DiscoveryService(store, http)
        discovery.mdns_candidates.add(candidate)
        monkeypatch.setattr(discovery, "trusted_host", trusted)
        monkeypatch.setattr(discovery, "discover_url", register)
        await discovery.scan()

    assert registrations == [
        (
            classified_base,
            "mDNS",
            ["text"],
            {
                "completion_paths": ["/chat/completions"],
                "name": None,
                "catalog_protocol": "openai",
            },
        )
    ]
    assert inspected == [candidate, secure_origin]


def test_merge_mdns_evidence_keeps_each_api_base_without_duplicate_rows():
    """A shared TCP port can carry multiple independent API surfaces."""
    origin = "https://192.0.2.49:47128"
    shared_surface = origin + "/v1"
    merged = merge_mdns_evidence(
        {
            "hosts": [
                {
                    "address": "192.0.2.49",
                    "ports": [47128],
                    "services": [
                        {
                            "port": 47128,
                            "surface_id": shared_surface,
                            "checked_at": 20,
                            "detail": "TCP sweep classified this API base",
                        }
                    ],
                }
            ]
        },
        {
            "hosts": [
                {
                    "address": "192.0.2.49",
                    "ports": [47128],
                    "services": [
                        {
                            "port": 47128,
                            "surface_id": shared_surface,
                            "checked_at": 10,
                            "discovery_source": "mdns",
                            "catalog_tracked": True,
                        },
                        {
                            "port": 47128,
                            "surface_id": origin + "/preview",
                            "checked_at": 10,
                            "discovery_source": "mdns",
                            "catalog_tracked": True,
                        },
                    ],
                }
            ]
        },
    )

    services = {
        service["surface_id"]: service for service in merged["hosts"][0]["services"]
    }
    assert set(services) == {shared_surface, origin + "/preview"}
    assert services[shared_surface]["detail"] == "TCP sweep classified this API base"
    assert services[shared_surface]["discovery_source"] == "mdns"
    assert services[shared_surface]["catalog_tracked"] is True


async def test_sweep_save_preserves_mdns_services_added_after_the_sweep_started(
    tmp_path,
):
    """A stale sweep snapshot cannot replace newer mDNS evidence on disk."""
    store = Store(str(tmp_path))
    address, port = "192.0.2.50", 47129
    origin = f"https://{address}:{port}"
    sweep = Sweep(store.discovery_directory)
    sweep.report = {
        "phase": Phase.RUNNING,
        "hosts": [
            {
                "address": address,
                "scope": "network",
                "ports": [port],
                "scan_complete": True,
                "services": [
                    {
                        "port": port,
                        "surface_id": origin + "/api",
                        "checked_at": 20,
                        "status": "model_surface",
                        "catalog_tracked": True,
                    }
                ],
            }
        ],
    }
    async with httpx.AsyncClient() as http:
        discovery = DiscoveryService(store, http)
        await discovery.record_mdns_services(
            origin,
            [
                {
                    "origin": origin,
                    "surface_id": origin + "/v1",
                    "status": "model_service",
                    "models": [{"id": "announced-model"}],
                    "capabilities": ["text"],
                },
                {
                    "origin": origin,
                    "surface_id": origin + "/preview",
                    "status": "model_surface",
                    "models": [],
                    "capabilities": ["text"],
                },
            ],
        )
    await sweep.save(force=True)

    report = read_report(store.discovery_directory)
    host = next(host for host in report["hosts"] if host["address"] == address)
    services = {service["surface_id"]: service for service in host["services"]}
    assert host["scope"] == "network"
    assert host["scan_complete"] is True
    assert set(services) == {origin + "/api", origin + "/v1", origin + "/preview"}
    assert services[origin + "/v1"]["discovery_source"] == "mdns"
    assert services[origin + "/preview"]["discovery_source"] == "mdns"
    # The live sweep retains the announcement too, so its next refresh cannot
    # erase the evidence it just protected on disk.
    in_memory = {
        service["surface_id"] for service in sweep.report["hosts"][0]["services"]
    }
    assert in_memory == {origin + "/api", origin + "/v1", origin + "/preview"}


async def test_explicit_scan_job_is_acknowledged_and_cancellable(tmp_path):
    policy = Discovery(targets=["192.0.2.1"]).model_dump()
    write_json(tmp_path / "policy.json", policy)
    job = request_scan(tmp_path)
    assert job["accepted"]
    assert request_scan(tmp_path) == {**job, "accepted": False}
    entered, released = asyncio.Event(), asyncio.Event()

    class Controlled(Collector):
        async def collect(self, settings):
            self.report.update(phase=Phase.RUNNING, started_at=time.time())
            await self.save(True)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                released.set()

    collector = Controlled(tmp_path)
    task = asyncio.create_task(collector.run(once=True))
    await asyncio.wait_for(entered.wait(), 2)
    assert request_scan(tmp_path)["status"] == Phase.RUNNING
    write_json(tmp_path / "cancel.json", {"id": job["id"]})
    await asyncio.wait_for(task, 3)
    assert released.is_set()
    report = read_report(tmp_path)
    assert report["job_id"] == job["id"] and report["phase"] == Phase.INTERRUPTED
    assert request_scan(tmp_path)["id"] != job["id"]


def test_missing_collector_heartbeat_is_visible(tmp_path):
    publish(
        tmp_path, {"phase": Phase.RUNNING, "updated_at": time.time() - 31, "hosts": []}
    )
    assert read_report(tmp_path)["phase"] == Phase.FAILED
