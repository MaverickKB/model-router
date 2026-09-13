"""Account token budgets and concurrency are enforced before any upstream send."""

import asyncio
import json
import math
import socket
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

import gateway.proxy
from gateway.accounts.limits import (
    DEFAULT_COMPLETION_RESERVE,
    REPORTING_WINDOW_SECONDS,
    UsageMeter,
    estimate_prompt_tokens,
    window_start,
)
from gateway.app import create_app
from gateway.schema import (
    AccountLevel,
    AccountsSettings,
    Client,
    Configuration,
    Engine,
    Route,
    Security,
    Selector,
    TokenBudget,
)
from gateway.security.credentials import digest

PROMPT = "PROMPT-MARKER-7f3a please answer"
REPLY = "COMPLETION-MARKER-9c1d fine"
USAGE = {"prompt_tokens": 12, "completion_tokens": 30}
BUDGET = TokenBudget(max_tokens=3000, window_seconds=3600)


async def sse(documents):
    # An async iterator keeps the mock response unread, so the proxy streams it
    # chunk by chunk exactly as it would from a socket.
    for document in documents:
        yield b"data: " + json.dumps(document).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


def payload(model="private", **extra) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": PROMPT}], **extra}


async def complete(http, model="private", **extra):
    return await http.post("/v1/chat/completions", json=payload(model, **extra))


class Fleet:
    """Two engines that report usage and can block, fail or oversize on demand."""

    def __init__(self):
        self.models = {"local-a.test": ["local-model"], "local-b.test": ["local-model"]}
        self.calls: list[tuple[str, dict]] = []
        self.statuses: list[int] = []
        self.errors: list[Exception] = []
        self.usage: dict | None = dict(USAGE)
        self.stream_documents: list[dict] | None = None
        self.oversize = False
        self.gate: asyncio.Event | None = None
        self.inflight = 0
        self.on_call = None

    async def wait_inflight(self, count: int):
        async with asyncio.timeout(5):
            while self.inflight < count:
                await asyncio.sleep(0.01)

    async def handle(self, request):
        host = request.url.host
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": m} for m in self.models.get(host, [])]}
            )
        body = json.loads(request.content)
        self.calls.append((host, body))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if self.errors:
            raise self.errors.pop(0)
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "unavailable"}})
        self.inflight += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.inflight -= 1
        if self.oversize:
            return httpx.Response(200, content=b"x" * 4096)
        if body.get("stream"):
            documents = self.stream_documents or [
                {"choices": [{"delta": {"content": word}}]} for word in REPLY.split()
            ]
            if self.usage and (body.get("stream_options") or {}).get("include_usage"):
                documents = [*documents, {"choices": [], "usage": self.usage}]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(documents),
            )
        document = {
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": REPLY}}],
        }
        if self.usage:
            document["usage"] = self.usage
        return httpx.Response(200, json=document)


class Setup:
    def __init__(self, app, fleet, engines, level, account, key):
        self.app, self.fleet, self.engines = app, fleet, engines
        self.level, self.account, self.key = level, account, key
        self.store = app.state.store
        self.limits = app.state.proxy.limits

    def caller(self, key=None, address="192.0.2.50"):
        headers = {"User-Agent": "python-requests/2.33.0"}
        if key is not False:
            headers["Authorization"] = f"Bearer {key or self.key}"
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(address, 1)),
            base_url="http://router.test",
            headers=headers,
        )

    def seed_used(self, used: int, seconds: int = 3600):
        now = time.time()
        self.store.record_usage(
            self.account["id"], window_start(now, seconds), seconds, used, 0, 0
        )
        self.limits.ledger.seed(self.store.open_usage_windows(now))

    def counter(self, seconds: int = 3600):
        return self.limits.ledger.counter(
            self.account["id"], window_start(time.time(), seconds), seconds
        )


async def build(
    tmp_path,
    *,
    budget=BUDGET,
    max_concurrency=2,
    clients=(),
    unsupported=(),
    max_response_bytes=8 * 1024 * 1024,
) -> Setup:
    fleet = Fleet()
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(fleet.handle)
    )
    store = app.state.store
    engines = [
        Engine(
            name=name,
            base_url=f"http://{host}/v1",
            unsupported_parameters=list(unsupported),
            max_response_bytes=max_response_bytes,
        )
        for name, host in (("Local A", "local-a.test"), ("Local B", "local-b.test"))
    ]
    ordered = Selector(engine_ids=[engine.id for engine in engines])
    level = AccountLevel(
        name="Standard",
        route_names=["free", "private", "defaulted"],
        token_budget=budget,
        max_concurrency=max_concurrency,
    )
    store.save(
        Configuration(
            engines=engines,
            routes=[
                Route(name="free", primary=ordered, strategy="ordered"),
                Route(
                    name="private",
                    primary=ordered,
                    strategy="ordered",
                    require_caller_key=True,
                ),
                Route(
                    name="defaulted",
                    primary=ordered,
                    strategy="ordered",
                    defaults={"max_tokens": 200},
                ),
            ],
            clients=list(clients),
            security=Security(operator_auth_enabled=False),
            accounts=AccountsSettings(enabled=True),
            account_levels=[level],
        )
    )
    account = store.create_account("alice", "Alice", level.id)
    token, _ = store.issue_activation(account["id"], "activate")
    assert (
        store.activate_account(token, digest("correct horse battery")) == account["id"]
    )
    key, _ = store.create_account_key(account["id"], "laptop")
    await app.state.discovery.refresh()
    return Setup(app, fleet, engines, level, store.account_snapshot(account["id"]), key)


@pytest.mark.asyncio
async def test_token_budget_rejects_before_upstream_send(tmp_path):
    setup = await build(tmp_path)
    setup.seed_used(BUDGET.max_tokens)
    before = time.time()
    async with setup.caller() as http:
        response = await complete(http)
    after = time.time()
    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "token_budget_exceeded"
    assert body["error"]["type"] == "rate_limit_exceeded"
    assert "3000 of 3000 tokens" in body["error"]["message"]
    resets_at = window_start(before, 3600) + 3600
    retry = int(response.headers["Retry-After"])
    assert math.ceil(resets_at - after) <= retry <= math.ceil(resets_at - before)
    assert setup.fleet.calls == []
    [event] = setup.store.events()
    assert event["status"] == "limited" and event["http_status"] == 429
    assert event["id"] == body["request_id"]
    assert event["limit"] == {"code": "token_budget_exceeded", "retry_after": retry}
    assert event["account_id"] == setup.account["id"] and "usage" not in event
    assert setup.limits.active(setup.account["id"]) == 0


@pytest.mark.asyncio
async def test_crossing_request_is_admitted_once(tmp_path):
    setup = await build(tmp_path)
    setup.seed_used(BUDGET.max_tokens - 1)
    async with setup.caller() as http:
        first = await complete(http)
        second = await complete(http)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "token_budget_exceeded"
    assert len(setup.fleet.calls) == 1
    counter = setup.counter()
    assert counter.used == BUDGET.max_tokens - 1 + 42 and counter.reserved == 0
    completed = next(e for e in setup.store.events() if e["status"] == "completed")
    assert completed["usage"] == {**USAGE, "estimated": False}
    assert completed["account_id"] == setup.account["id"]


@pytest.mark.asyncio
async def test_reservation_refuses_concurrent_requests_near_the_limit(tmp_path):
    setup = await build(tmp_path)
    setup.seed_used(BUDGET.max_tokens - 100)
    setup.fleet.gate = asyncio.Event()
    async with setup.caller() as http:
        first = asyncio.create_task(complete(http, max_tokens=500))
        await setup.fleet.wait_inflight(1)
        assert setup.counter().reserved == estimate_prompt_tokens(payload()) + 500
        second = await complete(http)
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "token_budget_exceeded"
        assert len(setup.fleet.calls) == 1
        setup.fleet.gate.set()
        assert (await first).status_code == 200
    counter = setup.counter()
    assert counter.reserved == 0 and counter.used == BUDGET.max_tokens - 100 + 42
    assert setup.limits.active(setup.account["id"]) == 0


@pytest.mark.asyncio
async def test_reservation_uses_max_tokens_then_route_default_then_1024(tmp_path):
    setup = await build(tmp_path)
    reserved = []
    setup.fleet.on_call = lambda _: reserved.append(setup.counter().reserved)
    prompt = estimate_prompt_tokens(payload())
    async with setup.caller() as http:
        for model, extra in (
            ("private", {"max_tokens": 500}),
            ("private", {"max_completion_tokens": 350}),
            ("defaulted", {}),
            ("private", {}),
            ("private", {"max_tokens": 50_000}),
        ):
            assert (await complete(http, model, **extra)).status_code == 200
    assert reserved == [
        prompt + 500,
        prompt + 350,
        prompt + 200,
        prompt + DEFAULT_COMPLETION_RESERVE,
        # A reservation never exceeds the budget itself.
        BUDGET.max_tokens,
    ]
    assert setup.counter().reserved == 0


class Clock:
    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


@pytest.mark.asyncio
async def test_fixed_window_rollover_charges_admission_window(tmp_path, monkeypatch):
    setup = await build(tmp_path)
    account = setup.account["id"]
    start = window_start(time.time(), 3600)
    clock = Clock(start + 3599)
    monkeypatch.setattr(gateway.proxy, "time", clock)
    setup.fleet.gate = asyncio.Event()
    async with setup.caller() as http:
        task = asyncio.create_task(complete(http))
        await setup.fleet.wait_inflight(1)
        clock.now += 5
        setup.fleet.gate.set()
        assert (await task).status_code == 200
    earlier = setup.limits.ledger.counter(account, start, 3600)
    later = setup.limits.ledger.snapshot(account, 3600, clock.now)
    assert earlier.used == 42 and earlier.reserved == 0 and earlier.requests == 1
    assert later.window_start == start + 3600
    assert later.used == 0 and later.reserved == 0
    [row] = setup.store.usage_windows(account)
    assert row["window_start"] == start
    assert row["prompt_tokens"] + row["completion_tokens"] == 42


@pytest.mark.asyncio
async def test_concurrency_limit_returns_429_without_retry_after(tmp_path):
    setup = await build(tmp_path)
    account = setup.account["id"]
    setup.fleet.gate = asyncio.Event()
    async with setup.caller() as http:
        first = asyncio.create_task(complete(http))
        second = asyncio.create_task(complete(http))
        await setup.fleet.wait_inflight(2)
        assert setup.limits.active(account) == 2
        third = await complete(http)
        assert third.status_code == 429
        body = third.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
        assert "2 active requests (limit 2)" in body["error"]["message"]
        assert "retry-after" not in third.headers
        assert len(setup.fleet.calls) == 2
        setup.fleet.gate.set()
        assert [(await first).status_code, (await second).status_code] == [200, 200]
    assert setup.limits.active(account) == 0
    limited = next(e for e in setup.store.events() if e["status"] == "limited")
    assert limited["limit"] == {
        "code": "concurrency_limit_exceeded",
        "retry_after": None,
    }
    assert setup.counter().used == 84 and setup.counter().reserved == 0


EXITS = {
    "retry_after_503": {"statuses": [503]},
    "retry_after_connect_error": {"errors": [httpx.ConnectError("refused")]},
    "all_engines_unavailable": {"statuses": [503, 503]},
    "non_stream_completed": {},
    "stream_completed": {"stream": True},
    "response_too_large": {"oversize": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", list(EXITS))
async def test_admission_is_held_across_retry_and_released_on_every_exit(
    tmp_path, exit_path
):
    case = EXITS[exit_path]
    setup = await build(tmp_path, max_response_bytes=1024)
    account = setup.account["id"]
    setup.fleet.statuses = list(case.get("statuses", []))
    setup.fleet.errors = list(case.get("errors", []))
    setup.fleet.oversize = case.get("oversize", False)
    active_during_calls = []
    setup.fleet.on_call = lambda _: active_during_calls.append(
        (setup.limits.active(account), setup.counter().reserved > 0)
    )
    async with setup.app.router.lifespan_context(setup.app), setup.caller() as http:
        response = await complete(http, stream=case.get("stream", False))
    event = setup.store.events()[0]
    counter = setup.counter()
    prompt = estimate_prompt_tokens(payload())
    assert setup.limits.active(account) == 0
    assert counter.reserved == 0
    assert event["account_id"] == account
    # The admission is held across every attempt, never re-claimed per attempt.
    assert active_during_calls == [(1, True)] * len(setup.fleet.calls)
    if exit_path == "all_engines_unavailable":
        assert response.status_code == 503 and len(setup.fleet.calls) == 2
        assert event["status"] == "failed" and event["http_status"] == 503
        assert event["usage"] == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "estimated": False,
        }
        assert counter.used == 0 and counter.requests == 1
    elif exit_path == "response_too_large":
        assert response.status_code == 502 and len(setup.fleet.calls) == 1
        assert event["status"] == "failed" and event["http_status"] == 502
        assert event["usage"] == {
            "prompt_tokens": prompt,
            "completion_tokens": DEFAULT_COMPLETION_RESERVE,
            "estimated": True,
        }
        assert counter.used == prompt + DEFAULT_COMPLETION_RESERVE
    else:
        expected_calls = 2 if exit_path.startswith("retry") else 1
        assert response.status_code == 200 and len(setup.fleet.calls) == expected_calls
        assert event["status"] == "completed"
        assert event["usage"] == {**USAGE, "estimated": False}
        assert counter.used == 42 and counter.requests == 1
    [row] = setup.store.usage_windows(account)
    assert (
        row["prompt_tokens"] + row["completion_tokens"] + row["estimated_tokens"]
        == counter.used
    )


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
async def test_cancelled_stream_is_charged_with_estimate(tmp_path):
    engine = FastAPI()

    @engine.get("/v1/models")
    async def catalog():
        return {"data": [{"id": "local-model"}]}

    @engine.post("/v1/chat/completions")
    async def completion():
        async def generate():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]})
            yield "\n\n"
            await asyncio.sleep(30)
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with serving(engine) as engine_url:
        app = create_app(str(tmp_path), background=False)
        store = app.state.store
        local = Engine(name="Socket engine", base_url=engine_url + "/v1")
        level = AccountLevel(
            name="Standard", route_names=["private"], token_budget=BUDGET
        )
        store.save(
            Configuration(
                engines=[local],
                routes=[
                    Route(
                        name="private",
                        primary=Selector(engine_ids=[local.id]),
                        require_caller_key=True,
                    )
                ],
                security=Security(operator_auth_enabled=False),
                accounts=AccountsSettings(enabled=True),
                account_levels=[level],
            )
        )
        account = store.create_account("alice", "Alice", level.id)
        token, _ = store.issue_activation(account["id"], "activate")
        assert store.activate_account(token, digest("correct horse battery"))
        key, _ = store.create_account_key(account["id"], "laptop")
        limits = app.state.proxy.limits
        async with (
            serving(app) as url,
            httpx.AsyncClient(
                base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=8
            ) as http,
        ):
            await app.state.discovery.refresh()
            async with http.stream(
                "POST", "/v1/chat/completions", json=payload(stream=True)
            ) as response:
                async for chunk in response.aiter_bytes():
                    assert b"hi" in chunk
                    break
            async with asyncio.timeout(5):
                while limits.active(account["id"]):
                    await asyncio.sleep(0.05)
        event = store.events()[0]
        assert event["status"] == "cancelled"
        assert event["usage"] == {
            "prompt_tokens": estimate_prompt_tokens(payload()),
            "completion_tokens": 0,
            "estimated": True,
        }
        counter = limits.ledger.counter(
            account["id"], window_start(time.time(), 3600), 3600
        )
        assert counter.used > 0 and counter.reserved == 0
        [row] = store.usage_windows(account["id"])
        assert row["estimated_tokens"] == counter.used and row["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_stream_usage_is_read_from_injected_usage_chunk_and_forwarded(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        response = await complete(http, stream=True)
    assert response.status_code == 200
    lines = [line for line in response.text.split("\n") if line.startswith("data:")]
    assert json.loads(lines[-2][5:]) == {"choices": [], "usage": USAGE}
    assert lines[-1] == "data: [DONE]"
    [(_, body)] = setup.fleet.calls
    assert body["stream_options"] == {"include_usage": True}
    event = setup.store.events()[0]
    assert event["stream_options_injected"] is True
    assert event["usage"] == {**USAGE, "estimated": False}
    assert setup.counter().used == 42


@pytest.mark.asyncio
async def test_caller_cannot_opt_out_of_include_usage(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        response = await complete(
            http,
            stream=True,
            stream_options={"include_usage": False, "include_obfuscation": False},
        )
    assert response.status_code == 200
    [(_, body)] = setup.fleet.calls
    assert body["stream_options"] == {
        "include_usage": True,
        "include_obfuscation": False,
    }
    assert setup.store.events()[0]["usage"] == {**USAGE, "estimated": False}


@pytest.mark.asyncio
async def test_stream_options_not_injected_when_engine_lists_it_unsupported(tmp_path):
    setup = await build(tmp_path, unsupported=["stream_options"])
    async with setup.caller() as http:
        response = await complete(http, stream=True)
    assert response.status_code == 200
    assert '"usage"' not in response.text
    [(_, body)] = setup.fleet.calls
    assert "stream_options" not in body
    event = setup.store.events()[0]
    assert "stream_options_injected" not in event
    # One content chunk is roughly one token; the [DONE] marker is excluded.
    assert event["usage"] == {
        "prompt_tokens": estimate_prompt_tokens(payload()),
        "completion_tokens": len(REPLY.split()),
        "estimated": True,
    }
    [row] = setup.store.usage_windows(setup.account["id"])
    assert row["estimated_tokens"] == setup.counter().used
    assert row["prompt_tokens"] == 0 and row["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_usage_detection_ignores_content_containing_the_word_usage(tmp_path):
    setup = await build(tmp_path, unsupported=["stream_options"])
    setup.fleet.stream_documents = [
        {
            "choices": [
                {
                    "delta": {
                        "content": 'the "usage": {"prompt_tokens": 9, '
                        '"completion_tokens": 9} object is text'
                    }
                }
            ]
        },
        {"choices": [{"delta": {"content": "more"}}], "usage": None},
        {
            "choices": [{"delta": {"content": "last"}}],
            "usage": {"prompt_tokens": "9", "completion_tokens": 9},
        },
    ]
    async with setup.caller() as http:
        response = await complete(http, stream=True)
    assert response.status_code == 200
    assert setup.store.events()[0]["usage"] == {
        "prompt_tokens": estimate_prompt_tokens(payload()),
        "completion_tokens": 3,
        "estimated": True,
    }
    # A real usage chunk is still found when it arrives split across chunks.
    meter = UsageMeter(payload(stream=True))
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: {"choices":[],"us'
    )
    meter.feed(b'age":{"prompt_tokens":5,"completion_tokens":6}}\n\ndata: [DONE]\n\n')
    usage = meter.result("completed", 200)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.estimated) == (
        5,
        6,
        False,
    )


@pytest.mark.asyncio
async def test_every_account_principal_is_metered_even_without_a_budget(tmp_path):
    setup = await build(tmp_path, budget=None, max_concurrency=None)
    account = setup.account["id"]
    async with setup.caller() as http:
        assert (await complete(http)).status_code == 200
    [row] = setup.store.usage_windows(account)
    assert row["window_seconds"] == REPORTING_WINDOW_SECONDS
    assert (row["prompt_tokens"], row["completion_tokens"], row["requests"]) == (
        12,
        30,
        1,
    )
    snapshot = setup.limits.snapshot(account, setup.level, time.time())
    assert snapshot["max_tokens"] is None and snapshot["max_concurrency"] is None
    assert snapshot["used"] == 42 and snapshot["active_requests"] == 0
    assert snapshot["resets_at"] == snapshot["window_start"] + REPORTING_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_client_and_unkeyed_principals_are_never_limited_or_metered(
    tmp_path, monkeypatch
):
    harness = Client(name="Harness", route_names=["free", "private"])
    setup = await build(tmp_path, clients=[harness])
    client_key = setup.store.issue_key(harness.id)
    admissions = []
    original = setup.limits.admit
    monkeypatch.setattr(
        setup.limits,
        "admit",
        lambda *args, **kwargs: admissions.append(args) or original(*args, **kwargs),
    )
    async with setup.caller(key=client_key) as http:
        assert (await complete(http)).status_code == 200
        assert (await complete(http, stream=True)).status_code == 200
    async with setup.caller(key=False) as http:
        assert (await complete(http, "free")).status_code == 200
    assert admissions == []
    assert setup.store.open_usage_windows(0) == []
    assert setup.limits.ledger.windows == {}
    events = setup.store.events()
    assert len(events) == 3
    for event in events:
        assert not {"usage", "account_id", "limit", "stream_options_injected"} & set(
            event
        )
    streamed = next(body for _, body in setup.fleet.calls if body.get("stream"))
    assert "stream_options" not in streamed


@pytest.mark.asyncio
async def test_usage_persists_and_reloads_across_restart(tmp_path):
    setup = await build(tmp_path)
    account = setup.account["id"]
    async with setup.caller() as http:
        assert (await complete(http)).status_code == 200
        assert (await complete(http)).status_code == 200
    assert setup.counter().used == 84
    start = window_start(time.time(), 3600)
    # Usage recorded by another process reaches this one only at start.
    setup.store.record_usage(account, start, 3600, BUDGET.max_tokens - 84, 0, 0)
    reopened = create_app(
        str(tmp_path),
        background=False,
        transport=httpx.MockTransport(setup.fleet.handle),
    )
    counter = reopened.state.proxy.limits.ledger.counter(account, start, 3600)
    assert counter.used == BUDGET.max_tokens and counter.requests == 3
    assert counter.reserved == 0
    await reopened.state.discovery.refresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reopened, client=("192.0.2.50", 1)),
        base_url="http://router.test",
        headers={"Authorization": f"Bearer {setup.key}"},
    ) as http:
        response = await complete(http)
    assert response.status_code == 429
    assert len(setup.fleet.calls) == 2


@pytest.mark.asyncio
async def test_usage_rows_and_events_contain_no_content(tmp_path):
    setup = await build(tmp_path)
    async with setup.caller() as http:
        assert (await complete(http)).status_code == 200
        assert (await complete(http, stream=True)).status_code == 200
    events = setup.store.events()
    serialized = json.dumps(
        [
            events,
            setup.store.usage_windows(setup.account["id"]),
            [
                list(row)
                for row in setup.store.db.execute("SELECT * FROM usage_windows")
            ],
        ]
    )
    for marker in ("PROMPT-MARKER-7f3a", "COMPLETION-MARKER-9c1d", setup.key):
        assert marker not in serialized
    assert all(event["usage"] == {**USAGE, "estimated": False} for event in events)
