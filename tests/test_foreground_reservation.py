"""Foreground reservation on the existing admission owner and actual proxy path."""

import asyncio
import json

import httpx
import pytest
from anyio import create_task_group
from pydantic import ValidationError

from gateway.app import create_app
from gateway.discovery import Observation
from gateway.proxy import InflightRequest
from gateway.schema import Client, Configuration, Engine, Route


@pytest.mark.parametrize("reserve,background_limit", [(0, 8), (1, 7), (2, 6), (8, 0)])
async def test_background_quota_and_total_capacity(reserve, background_limit):
    obs = Observation(engine_id="quota")
    claims = [
        InflightRequest.claim(
            obs, 8, 15, background=True, foreground_reserved_slots=reserve
        )
        for _ in range(background_limit)
    ]
    assert all(claims)
    assert (obs.inflight, obs.background_inflight) == (
        background_limit,
        background_limit,
    )
    assert (
        InflightRequest.claim(
            obs, 8, 15, background=True, foreground_reserved_slots=reserve
        )
        is None
    )
    foreground = [
        InflightRequest.claim(obs, 8, 15, foreground_reserved_slots=reserve)
        for _ in range(reserve)
    ]
    assert all(foreground)
    assert obs.inflight == 8
    assert InflightRequest.claim(obs, 8, 15, foreground_reserved_slots=reserve) is None
    await asyncio.gather(*(claim.release() for claim in claims + foreground))
    assert (obs.inflight, obs.background_inflight) == (0, 0)


async def test_old_positional_api_default_zero_and_mixed_classes():
    obs = Observation(engine_id="compatibility")
    old = [InflightRequest.claim(obs, 8, 15) for _ in range(8)]
    assert all(old) and obs.background_inflight == 0
    assert InflightRequest.claim(obs, 8, 15) is None
    await asyncio.gather(*(claim.release() for claim in old))
    foreground = [InflightRequest.claim(obs, 8, 15) for _ in range(3)]
    background = [
        InflightRequest.claim(obs, 8, 15, background=True, foreground_reserved_slots=2)
        for _ in range(5)
    ]
    assert all(foreground + background)
    assert (obs.inflight, obs.background_inflight) == (8, 5)
    assert (
        InflightRequest.claim(obs, 8, 15, background=True, foreground_reserved_slots=2)
        is None
    )
    await asyncio.gather(*(claim.release() for claim in foreground + background))
    assert (obs.inflight, obs.background_inflight) == (0, 0)


async def test_event_loop_contention_is_atomic_and_identity_lock_blocks_both_classes():
    obs = Observation(engine_id="contended")

    async def compete():
        await asyncio.sleep(0)
        return InflightRequest.claim(
            obs, 8, 15, background=True, foreground_reserved_slots=2
        )

    claims = [
        claim
        for claim in await asyncio.gather(*(compete() for _ in range(64)))
        if claim
    ]
    assert len(claims) == 6
    assert (obs.inflight, obs.background_inflight) == (6, 6)
    obs.identity_locked = True
    assert InflightRequest.claim(obs, 8, 15) is None
    assert InflightRequest.claim(obs, 8, 15, background=True) is None
    obs.identity_locked = False
    await asyncio.gather(*(claim.release() for claim in claims))
    assert (obs.inflight, obs.background_inflight) == (0, 0)


async def test_class_is_frozen_and_release_is_shielded_and_idempotent():
    obs = Observation(engine_id="release")
    claim = InflightRequest.claim(
        obs, 8, 15, background=True, foreground_reserved_slots=1
    )
    with pytest.raises(AttributeError):
        claim.background = False
    closing, allow_close = asyncio.Event(), asyncio.Event()
    closed = []

    class Upstream:
        async def aclose(self):
            closing.set()
            await allow_close.wait()
            closed.append(True)

    claim.response = Upstream()
    async with create_task_group() as tasks:
        tasks.start_soon(claim.release)
        await closing.wait()
        tasks.start_soon(claim.release)
        tasks.cancel_scope.cancel()
        allow_close.set()
    await claim.release()
    assert closed == [True]
    assert (obs.inflight, obs.background_inflight) == (0, 0)


async def test_close_error_still_releases_both_counts_once():
    obs = Observation(engine_id="error")
    claim = InflightRequest(obs, 15, background=True)

    class Upstream:
        async def aclose(self):
            raise RuntimeError("close fixture")

    claim.response = Upstream()
    with pytest.raises(RuntimeError, match="close fixture"):
        await claim.release()
    await claim.release()
    assert (obs.inflight, obs.background_inflight) == (0, 0)


@pytest.mark.parametrize(
    "mode", ["completed", "cancelled", "transport_error", "finish_error"]
)
async def test_background_stream_cleanup_all_outcomes(mode):
    obs = Observation(engine_id="stream")
    claim = InflightRequest.claim(
        obs, 8, 15, background=True, foreground_reserved_slots=1
    )
    statuses, closed = [], []
    reading = asyncio.Event()

    class Upstream:
        async def aclose(self):
            await asyncio.sleep(0)
            closed.append(True)

    claim.response = Upstream()

    async def chunks():
        reading.set()
        if mode == "cancelled":
            await asyncio.Future()
        elif mode == "transport_error":
            raise httpx.ReadError("transport fixture")
        yield b"data: [DONE]\n\n"

    async def finish(status, code):
        statuses.append((status, code))
        if mode == "finish_error":
            raise RuntimeError("finish fixture")

    async def consume():
        async for _ in claim.relay(b"data: {}\n\n", chunks(), finish):
            pass

    if mode == "cancelled":
        async with create_task_group() as tasks:
            tasks.start_soon(consume)
            await reading.wait()
            tasks.cancel_scope.cancel()
    elif mode == "finish_error":
        with pytest.raises(RuntimeError, match="finish fixture"):
            await consume()
    else:
        await consume()
    assert (obs.inflight, obs.background_inflight) == (0, 0)
    assert closed == [True]
    assert (
        statuses[0][0]
        == {
            "completed": "completed",
            "cancelled": "cancelled",
            "transport_error": "failed",
            "finish_error": "completed",
        }[mode]
    )
    assert bool(obs.circuit_until) == (mode == "transport_error")


@pytest.mark.parametrize("limit,reserve", [(1, 0), (1, 1), (8, 1), (8, 2), (8, 8)])
def test_engine_reservation_validates_and_round_trips(limit, reserve):
    engine = Engine(
        name="quota",
        base_url="http://engine.test/v1",
        max_inflight=limit,
        foreground_reserved_slots=reserve,
    )
    assert (
        Engine.model_validate(engine.model_dump()).foreground_reserved_slots == reserve
    )
    assert (
        Engine(
            name="legacy", base_url="http://engine.test/v1"
        ).foreground_reserved_slots
        == 0
    )


@pytest.mark.parametrize("reserve", [-1, 9, 10001])
def test_engine_rejects_invalid_reservation(reserve):
    with pytest.raises(ValidationError):
        Engine(
            name="quota",
            base_url="http://engine.test/v1",
            max_inflight=8,
            foreground_reserved_slots=reserve,
        )


def configured_app(tmp_path, handler, reserve=1):
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    engine = Engine(
        name="quota",
        base_url="http://engine.test/v1",
        max_inflight=8,
        foreground_reserved_slots=reserve,
    )
    caller = Client(name="fixture", route_names=["auto"])
    app.state.store.save(
        Configuration(engines=[engine], clients=[caller], routes=[Route(name="auto")])
    )
    key = app.state.store.issue_key(caller.id)
    return app, engine, {"Authorization": "Bearer " + key}


@pytest.mark.parametrize("reserve", [0, 1, 2])
async def test_actual_asgi_background_refusal_reserves_foreground_without_cooldown(
    tmp_path, reserve
):
    release = asyncio.Event()
    requests = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        requests.append(json.loads(request.content))
        await release.wait()
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "choices": [{"message": {"content": "fixture reply"}}],
            },
        )

    app, engine, headers = configured_app(tmp_path, handler, reserve)
    obs = app.state.discovery.observation(engine.id)
    payload = {"model": "auto", "messages": [{"role": "user", "content": "fixture"}]}
    tasks = []

    async def occupancy(total):
        async with asyncio.timeout(4):
            while obs.inflight != total:
                await asyncio.sleep(0.01)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers=headers,
        ) as client,
    ):
        await app.state.discovery.refresh()
        try:
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/v1/chat/completions",
                        json={**payload, "priority": "background"},
                    )
                )
                for _ in range(8 - reserve)
            ]
            await occupancy(8 - reserve)
            denial = await client.post(
                "/v1/chat/completions", json={**payload, "priority": "background"}
            )
            assert denial.status_code == 503
            if reserve == 0:
                assert denial.json()["error"].get("code") != "background_quota_exceeded"
                assert obs.circuit_until == 0 and obs.error == ""
                release.set()
                results = await asyncio.gather(*tasks)
                assert all(result.status_code == 200 for result in results)
                assert len(requests) == 8
                assert (obs.inflight, obs.background_inflight) == (0, 0)
                return
            assert denial.json()["error"]["code"] == "background_quota_exceeded"
            event = next(
                e
                for e in app.state.store.events()
                if e["id"] == denial.json()["request_id"]
            )
            assert event["status"] == "unavailable" and event["attempts"] == []
            assert event["request_class"] == "background"
            rejection = event["admission_rejections"][0]
            assert rejection["code"] == "background_quota_exceeded"
            assert rejection["background_inflight"] == 8 - reserve
            assert rejection["foreground_reserved_slots"] == reserve
            assert (
                obs.circuit_until == 0 and obs.error == "" and obs.status == "available"
            )
            assert len(requests) <= 8 - reserve
            assert app.state.discovery.views()[0]["background_inflight"] == 8 - reserve
            tasks.extend(
                asyncio.create_task(client.post("/v1/chat/completions", json=payload))
                for _ in range(reserve)
            )
            await occupancy(8)
            full = await client.post("/v1/chat/completions", json=payload)
            assert full.status_code == 503
            assert full.json()["error"].get("code") != "background_quota_exceeded"
            assert obs.circuit_until == 0 and obs.error == ""
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(result.status_code == 200 for result in results)
            assert (obs.inflight, obs.background_inflight) == (0, 0)
            assert len(requests) == 8
            assert (
                sum(body.get("priority") == "background" for body in requests)
                == 8 - reserve
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_actual_asgi_transport_failure_releases_background(tmp_path):
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        raise httpx.ConnectError("transport fixture")

    app, engine, headers = configured_app(tmp_path, handler)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers=headers,
        ) as client,
    ):
        await app.state.discovery.refresh()
        result = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [], "priority": "background"},
        )
        assert result.status_code == 503
    obs = app.state.discovery.observation(engine.id)
    assert (obs.inflight, obs.background_inflight) == (0, 0)
    assert obs.circuit_until > 0


async def test_actual_asgi_background_disconnect_releases_without_model_failure(
    tmp_path,
):
    entered = asyncio.Event()

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        entered.set()
        await asyncio.Future()

    app, engine, headers = configured_app(tmp_path, handler)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers=headers,
        ) as client,
    ):
        await app.state.discovery.refresh()
        task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [], "priority": "background"},
            )
        )
        await asyncio.wait_for(entered.wait(), 4)
        obs = app.state.discovery.observation(engine.id)
        assert (obs.inflight, obs.background_inflight) == (1, 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert (obs.inflight, obs.background_inflight) == (0, 0)
        assert obs.circuit_until == 0 and obs.error == ""
        assert app.state.store.events()[0]["status"] == "cancelled"


async def test_actual_asgi_pre_send_persistence_failure_releases_background(
    tmp_path, monkeypatch
):
    sent = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        sent.append(True)
        return httpx.Response(200, json={})

    app, engine, headers = configured_app(tmp_path, handler)
    persist = app.state.store.event
    failed = []

    def fail_once(event):
        if event["status"] == "waiting" and not failed:
            failed.append(True)
            raise RuntimeError("write fixture")
        persist(event)

    monkeypatch.setattr(app.state.store, "event", fail_once)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://router.test",
            headers=headers,
        ) as client,
    ):
        await app.state.discovery.refresh()
        with pytest.raises(RuntimeError, match="write fixture"):
            await client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [], "priority": "background"},
            )
    obs = app.state.discovery.observation(engine.id)
    assert failed == [True] and sent == []
    assert (obs.inflight, obs.background_inflight) == (0, 0)
    assert obs.circuit_until == 0 and obs.error == ""
