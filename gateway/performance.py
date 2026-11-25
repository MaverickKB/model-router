"""Measure how an engine served one request from clock readings and token counts."""

from __future__ import annotations

import math
import time

from .accounts.limits import Usage


def _milliseconds(start: float, end: float) -> int:
    return round((end - start) * 1000)


class ServedTiming:
    """Time the upstream attempt that served a request.

    Only clock readings are kept. Chunk contents never reach this class.
    """

    def __init__(self):
        self.sent: float | None = None
        self.first_chunk: float | None = None
        self.last_chunk: float | None = None
        self.body_read: float | None = None

    def mark_sent(self) -> None:
        # Each attempt starts a fresh measurement, so a failed attempt on
        # another engine never counts toward the engine that served.
        self.sent = time.monotonic()
        self.first_chunk = None
        self.last_chunk = None
        self.body_read = None

    def mark_chunk(self) -> None:
        now = time.monotonic()
        if self.first_chunk is None:
            self.first_chunk = now
        self.last_chunk = now

    def mark_body_read(self) -> None:
        self.body_read = time.monotonic()

    def summary(self, usage: Usage) -> dict | None:
        sent = self.sent
        first_chunk = self.first_chunk
        last_chunk = self.last_chunk
        body_read = self.body_read
        if sent is None:
            return None

        stream = False
        first_chunk_ms = None
        generation_ms = None
        tokens_per_second = None
        if first_chunk is not None and last_chunk is not None:
            stream = True
            upstream_ms = _milliseconds(sent, last_chunk)
            first_chunk_ms = _milliseconds(sent, first_chunk)
            generation_ms = _milliseconds(first_chunk, last_chunk)
            # A rate needs a measured interval after the first chunk.
            if generation_ms > 0 and usage.completion_tokens > 1:
                seconds = last_chunk - first_chunk
                tokens_per_second = round(usage.completion_tokens / seconds, 1)
        elif body_read is not None:
            upstream_ms = _milliseconds(sent, body_read)
        else:
            return None

        return {
            "stream": stream,
            "upstream_ms": upstream_ms,
            "first_chunk_ms": first_chunk_ms,
            "generation_ms": generation_ms,
            "completion_tokens": usage.completion_tokens,
            "tokens_estimated": usage.estimated,
            "tokens_per_second": tokens_per_second,
        }


# Upstream answers that route the request to another candidate.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def percentile(values: list, fraction: float):
    """Nearest-rank percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    rank = max(rank, 1)
    return ordered[rank - 1]


def _attempt_outcome(attempt: dict, is_final: bool, event: dict) -> str:
    """Classify one attempt as ``served``, ``failed`` or ``other``.

    Only what the engine itself did counts against it. Cancellations and
    the engine's own 4xx answers to the request are ``other``.
    """
    if is_final and event.get("status") == "completed":
        return "served"
    if attempt.get("status") == "failed":
        return "failed"
    http_status = attempt.get("http_status")
    if isinstance(http_status, int) and (
        http_status in RETRY_STATUSES or http_status >= 500
    ):
        return "failed"
    # An interrupted stream or an oversized body after a successful answer.
    if is_final and event.get("status") == "failed" and event.get("http_status") == 502:
        return "failed"
    return "other"


def _new_row(engine_id: str, model: str, ts: float) -> dict:
    return {
        "engine_id": engine_id,
        "engine": engine_id,
        "model": model,
        "served": 0,
        "failed": 0,
        "first_seen": ts,
        "last_seen": ts,
        "first_chunk_ms": [],
        "tokens_per_second": [],
        "estimated_samples": 0,
        "upstream_ms": [],
    }


def _record_served(row: dict, performance: dict) -> None:
    if performance.get("stream"):
        first_chunk_ms = performance.get("first_chunk_ms")
        if isinstance(first_chunk_ms, (int, float)):
            row["first_chunk_ms"].append(first_chunk_ms)
        tokens_per_second = performance.get("tokens_per_second")
        if isinstance(tokens_per_second, (int, float)):
            row["tokens_per_second"].append(tokens_per_second)
            if performance.get("tokens_estimated"):
                row["estimated_samples"] += 1
    else:
        upstream_ms = performance.get("upstream_ms")
        if isinstance(upstream_ms, (int, float)):
            row["upstream_ms"].append(upstream_ms)


def _finish_row(row: dict, names: dict, catalogs: dict) -> dict:
    engine_id = row["engine_id"]
    name = names.get(engine_id)
    if name is None:
        name = row["engine"]
    # None means the engine's current catalog is unknown, so the model can be
    # neither confirmed nor reported as replaced.
    in_catalog = None
    catalog = catalogs.get(engine_id)
    if catalog is not None:
        in_catalog = row["model"] in catalog
    counted = row["served"] + row["failed"]
    failure_rate = None
    if counted:
        failure_rate = round(row["failed"] / counted, 4)
    return {
        "engine_id": engine_id,
        "engine": name,
        "model": row["model"],
        "in_catalog": in_catalog,
        "served": row["served"],
        "failed": row["failed"],
        "failure_rate": failure_rate,
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "stream": {
            "samples": len(row["first_chunk_ms"]),
            "first_chunk_ms_p50": percentile(row["first_chunk_ms"], 0.5),
            "first_chunk_ms_p95": percentile(row["first_chunk_ms"], 0.95),
            "tokens_per_second_p50": percentile(row["tokens_per_second"], 0.5),
            "estimated_samples": row["estimated_samples"],
        },
        "non_stream": {
            "samples": len(row["upstream_ms"]),
            "upstream_ms_p50": percentile(row["upstream_ms"], 0.5),
            "upstream_ms_p95": percentile(row["upstream_ms"], 0.95),
        },
    }


def _row_order(row: dict):
    # Engines alphabetically; within an engine the most recently used model
    # first, so a replacement sits directly above the model it replaced.
    return (row["engine"].lower(), row["engine_id"], -row["last_seen"], row["model"])


def summarize(events: list[dict], names: dict, catalogs: dict) -> list[dict]:
    """Group request history by the engine and model each attempt used.

    ``names`` maps engine ids to current names. ``catalogs`` maps engine ids
    to the model ids currently served; an absent engine has an unknown catalog.
    """
    rows = {}
    for event in events:
        attempts = event.get("attempts") or []
        ts = event.get("ts", 0)
        for index, attempt in enumerate(attempts):
            engine_id = attempt.get("engine_id")
            model = attempt.get("model")
            if not engine_id or not model:
                continue
            key = (engine_id, model)
            row = rows.get(key)
            if row is None:
                row = _new_row(engine_id, model, ts)
                rows[key] = row
            row["first_seen"] = min(row["first_seen"], ts)
            if ts >= row["last_seen"]:
                row["last_seen"] = ts
                if attempt.get("engine"):
                    row["engine"] = attempt["engine"]

            is_final = index == len(attempts) - 1
            outcome = _attempt_outcome(attempt, is_final, event)
            if outcome == "served":
                row["served"] += 1
                performance = event.get("performance")
                if isinstance(performance, dict):
                    _record_served(row, performance)
            elif outcome == "failed":
                row["failed"] += 1

    finished = []
    for row in rows.values():
        finished.append(_finish_row(row, names, catalogs))
    finished.sort(key=_row_order)
    return finished
