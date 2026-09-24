"""Measure how an engine served one request from clock readings and token counts."""

from __future__ import annotations

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
