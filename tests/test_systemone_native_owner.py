"""Causal ownership against finite actual HTTP/SQLite; all inputs are inert fixtures."""
import asyncio
import json
import os
import sys
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException
from test_systemone_native import ANSWER, HEALTH, PAYLOAD, TIMING, configured, handler

import gateway.systemone_native as owner_source
from gateway.systemone_native import NativeFault, exited, starttoken


class Backend:
    def __init__(self):
        self.posts = 0
        self.submitted = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.tasks, self.writers = set(), set()
        self.status, self.reply, self.timing = 200, ANSWER, TIMING

    async def connection(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task); self.writers.add(writer)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
            assert len(head) <= 8192
            length = next((int(h.split(b":", 1)[1]) for h in head.split(b"\r\n")
                           if h.lower().startswith(b"content-length:")), 0)
            assert length <= 8192
            body = await asyncio.wait_for(reader.readexactly(length), 1)
            if head.startswith(b"POST "):
                self.posts += 1; self.submitted.set()
                assert json.loads(body)["model"] == HEALTH["model"]
                await asyncio.wait_for(self.release.wait(), 2)
                document, status = self.reply, self.status
            else:
                document, status = HEALTH, 200
            raw = json.dumps(document).encode()
            extra = f"Server-Timing: {self.timing}\r\n" if status == 200 else ""
            writer.write((f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(raw)}\r\n"
                          f"Content-Type: application/json\r\nConnection: close\r\n{extra}\r\n").encode() + raw)
            await writer.drain()
            if head.startswith(b"POST "): self.finished.set()
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close(); await writer.wait_closed()
            self.tasks.discard(task); self.writers.discard(writer)


@asynccontextmanager
async def backend():
    value = Backend()
    server = await asyncio.start_server(value.connection, "127.0.0.1", 0, limit=8193)
    value.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield value
    finally:
        server.close(); await server.wait_closed()
        for task in list(value.tasks): task.cancel()
        await asyncio.gather(*value.tasks, return_exceptions=True)
        for writer in list(value.writers): writer.close()


@asynccontextmanager
async def serving(tmp_path, fake, **kwargs):
    app, engine = configured(tmp_path, base_url=fake.url, **kwargs)
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            yield app, engine, client


async def terminal(owner):
    try:
        return await asyncio.wait_for(asyncio.shield(owner.task), 2)
    except HTTPException as exc:
        assert exc.status_code == 503
        return exc


async def test_actual_caller_cancel_retains_one_job_until_positive_late_finish(tmp_path):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, client):
        owner = app.state.native_owner
        request = asyncio.create_task(client.post("/v1/systemone", json=PAYLOAD))
        await asyncio.wait_for(fake.submitted.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError): await request
        assert not owner.task.done() and owner._sql("read")["phase"] == "submitted"
        assert app.state.discovery.observation(engine.id).inflight == 1
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert fake.posts == 1
        fake.release.set()
        assert (await terminal(owner)).status_code == 200
        assert owner._sql("read")["phase"] == "completed"
        assert app.state.discovery.observation(engine.id).inflight == 0
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 200
        assert fake.posts == 2


async def test_actual_asgi_disconnect_does_not_cancel_backend_owner(tmp_path):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, _):
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": json.dumps(PAYLOAD).encode(), "more_body": False})
        outgoing = []
        async def send(message): outgoing.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "path": "/v1/systemone", "raw_path": b"/v1/systemone", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")], "scheme": "http",
                 "server": ("router.test", 80), "client": ("127.0.0.1", 1234), "root_path": ""}
        caller = asyncio.create_task(app(scope, incoming.get, send))
        await asyncio.wait_for(fake.submitted.wait(), 1)
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(caller, 1)
        assert outgoing[0]["status"] == 499
        assert not app.state.native_owner.task.done()
        fake.release.set(); await terminal(app.state.native_owner)
        assert app.state.discovery.observation(engine.id).inflight == 0


async def test_timeout_reopen_and_false_exit_never_clear_uncertain_reservation(tmp_path):
    async with backend() as fake:
        async with serving(tmp_path, fake) as (app, engine, client):
            response = await client.post("/v1/systemone", json=PAYLOAD)
            assert response.status_code == 503
            row = app.state.native_owner._sql("read")
            assert row["phase"] == "unknown" and fake.posts == 1
            assert app.state.discovery.observation(engine.id).inflight == 1
            assert app.state.discovery.observation(engine.id).circuit_until == 0
            fake.release.set(); await asyncio.wait_for(fake.finished.wait(), 1)
        async with serving(tmp_path, fake, exit_probe=lambda _: False) as (reopened, engine2, client2):
            owner = reopened.state.native_owner
            assert owner._sql("read")["job_id"] == row["job_id"]
            assert engine2.id == row["engine_id"]
            assert reopened.state.discovery.observation(engine2.id).inflight == 1
            assert reopened.state.discovery.observation(row["engine_id"]).inflight == 1
            assert (await client2.post("/v1/systemone", json=PAYLOAD)).status_code == 503
            with pytest.raises(NativeFault, match="backend_exit_unproven"):
                await owner.reconcile(row["job_id"])
            owner.exit_probe = lambda _: True
            assert (await owner.reconcile(row["job_id"]))["phase"] == "recovered"
            assert (await client2.post("/v1/systemone", json=PAYLOAD)).status_code == 200
            assert fake.posts == 2


async def test_shutdown_retains_submitted_ownership_across_restart(tmp_path):
    async with backend() as fake:
        async with serving(tmp_path, fake) as (app, _, client):
            caller = asyncio.create_task(client.post("/v1/systemone", json=PAYLOAD))
            await asyncio.wait_for(fake.submitted.wait(), 1)
            await app.state.native_owner.close()
            with pytest.raises(asyncio.CancelledError): await caller
            row = app.state.native_owner._sql("read")
            assert row["phase"] == "unknown"
        async with serving(tmp_path, fake) as (new, _, client):
            assert new.state.native_owner._sql("read")["job_id"] == row["job_id"]
            assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
            assert fake.posts == 1


async def test_native_422_prequeue_rejection_is_exact_and_reusable(tmp_path):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, client):
        fake.release.set(); fake.status = 422
        fake.reply = {"detail": [{"loc": ["body"], "msg": "Fixture whole context exceeds limit; no truncation", "type": "value_error"}]}
        response = await client.post("/v1/systemone", json=PAYLOAD)
        assert response.status_code == 422 and response.json() == fake.reply
        assert app.state.native_owner._sql("read")["phase"] == "rejected"
        assert app.state.discovery.observation(engine.id).inflight == 0
        fake.status, fake.reply = 200, ANSWER
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 200


@pytest.mark.parametrize("fault", ["partial", "foreign", "missing_timing", "reply_limit", "status"])
async def test_nonpositive_response_quarantines_without_health_cooldown(tmp_path, fault):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, client):
        fake.release.set()
        if fault == "partial": fake.reply = {"model": "fixture-native"}
        elif fault == "foreign": fake.reply = {**ANSWER, "model": "foreign"}
        elif fault == "missing_timing": fake.timing = "parse;dur=1"
        elif fault == "reply_limit": fake.reply = {"private": "x" * 4097}
        else: fake.status = 500
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert app.state.native_owner._sql("read")["phase"] == "unknown"
        assert app.state.discovery.observation(engine.id).inflight == 1
        assert app.state.discovery.observation(engine.id).circuit_until == 0
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert fake.posts == 1


async def test_cancel_during_actual_sqlite_claim_is_pre_submit_rejected(tmp_path):
    app, engine = configured(tmp_path, handler)
    owner = app.state.native_owner
    entered, release = threading.Event(), threading.Event()
    original = owner._sql
    def delayed(action, *args):
        value = original(action, *args)
        if action == "claim": entered.set(); assert release.wait(1)
        return value
    owner._sql = delayed
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            caller = asyncio.create_task(client.post("/v1/systemone", json=PAYLOAD))
            assert await asyncio.to_thread(entered.wait, 1)
            caller.cancel(); release.set()
            with pytest.raises(asyncio.CancelledError): await caller
            assert owner.task is None and original("read")["phase"] == "rejected"
            assert app.state.discovery.observation(engine.id).inflight == 0
            assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 200


async def test_failed_terminal_commit_is_not_positive_capacity_release(tmp_path):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, client):
        fake.release.set(); owner = app.state.native_owner; original = owner._sql
        def fail_terminal(action, value=None, patch=None):
            if action == "update" and patch.get("phase") == "completed":
                raise NativeFault("ownership_store_busy")
            return original(action, value, patch)
        owner._sql = fail_terminal
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert owner._sql("read")["phase"] == "unknown"
        assert app.state.discovery.observation(engine.id).inflight == 1
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert fake.posts == 1


async def test_real_process_and_listener_exit_proof_is_positive_not_healthy(monkeypatch):
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(.3)",
                                                  stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    record = {"pid": child.pid, "starttoken": starttoken(child.pid), "unit": "fixture.service",
              "cgroup": f"/user.slice/user-{os.getuid()}.slice/nonexistent-native-fixture"}
    monkeypatch.setattr(owner_source, "unit_properties", lambda _: {"MainPID": "0", "ActiveState": "inactive"})
    async with backend() as fake:
        record["endpoint"] = fake.url
        assert not exited(record)  # Real process exists, regardless of mocked unit label.
        await asyncio.wait_for(child.wait(), 1)
        assert not exited(record)  # Actual listener still exists.
    assert exited(record)
    monkeypatch.setattr(owner_source, "unit_properties", lambda _: {"MainPID": "1", "ActiveState": "active"})
    assert not exited(record)


async def test_two_actual_router_owners_race_one_sqlite_singleton_no_hidden_queue(tmp_path):
    async with (
        backend() as fake,
        serving(tmp_path, fake) as (one, _, client1),
        serving(tmp_path, fake) as (two, _, client2),
    ):
        first = asyncio.create_task(client1.post("/v1/systemone", json=PAYLOAD))
        second = asyncio.create_task(client2.post("/v1/systemone", json=PAYLOAD))
        await asyncio.wait_for(fake.submitted.wait(), 1)
        finished, _ = await asyncio.wait({first, second}, timeout=.5, return_when=asyncio.FIRST_COMPLETED)
        assert len(finished) == 1 and finished.pop().result().status_code == 503
        assert fake.posts == 1
        fake.release.set()
        assert sorted(r.status_code for r in await asyncio.gather(first, second)) == [200, 503]
        row = one.state.native_owner._sql("read")
        assert row == two.state.native_owner._sql("read") and row["phase"] == "completed"

async def test_generation_change_after_response_remains_owned(tmp_path):
    calls = 0
    app, engine = configured(tmp_path, handler)
    owner = app.state.native_owner
    original = owner.probe
    def changed(value):
        nonlocal calls
        calls += 1
        result = original(value)
        if calls > 1: result["starttoken"] = "changed"
        return result
    owner.probe = changed
    async with app.router.lifespan_context(app):
        await app.state.discovery.refresh_engine(engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router.test") as client:
            response = await client.post("/v1/systemone", json=PAYLOAD)
            assert response.status_code == 503 and response.json()["detail"] == "native_generation_changed"
            assert owner._sql("read")["phase"] == "unknown"
            assert app.state.discovery.observation(engine.id).inflight == 1


async def test_actual_engine_specific_smaller_reply_limit_remains_uncertain(tmp_path):
    async with backend() as fake, serving(tmp_path, fake) as (app, engine, client):
        fake.release.set(); fake.reply = {"private": "x" * 1100}
        config = app.state.store.config(); config.engines[0].max_response_bytes = 1024
        app.state.store.save(config)
        assert (await client.post("/v1/systemone", json=PAYLOAD)).status_code == 503
        assert app.state.native_owner._sql("read")["code"] == "native_reply_limit"
        assert app.state.discovery.observation(engine.id).inflight == 1
