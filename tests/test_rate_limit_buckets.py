"""Per-key token buckets never evict a live budget to make room for a new key."""

import pytest
from fastapi import HTTPException

from gateway.security import limits
from gateway.security.limits import RateLimit


def test_full_table_refuses_a_new_key_instead_of_evicting_a_live_bucket():
    limit = RateLimit(capacity=2, period=60, max_keys=3)
    for _ in range(2):
        limit.take("user:victim")
    with pytest.raises(HTTPException):
        limit.take("user:victim")
    limit.take("user:a")
    limit.take("user:b")

    with pytest.raises(HTTPException) as refused:
        limit.take("user:c")

    assert refused.value.status_code == 429
    assert "user:victim" in limit.buckets
    # The victim is still exhausted: no dummy username refilled its budget.
    with pytest.raises(HTTPException) as still_limited:
        limit.take("user:victim")
    assert still_limited.value.status_code == 429


def test_idle_buckets_are_evicted_before_new_keys_are_refused(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock["now"])
    limit = RateLimit(capacity=2, period=10, max_keys=2)
    limit.take("user:old")
    clock["now"] += 5
    limit.take("user:live")
    clock["now"] += 6

    limit.take("user:new")

    assert "user:old" not in limit.buckets
    assert "user:live" in limit.buckets
    assert "user:new" in limit.buckets


def test_same_key_still_spends_its_own_bucket(monkeypatch):
    clock = {"now": 500.0}
    monkeypatch.setattr(limits.time, "monotonic", lambda: clock["now"])
    limit = RateLimit(capacity=1, period=10, max_keys=4)
    limit.take("source")
    with pytest.raises(HTTPException):
        limit.take("source")
    clock["now"] += 10
    limit.take("source")
