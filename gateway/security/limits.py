"""Bounded token buckets for authentication and registration attempts."""

import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class RateLimit:
    def __init__(self, capacity=5, period=60, max_keys=2048):
        self.capacity, self.period, self.max_keys = capacity, period, max_keys
        self.buckets: OrderedDict[str, Bucket] = OrderedDict()

    def reset(self, key: str):
        self.buckets.pop(key, None)

    def _evict_idle(self, now: float):
        """Drop buckets oldest first, stopping at the first one still in use."""
        idle_before = now - self.period
        for existing in list(self.buckets.keys()):
            if self.buckets[existing].updated_at > idle_before:
                break
            self.buckets.pop(existing)

    def take(self, key: str):
        now = time.monotonic()
        bucket = self.buckets.pop(key, Bucket(self.capacity, now))
        bucket.tokens = min(
            self.capacity,
            bucket.tokens + (now - bucket.updated_at) * self.capacity / self.period,
        )
        bucket.updated_at = now
        if len(self.buckets) >= self.max_keys:
            # Only a bucket that has been idle for a full period can be dropped:
            # it is indistinguishable from a fresh one. Evicting the oldest live
            # bucket would let many dummy usernames clear a real victim's budget.
            self._evict_idle(now)
            if len(self.buckets) >= self.max_keys:
                raise HTTPException(
                    429,
                    "Too many attempts; retry later",
                    headers={"Retry-After": str(self.period)},
                )
        self.buckets[key] = bucket
        if bucket.tokens < 1:
            raise HTTPException(
                429,
                "Too many attempts; retry later",
                headers={"Retry-After": str(self.period)},
            )
        bucket.tokens -= 1
