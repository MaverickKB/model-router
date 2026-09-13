"""Terminal SSE framing and request outcomes are independent of HTTP EOF."""

import asyncio

import pytest
from anyio import create_task_group

from gateway.discovery import Observation
from gateway.proxy import InflightRequest
from gateway.stream_protocol import CompletionMarker


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("space", [b"", b" "])
def test_terminal_marker_accepts_every_chunk_split(ending, space):
    data = b'data: {"delta":"ordinary content"}' + ending * 2
    data += b"data:" + space + b"[DONE]" + ending * 2
    for split in range(len(data) + 1):
        marker = CompletionMarker()
        marker.feed(data[:split])
        marker.feed(data[split:])
        assert marker.complete, split
    marker = CompletionMarker()
    for value in data:
        marker.feed(bytes([value]))
    assert marker.complete


@pytest.mark.parametrize(
    "data",
    [
        b'data: {"text":"[DONE]"}\n\n',
        b'data: {"text":"data: [DONE]\\n\\n"}\n\n',
        b": data: [DONE]\n\n",
        b"event: [DONE]\n\n",
        b"data: [DONE]",
        b"data: [DONE]\n",
        b"data: [DONE]\r\n",
        b"data: [DONE]\ndata: more\n\n",
        b"data:\ndata: [DONE]\n\n",
        b"data: [DONE]\ndata:\n\n",
        b"data: [DO\ndata: NE]\n\n",
        b"data:  [DONE]\n\n",
        b"data: [DONE] \n\n",
        b"data: [DONE]suffix\n\n",
    ],
)
def test_nonterminal_data_never_claims_completion(data):
    for split in range(len(data) + 1):
        marker = CompletionMarker()
        marker.feed(data[:split])
        marker.feed(data[split:])
        assert not marker.complete, split


def test_marker_ignores_sse_comments_fields_and_initial_bom():
    marker = CompletionMarker()
    data = b"\xef\xbb\xbf: keepalive\r\nid: 9\r\ndata: [DONE]\r\n\r\n"
    for value in data:
        marker.feed(bytes([value]))
    assert marker.complete


def test_large_payload_does_not_grow_parser_storage_or_match_a_truncated_line():
    marker = CompletionMarker()
    marker.feed(b"data: [DONE]" + b"x" * 100_000)
    assert len(marker._line) <= marker._LINE_LIMIT
    marker.feed(b"\n\n")
    assert not marker.complete
    marker.feed(b"data: [DONE]\n\n")
    assert marker.complete


@pytest.mark.parametrize("terminal", [b"data: [DONE]\n\n", b"data: [DONE]\r\n\r\n"])
async def test_disconnect_during_completion_write_keeps_completed_and_releases(
    terminal,
):
    observation = Observation(engine_id="stream-test")
    connection = InflightRequest(observation, failure_cooldown=1)
    statuses, closed = [], []
    writing = asyncio.Event()
    allow_write = asyncio.Event()

    class Upstream:
        async def aclose(self):
            await asyncio.sleep(0)
            closed.append(True)

    connection.response = Upstream()

    async def chunks():
        pytest.fail("The terminal marker must end consumption before HTTP EOF")
        yield b"unreachable"

    async def finish(status, code=None):
        writing.set()
        await allow_write.wait()
        statuses.append((status, code))

    async def consume():
        async for _ in connection.relay(terminal, chunks(), finish):
            pass

    async with create_task_group() as tasks:
        tasks.start_soon(consume)
        await writing.wait()
        tasks.cancel_scope.cancel()
        allow_write.set()
    assert statuses == [("completed", 200)]
    assert closed == [True]
    assert observation.inflight == 0
    assert observation.last_success is not None


async def test_closing_generator_after_terminal_yield_records_completed():
    observation = Observation(engine_id="stream-test")
    connection = InflightRequest(observation, failure_cooldown=1)
    statuses = []

    async def chunks():
        await asyncio.Future()
        yield b"unreachable"

    async def finish(status, code=None):
        statuses.append((status, code))

    stream = connection.relay(b"data: [DONE]\n\n", chunks(), finish)
    assert await anext(stream) == b"data: [DONE]\n\n"
    await stream.aclose()
    assert statuses == [("completed", 200)]
    assert observation.inflight == 0


async def test_end_of_stream_without_terminal_marker_never_records_success():
    observation = Observation(engine_id="stream-test")
    recorded, statuses = [], []

    async def record_success():
        recorded.append(True)

    connection = InflightRequest(
        observation,
        failure_cooldown=1,
        record_success=record_success,
    )

    async def chunks():
        if False:
            yield b"unreachable"

    async def finish(status, code=None):
        statuses.append((status, code))

    body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    output = [chunk async for chunk in connection.relay(body, chunks(), finish)]
    assert output[-1] == (
        b'event: error\ndata: {"error":{"message":"Upstream stream ended before completion"}}\n\n'
    )
    assert recorded == []
    assert statuses == [("failed", 502)]
    assert observation.last_success is None
    assert observation.inflight == 0
