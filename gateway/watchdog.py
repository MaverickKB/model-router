"""Runaway-output watchdog for the stream relay.

Inspects streamed server-sent-event bytes without retaining content and
returns a trip reason when a stream looks runaway: repeated text, reasoning
growth with no answer, a missing first token, or no progress. The relay owns
the no-progress window because only it observes a total stall; the other three
trigger on chunk arrival in ``Watchdog.feed``.
"""

import json
import time


class WatchdogTrip(Exception):
    """Raised inside the relay so a stream can be cancelled and marked."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def extract_stream_text(chunk: bytes) -> tuple[str, str]:
    """Return (content_text, reasoning_text) from one SSE chunk.

    Never retains content; callers keep only lengths and a bounded window.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw = chunk.decode("utf-8", "replace")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        if len(payload) > 64 * 1024:
            # An oversized or pathological single line is not an output delta.
            continue
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            continue
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(text, str):
                content_parts.append(text)
            if isinstance(thought, str):
                reasoning_parts.append(thought)
    return "".join(content_parts), "".join(reasoning_parts)


class Watchdog:
    """One per streamed request; feed every decoded chunk in order."""

    def __init__(
        self,
        *,
        repeat_window: int,
        max_reasoning_bytes_without_answer: int,
        first_token_timeout_seconds: float,
        no_progress_timeout_seconds: float,
    ):
        self.repeat_window = repeat_window
        self.max_reasoning_bytes_without_answer = max_reasoning_bytes_without_answer
        self.first_token_timeout_seconds = first_token_timeout_seconds
        # The relay reads this to bound a silent read; the watchdog itself only
        # needs the chunk-arrival rules.
        self.no_progress_timeout_seconds = no_progress_timeout_seconds
        self._started = time.monotonic()
        self._reasoning_bytes = 0
        self._saw_answer = False
        self._recent_content: list[str] = []

    def feed(self, chunk: bytes) -> str | None:
        """Inspect one chunk; return a trip reason string or None."""
        content, reasoning = extract_stream_text(chunk)
        if reasoning:
            self._reasoning_bytes += len(reasoning)
        if content:
            delta = content.strip()
            if delta:
                self._recent_content.append(delta)
                if len(self._recent_content) > self.repeat_window:
                    self._recent_content.pop(0)
                self._saw_answer = True
                if self._repeated():
                    return "repeated_text"
        if not self._saw_answer:
            if self._reasoning_bytes > self.max_reasoning_bytes_without_answer:
                return "reasoning_without_answer"
            if time.monotonic() - self._started >= self.first_token_timeout_seconds:
                return "first_token_timeout"
        return None

    def _repeated(self) -> bool:
        if len(self._recent_content) < self.repeat_window:
            return False
        return all(
            part == self._recent_content[-1] for part in self._recent_content
        )
