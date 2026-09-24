"""Admit account requests against level budgets and concurrency; meter usage without content."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal

from ..schema import AccountLevel

# Accounts without a budget are still metered so usage can be shown before a
# limit exists.
REPORTING_WINDOW_SECONDS = 86400
DEFAULT_COMPLETION_RESERVE = 1024
# The stream tail kept for usage detection. It is process memory only and
# dies with the request.
TAIL_BYTES = 32768


def window_start(now: float, seconds: int) -> float:
    return float(math.floor(now / seconds) * seconds)


def estimate_prompt_tokens(payload: dict) -> int:
    source = payload.get("messages") or payload.get("prompt") or ""
    return math.ceil(len(json.dumps(source)) / 4)


def _positive(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def completion_reserve(payload: dict, decision: dict, level: AccountLevel) -> int:
    defaults = decision.get("defaults") or {}
    reserve = next(
        (
            value
            for value in (
                _positive(payload.get("max_tokens")),
                _positive(payload.get("max_completion_tokens")),
                _positive(defaults.get("max_tokens")),
                _positive(defaults.get("max_completion_tokens")),
            )
            if value is not None
        ),
        DEFAULT_COMPLETION_RESERVE,
    )
    if level.token_budget:
        reserve = max(1, min(reserve, level.token_budget.max_tokens))
    return reserve


def _usage_counts(document) -> tuple[int, int] | None:
    """Accept only a top-level usage object with integer counts."""
    if not isinstance(document, dict) or not isinstance(document.get("usage"), dict):
        return None
    usage = document["usage"]
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (prompt, completion)):
        return None
    if prompt < 0 or completion < 0:
        return None
    return prompt, completion


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int
    estimated: bool

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def row(self) -> tuple[int, int, int]:
        # Reported counts fill the prompt and completion columns; an estimate
        # fills estimated_tokens only, so readers can tell the two apart.
        if self.estimated:
            return 0, 0, self.total
        return self.prompt_tokens, self.completion_tokens, 0


@dataclass
class WindowCounter:
    account_id: str
    window_start: float
    window_seconds: int
    used: int = 0
    reserved: int = 0
    requests: int = 0


class UsageLedger:
    """In-memory window counters; authoritative for enforcement in this process."""

    def __init__(self):
        self.windows: dict[tuple[str, float, int], WindowCounter] = {}

    def counter(
        self, account_id: str, window_start: float, window_seconds: int
    ) -> WindowCounter:
        key = (account_id, window_start, window_seconds)
        counter = self.windows.get(key)
        if counter is None:
            counter = self.windows[key] = WindowCounter(
                account_id, window_start, window_seconds
            )
        return counter

    def seed(self, rows: list[dict]) -> None:
        for row in rows:
            counter = self.counter(
                row["account_id"], row["window_start"], row["window_seconds"]
            )
            counter.used += (
                row["prompt_tokens"]
                + row["completion_tokens"]
                + row["estimated_tokens"]
            )
            counter.requests += row["requests"]

    def prune(self, now: float) -> None:
        # One full extra window is kept so a request that straddles a boundary
        # always finds the counter it was admitted against.
        stale = [
            key
            for key, counter in self.windows.items()
            if counter.reserved == 0
            and counter.window_start + 2 * counter.window_seconds < now
        ]
        for key in stale:
            del self.windows[key]

    def snapshot(
        self, account_id: str, window_seconds: int, now: float
    ) -> WindowCounter:
        start = window_start(now, window_seconds)
        return self.windows.get(
            (account_id, start, window_seconds),
            WindowCounter(account_id, start, window_seconds),
        )


class AccountSlots:
    def __init__(self):
        self.active_requests: dict[str, int] = {}

    def active(self, account_id: str) -> int:
        return self.active_requests.get(account_id, 0)

    def claim(self, account_id: str) -> None:
        self.active_requests[account_id] = self.active(account_id) + 1

    def release(self, account_id: str) -> None:
        remaining = self.active(account_id) - 1
        if remaining > 0:
            self.active_requests[account_id] = remaining
        else:
            self.active_requests.pop(account_id, None)


@dataclass
class Refusal:
    code: Literal["token_budget_exceeded", "concurrency_limit_exceeded"]
    message: str
    retry_after: int | None

    def body(self, request_id: str) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": "rate_limit_exceeded",
                "code": self.code,
                "param": None,
            },
            "request_id": request_id,
        }

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after else {}


class AccountAdmission:
    """One admitted request: a reservation in its admission window and a slot."""

    def __init__(
        self,
        limits: AccountLimits,
        counter: WindowCounter,
        reserve: int,
        prompt_estimate: int,
    ):
        self.limits = limits
        self.account_id = counter.account_id
        self.window_start = counter.window_start
        self.window_seconds = counter.window_seconds
        self.reserve = reserve
        self.prompt_estimate = prompt_estimate
        self.slot_held = True
        self.settled = False

    def settle(self, usage: Usage) -> None:
        # Synchronous and idempotent: plain dictionary arithmetic, so a
        # cancellation cannot land between releasing the slot and charging.
        if self.settled:
            return
        self.settled = True
        # Always the admission window, never the finish window, so a request
        # that straddles a boundary is charged where it was admitted.
        counter = self.limits.ledger.counter(
            self.account_id, self.window_start, self.window_seconds
        )
        counter.reserved -= self.reserve
        counter.used += usage.total
        counter.requests += 1
        self.limits.slots.release(self.account_id)
        self.slot_held = False


class AccountLimits:
    """One per Proxy. Per-process, like engine capacity."""

    def __init__(self):
        self.ledger = UsageLedger()
        self.slots = AccountSlots()

    def admit(
        self,
        account_id: str,
        level: AccountLevel,
        payload: dict,
        decision: dict,
        now: float,
    ) -> AccountAdmission | Refusal:
        # This check-and-increment is atomic on the event loop: no await may
        # enter between the two checks and the two increments.
        budget = level.token_budget
        seconds = budget.window_seconds if budget else REPORTING_WINDOW_SECONDS
        self.ledger.prune(now)
        counter = self.ledger.counter(account_id, window_start(now, seconds), seconds)
        active = self.slots.active(account_id)
        if level.max_concurrency is not None and active >= level.max_concurrency:
            return Refusal(
                "concurrency_limit_exceeded",
                f"This account already has {active} active requests"
                f" (limit {level.max_concurrency}). Retry when one finishes.",
                None,
            )
        if budget and counter.used + counter.reserved >= budget.max_tokens:
            retry = math.ceil(counter.window_start + seconds - now)
            return Refusal(
                "token_budget_exceeded",
                f"Token budget for this account is used up: {counter.used} of"
                f" {budget.max_tokens} tokens in the current {seconds}-second"
                f" window. Retry after {retry} seconds.",
                retry,
            )
        prompt_estimate = estimate_prompt_tokens(payload)
        reserve = prompt_estimate + completion_reserve(payload, decision, level)
        if budget:
            reserve = max(1, min(reserve, budget.max_tokens))
        counter.reserved += reserve
        self.slots.claim(account_id)
        return AccountAdmission(self, counter, reserve, prompt_estimate)

    def active(self, account_id: str) -> int:
        return self.slots.active(account_id)

    def snapshot(self, account_id: str, level: AccountLevel, now: float) -> dict:
        budget = level.token_budget
        seconds = budget.window_seconds if budget else REPORTING_WINDOW_SECONDS
        counter = self.ledger.snapshot(account_id, seconds, now)
        return {
            "window_start": counter.window_start,
            "window_seconds": seconds,
            "max_tokens": budget.max_tokens if budget else None,
            "used": counter.used,
            "reserved": counter.reserved,
            "active_requests": self.slots.active(account_id),
            "max_concurrency": level.max_concurrency,
            "resets_at": counter.window_start + seconds,
        }


class UsageMeter:
    """Count tokens from the upstream usage report, else estimate from sizes.

    Stream bytes are counted and only a bounded tail is kept for the final
    usage chunk; nothing is decoded or retained beyond the request.
    """

    def __init__(self, payload: dict):
        self.prompt_estimate = estimate_prompt_tokens(payload)
        self.stream = bool(payload.get("stream"))
        self.bytes = 0
        self.chunks = 0
        self.tail = b""
        self.reported: tuple[int, int] | None = None
        self.body_seen = False
        self.completion_estimate = 0
        self.forced: tuple[int, int] | None = None

    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        self.chunks += chunk.count(b"data:")
        self.tail = (self.tail + chunk)[-TAIL_BYTES:]

    def feed_json(self, content: bytes) -> None:
        self.body_seen = True
        try:
            document = json.loads(content)
        except (ValueError, RecursionError):
            # Deeply nested JSON is valid for the client; counting it is
            # optional, so the meter falls back to an estimate.
            return
        self.reported = _usage_counts(document)
        if self.reported is not None or not isinstance(document, dict):
            return
        text = 0
        for choice in document.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            value = (
                message.get("content") if isinstance(message, dict) else None
            ) or choice.get("text")
            if isinstance(value, str):
                text += len(value)
        self.completion_estimate = math.ceil(text / 4)

    def oversized(self, reserve: int) -> None:
        # The response was discarded past the size limit: charge the whole
        # reservation on purpose rather than guess how much was generated.
        self.forced = (self.prompt_estimate, max(0, reserve - self.prompt_estimate))

    def _reported_from_tail(self) -> tuple[int, int] | None:
        for line in reversed(self.tail.split(b"\n")):
            line = line.strip()
            if not line.startswith(b"data:") or b'"usage"' not in line:
                continue
            try:
                counts = _usage_counts(json.loads(line[5:].strip()))
            except (ValueError, RecursionError):
                continue
            if counts is not None:
                return counts
        return None

    def result(self, status: str, code: int | None = None) -> Usage:
        if self.forced is not None:
            return Usage(*self.forced, estimated=True)
        reported = self.reported
        if reported is None and self.stream and self.bytes:
            reported = self._reported_from_tail()
        if reported is not None:
            return Usage(*reported, estimated=False)
        if self.bytes:
            # A stream reached the client (completed, cancelled or interrupted):
            # one chunk is roughly one token, with the [DONE] marker excluded.
            return Usage(self.prompt_estimate, max(0, self.chunks - 1), True)
        if status == "completed":
            return Usage(self.prompt_estimate, self.completion_estimate, True)
        if status == "cancelled" or (status == "failed" and self.body_seen):
            # The prompt was sent; nothing usable came back.
            return Usage(self.prompt_estimate, 0, True)
        # No engine produced anything: an exhausted fleet or a denial between
        # attempts. Nothing is charged, but the reservation is still released.
        return Usage(0, 0, False)
