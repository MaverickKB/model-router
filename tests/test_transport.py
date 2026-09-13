"""Real sockets exercise cancellation, streaming, refresh and fallback end to end."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from anyio import create_task_group
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.app import create_app
from gateway.discovery import Observation
from gateway.proxy import InflightRequest
from gateway.schema import Client, Configuration, Discovery, Engine, Route, Selector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first", [b"first", b"data: [DONE]\n", b"data: [DONE]\ndata: more\n\n"]
)
async def test_stream_cancellation_finishes_history_and_closes_upstream(first):
    observation = Observation(engine_id="stream-test")
    connection = InflightRequest(observation, failure_cooldown=1)
    statuses = []
    closed = []
    reading = asyncio.Event()

    class Upstream:
        async def aclose(self):
            await asyncio.sleep(0)
            closed.append(True)

    connection.response = Upstream()

    async def chunks():
        reading.set()
        await asyncio.Future()
        yield b"unreachable"

    async def finish(status, code=None):
        await asyncio.sleep(0)
        statuses.append(status)

    async def consume():
        async for _ in connection.relay(first, chunks(), finish):
            pass

    async with create_task_group() as tasks:
        tasks.start_soon(consume)
        await reading.wait()
        tasks.cancel_scope.cancel()
    assert statuses == ["cancelled"]
    assert closed == [True]
    assert observation.inflight == 0


@asynccontextmanager
async def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", proxy_headers=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(8):
            while not server.started:
                await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)


@pytest.mark.asyncio
async def test_real_http_model_swap_cloud_fallback_stream_and_cancel(tmp_path):
    engine = FastAPI()
    state = {"model": "initial", "fail": False, "mode": "normal", "closed": 0}

    @engine.get("/v1/models")
    async def catalog():
        return {"data": [{"id": state["model"]}]}

    @engine.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        if state["fail"]:
            return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
        if body["model"] != state["model"]:
            return JSONResponse({"error": "model replaced"}, status_code=404)
        if not body.get("stream") and state["mode"] == "slow":
            while not await request.is_disconnected():
                await asyncio.sleep(0.02)
            return JSONResponse({"cancelled": True}, status_code=499)
        if not body.get("stream"):
            return {
                "model": state["model"],
                "choices": [{"message": {"content": "hello"}}],
            }
        mode = state["mode"]

        async def generate():
            try:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "model": state["model"],
                            "choices": [{"delta": {"content": "hello"}}],
                        }
                    )
                    + "\n\n"
                )
                if mode == "break":
                    raise RuntimeError("injected upstream disconnect")
                if mode == "slow":
                    await asyncio.sleep(30)
                yield "data: [DONE]\n\n"
            finally:
                state["closed"] += 1

        return StreamingResponse(generate(), media_type="text/event-stream")

    remote = FastAPI()
    remote_calls = []

    @remote.get("/v1/models")
    async def remote_models():
        return {"data": [{"id": "backup"}]}

    @remote.post("/v1/chat/completions")
    async def remote_completion(request: Request):
        remote_calls.append(await request.json())
        return {
            "model": "backup",
            "choices": [{"message": {"content": "remote hello"}}],
        }

    async with serving(engine) as local_url, serving(remote) as remote_url:
        app = create_app(str(tmp_path), background=True)
        local = Engine(name="HTTP engine", base_url=local_url + "/v1")
        cloud = Engine(
            name="HTTP cloud protocol fixture",
            base_url=remote_url + "/v1",
            kind="cloud",
            model_patterns=["*"],
        )
        client = Client(
            name="Socket client", allow_cloud=True, allow_direct_models=True
        )
        config = Configuration(
            engines=[local, cloud],
            clients=[client],
            routes=[
                Route(
                    name="auto", fallback=Selector(kind="cloud", model_patterns=["*"])
                )
            ],
            discovery=Discovery(enabled=False, mdns=False, refresh_seconds=2),
        )
        app.state.store.save(config)
        key = app.state.store.issue_key(client.id)

        # Avoid opening multicast during this controlled transport fixture.
        async def no_mdns():
            await asyncio.Future()

        app.state.discovery.mdns = no_mdns
        async with (
            serving(app) as url,
            httpx.AsyncClient(
                base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=8
            ) as http,
        ):
            await app.state.discovery.refresh()
            payload = {
                "model": "auto",
                "messages": [{"role": "user", "content": "test"}],
            }
            assert (await http.post("/v1/chat/completions", json=payload)).json()[
                "model"
            ] == "initial"
            state["model"] = "unseen-replacement"
            async with asyncio.timeout(6):
                while "unseen-replacement" not in [
                    x["id"] for x in (await http.get("/v1/models")).json()["data"]
                ]:
                    await asyncio.sleep(0.15)
            assert (await http.post("/v1/chat/completions", json=payload)).json()[
                "model"
            ] == "unseen-replacement"
            result = await http.post(
                "/v1/chat/completions", json={**payload, "stream": True}
            )
            assert (
                result.status_code == 200
                and "data: [DONE]" in result.text
                and "unseen-replacement" in result.text
            )
            state["mode"] = "slow"
            async with http.stream(
                "POST", "/v1/chat/completions", json={**payload, "stream": True}
            ) as response:
                async for chunk in response.aiter_bytes():
                    assert b"hello" in chunk
                    break
            async with asyncio.timeout(3):
                while app.state.discovery.observation(local.id).inflight:
                    await asyncio.sleep(0.05)
            assert app.state.store.events()[0]["status"] == "cancelled"
            async with httpx.AsyncClient(
                base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=0.25
            ) as impatient:
                with pytest.raises(httpx.ReadTimeout):
                    await impatient.post("/v1/chat/completions", json=payload)
            async with asyncio.timeout(3):
                while app.state.discovery.observation(local.id).inflight:
                    await asyncio.sleep(0.05)
            assert app.state.store.events()[0]["status"] == "cancelled"
            state["mode"] = "break"
            broken = await http.post(
                "/v1/chat/completions", json={**payload, "stream": True}
            )
            assert (
                "hello" in broken.text and "Upstream stream interrupted" in broken.text
            )
            assert not remote_calls, (
                "A partially delivered stream must not replay to a backup"
            )
            failed = app.state.store.events()[0]
            assert failed["status"] == "failed"
            assert failed["http_status"] == 502
            assert app.state.discovery.observation(local.id).inflight == 0
            app.state.discovery.observation(local.id).circuit_until = 0
            state["fail"] = True
            assert (await http.post("/v1/chat/completions", json=payload)).json()[
                "model"
            ] == "backup"
            assert len(remote_calls) == 1
            saved = app.state.store.config()
            saved.clients[0].allow_cloud = False
            app.state.store.save(saved)
            assert (
                await http.post("/v1/chat/completions", json=payload)
            ).status_code == 503
            assert len(remote_calls) == 1


async def test_client_close_at_done_records_completed_before_upstream_eof(tmp_path):
    upstream = FastAPI()
    upstream_closed = asyncio.Event()

    @upstream.get("/v1/models")
    async def catalog():
        return {"data": [{"id": "tool-model", "capabilities": {"tools": True}}]}

    @upstream.post("/v1/chat/completions")
    async def completion():
        async def chunks():
            try:
                yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"inspect","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
                yield b"data: [DO"
                await asyncio.sleep(0.01)
                yield b"NE]\r\n\r\n"
                # Some upstreams keep the HTTP connection open after their
                # protocol terminal event. The router owns closing this stream.
                await asyncio.Future()
            finally:
                upstream_closed.set()

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with serving(upstream) as upstream_url:
        app = create_app(str(tmp_path), background=False)
        engine = Engine(name="Tool service", base_url=upstream_url + "/v1")
        app.state.store.save(Configuration(engines=[engine]))
        await app.state.discovery.refresh()
        async with serving(app) as router_url, httpx.AsyncClient(timeout=5) as client:
            async with client.stream(
                "POST",
                router_url + "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "inspect"}],
                    "tools": [{"type": "function", "function": {"name": "inspect"}}],
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line == "data: [DONE]":
                        break
                else:
                    pytest.fail("No protocol completion marker was delivered")
            async with asyncio.timeout(3):
                while app.state.discovery.observation(engine.id).inflight:
                    await asyncio.sleep(0.01)
            event = app.state.store.events()[0]
            assert event["status"] == "completed"
            assert event["http_status"] == 200
            assert app.state.discovery.observation(engine.id).last_success is not None
            await asyncio.wait_for(upstream_closed.wait(), 3)
