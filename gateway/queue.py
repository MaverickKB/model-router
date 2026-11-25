"""Per-engine admission wait queue.

When every candidate engine is at its concurrency limit, a caller may wait in
line instead of receiving an immediate 503. ``EngineQueue`` orders waiters chat
before background, reports a live position, bounds the wait, and removes a
waiter on timeout or cancellation so a slot is never leaked.
"""

import asyncio
import time


class QueueTicket:
    """One line position for a single request.

    The ticket is already enqueued in ticket order when returned. ``wait``
    blocks until a slot frees or the timeout lapses; ``position`` reports how
    many higher-or-equal priority waiters stand ahead.
    """

    def __init__(self, queue: "EngineQueue", background: bool):
        self._queue = queue
        self.background = background
        self._event = asyncio.Event()
        self._enqueued_at = time.monotonic()

    def position(self) -> int:
        ahead = 0
        for waiter in self._queue._waiters:
            if waiter is self:
                break
            # A chat waiter sees background waiters as behind it; a background
            # waiter counts every earlier waiter.
            if not self.background or not waiter.background:
                ahead += 1
        return ahead

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._queue._remove(self)


class EngineQueue:
    """FIFO waiters with chat priority, coordinated on one event loop."""

    def __init__(self):
        self._waiters: list[QueueTicket] = []

    def ticket(self, *, background: bool) -> QueueTicket:
        waiter = QueueTicket(self, background)
        if background:
            self._waiters.append(waiter)
        else:
            index = 0
            while index < len(self._waiters) and not self._waiters[index].background:
                index += 1
            self._waiters.insert(index, waiter)
        return waiter

    def notify(self) -> None:
        # Wake the head so one freed slot admits one waiter; the woken waiter
        # re-runs its own claim, and a further release wakes the next.
        if self._waiters:
            self._waiters[0]._event.set()

    def _remove(self, waiter: QueueTicket) -> None:
        if waiter in self._waiters:
            self._waiters.remove(waiter)

    def __len__(self) -> int:
        return len(self._waiters)
