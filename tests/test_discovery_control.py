"""Discovery protocol fixtures never send traffic to a LAN."""

import asyncio
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from gateway.network import scanner
from gateway.network.announcements import endpoint
from gateway.network.collector import Collector
from gateway.network.portable import addresses
from gateway.network.report import Phase, publish, read_report, request_scan, write_json
from gateway.network.sweep import Sweep
from gateway.schema import Discovery


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
        assert endpoint(hosts[0]["address"], 65535) == "http://[2001:db8::17]:65535/v1"


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
        return httpx.Response(200, json={"data": [{"id": "observed"}]})

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
        assert calls and host["services"][0]["status"] == "model_service"
    assert (
        not Discovery().enabled
        and not Discovery().auto_register
        and not Discovery().mdns
    )
    with pytest.raises(ValueError, match="budget"):
        await addresses(["192.0.2.0/24"], 8)
    assert await addresses(["2001:db8::1/128"], 1) == ["2001:db8::1"]


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
