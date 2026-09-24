"""Operators compare engines and model replacements from request history."""

import threading

import httpx
import pytest
from test_gateway import send, setup  # noqa: F401  (pytest fixture)

import gateway.app
from gateway.performance import percentile, summarize


def served(engine_id, model, ts, performance, attempts_before=()):
    attempts = list(attempts_before)
    attempts.append(
        {
            "engine_id": engine_id,
            "engine": "Name " + engine_id,
            "model": model,
            "status": "responded",
            "http_status": 200,
        }
    )
    return {
        "ts": ts,
        "status": "completed",
        "http_status": 200,
        "attempts": attempts,
        "performance": performance,
    }


def stream_timing(first_chunk_ms, tokens_per_second, estimated=False):
    return {
        "stream": True,
        "upstream_ms": first_chunk_ms + 500,
        "first_chunk_ms": first_chunk_ms,
        "generation_ms": 500,
        "completion_tokens": 20,
        "tokens_estimated": estimated,
        "tokens_per_second": tokens_per_second,
    }


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.5) is None
    assert percentile([7], 0.95) == 7
    assert percentile([4, 1, 3, 2], 0.5) == 2
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10


def test_summary_attributes_each_attempt_to_its_own_engine_and_model():
    busy = {
        "engine_id": "a",
        "engine": "Engine A",
        "model": "m",
        "status": "responded",
        "http_status": 503,
    }
    unreachable = {
        "engine_id": "a",
        "engine": "Engine A",
        "model": "m",
        "status": "failed",
    }
    events = [
        served("a", "m", 10, stream_timing(100, 40.0)),
        served("a", "m", 20, stream_timing(300, 20.0, estimated=True)),
        served("b", "n", 30, stream_timing(900, 5.0), attempts_before=[busy]),
        served("b", "n", 40, stream_timing(800, 6.0), attempts_before=[unreachable]),
        # A stream interrupted after the engine answered is that engine's failure.
        {
            "ts": 50,
            "status": "failed",
            "http_status": 502,
            "attempts": [
                {
                    "engine_id": "b",
                    "engine": "Engine B",
                    "model": "n",
                    "status": "responded",
                    "http_status": 200,
                }
            ],
        },
        # Cancellation and an engine's own 4xx answer are not engine failures.
        {
            "ts": 60,
            "status": "cancelled",
            "attempts": [
                {
                    "engine_id": "a",
                    "engine": "Engine A",
                    "model": "m",
                    "status": "cancelled",
                }
            ],
        },
        {
            "ts": 70,
            "status": "failed",
            "http_status": 400,
            "attempts": [
                {
                    "engine_id": "a",
                    "engine": "Engine A",
                    "model": "m",
                    "status": "responded",
                    "http_status": 400,
                }
            ],
        },
        # Refusals never reached an engine.
        {"ts": 80, "status": "denied", "http_status": 403, "attempts": []},
    ]

    rows = summarize(
        events,
        names={"a": "Engine A renamed"},
        catalogs={"a": {"m"}, "b": set()},
    )

    assert [(row["engine_id"], row["model"]) for row in rows] == [
        ("a", "m"),
        ("b", "n"),
    ]
    first, second = rows
    assert first["engine"] == "Engine A renamed"
    assert first["served"] == 2
    assert first["failed"] == 2
    assert first["failure_rate"] == 0.5
    assert first["in_catalog"] is True
    assert first["first_seen"] == 10
    assert first["last_seen"] == 70
    assert first["stream"] == {
        "samples": 2,
        "first_chunk_ms_p50": 100,
        "first_chunk_ms_p95": 300,
        "tokens_per_second_p50": 20.0,
        "estimated_samples": 1,
    }
    assert first["non_stream"] == {
        "samples": 0,
        "upstream_ms_p50": None,
        "upstream_ms_p95": None,
    }
    assert second["engine"] == "Engine B"
    assert second["served"] == 2
    assert second["failed"] == 1
    assert second["in_catalog"] is False


def test_unknown_catalog_is_not_reported_as_replaced():
    rows = summarize(
        [served("gone", "m", 1, stream_timing(100, 10.0))], names={}, catalogs={}
    )
    assert rows[0]["in_catalog"] is None


async def test_performance_api_compares_a_model_replacement_and_failover(setup):  # noqa: F811 (pytest fixture)
    app, http, fleet, local, cloud, _ = setup
    assert (await send(http)).status_code == 200
    assert (await send(http)).status_code == 200

    fleet.models["local.test"] = ["replacement-model"]
    await app.state.discovery.refresh()
    assert (await send(http)).status_code == 200

    fleet.failure["local.test"] = 503
    assert (await send(http)).json()["model"] == "remote-model"

    result = await http.get("/api/v1/performance", params={"hours": 24})
    assert result.status_code == 200
    body = result.json()
    assert body["window_hours"] == 24
    rows = {}
    for row in body["rows"]:
        rows[(row["engine_id"], row["model"])] = row

    before = rows[(local.id, "first-model")]
    after = rows[(local.id, "replacement-model")]
    backup = rows[(cloud.id, "remote-model")]
    assert before["served"] == 2 and before["failed"] == 0
    assert before["in_catalog"] is False
    assert after["served"] == 1 and after["failed"] == 1
    assert after["in_catalog"] is True
    assert backup["served"] == 1
    assert before["non_stream"]["samples"] == 2
    assert before["non_stream"]["upstream_ms_p50"] is not None
    assert "private prompt marker" not in result.text


@pytest.mark.parametrize("hours", ["0", "169", "soon"])
async def test_performance_api_bounds_the_window_to_history_retention(setup, hours):  # noqa: F811 (pytest fixture)
    _, http, *_ = setup
    result = await http.get("/api/v1/performance", params={"hours": hours})
    assert result.status_code in {400, 422}


async def test_performance_api_requires_the_operator(setup):  # noqa: F811 (pytest fixture)
    app, *_ = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5124)),
        base_url="http://localhost",
    ) as anonymous:
        result = await anonymous.get("/api/v1/performance")
    assert result.status_code == 401


async def test_performance_summary_runs_off_the_event_loop(setup, monkeypatch):  # noqa: F811 (pytest fixture)
    _, http, *_ = setup
    loop_thread = threading.get_ident()
    threads = []

    def recording_summarize(events, names, catalogs):
        threads.append(threading.get_ident())
        return summarize(events, names, catalogs)

    monkeypatch.setattr(gateway.app, "summarize", recording_summarize)
    assert (await send(http)).status_code == 200
    result = await http.get("/api/v1/performance")
    assert result.status_code == 200
    assert len(threads) == 1
    assert threads[0] != loop_thread
