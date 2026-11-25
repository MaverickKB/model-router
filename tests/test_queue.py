"""Per-engine admission queue: ordering, wake, timeout, and cancellation."""

import asyncio

import pytest

from gateway.queue import EngineQueue


def test_chat_orders_ahead_of_background():
    queue = EngineQueue()
    first = queue.ticket(background=False)
    second = queue.ticket(background=True)
    third = queue.ticket(background=False)
    assert first.position() == 0
    assert third.position() == 1
    assert second.position() == 2


def test_position_counts_only_waiters_ahead():
    queue = EngineQueue()
    a = queue.ticket(background=False)
    b = queue.ticket(background=False)
    c = queue.ticket(background=True)
    assert a.position() == 0
    assert b.position() == 1
    assert c.position() == 2


async def test_notify_wakes_head_and_wait_removes_ticket():
    queue = EngineQueue()
    ticket = queue.ticket(background=False)
    assert len(queue) == 1

    outcomes = []

    async def waiter():
        outcomes.append(await ticket.wait(timeout=1))

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    queue.notify()
    await asyncio.wait_for(task, timeout=1)
    assert outcomes == [True]
    assert len(queue) == 0


async def test_timeout_returns_false_and_removes_ticket():
    queue = EngineQueue()
    ticket = queue.ticket(background=False)
    result = await ticket.wait(timeout=0.01)
    assert result is False
    assert len(queue) == 0


async def test_cancellation_removes_ticket_without_leaking():
    queue = EngineQueue()
    ticket = queue.ticket(background=False)
    task = asyncio.create_task(ticket.wait(timeout=5))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(queue) == 0
