"""Signal traps and the graceful shutdown sequence."""

from __future__ import annotations

import asyncio
import logging
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.lifecycle import Lifecycle, graceful_shutdown

# ------------------------------------------------------------------
# Signal handling
# ------------------------------------------------------------------


async def test_first_signal_sets_stop_not_force() -> None:
    lc = Lifecycle()
    lc.install()
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(lc.stop.wait(), timeout=1)
    finally:
        lc.remove()

    assert not lc.force.is_set()


async def test_second_signal_sets_force() -> None:
    lc = Lifecycle()
    lc.install()
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(lc.stop.wait(), timeout=1)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(lc.force.wait(), timeout=1)
    finally:
        lc.remove()


async def test_sigint_takes_the_same_path_as_sigterm() -> None:
    """Ctrl-C in local dev must exercise the drain, not a separate route."""
    lc = Lifecycle()
    lc.install()
    try:
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(lc.stop.wait(), timeout=1)
    finally:
        lc.remove()


async def test_remove_restores_default_handlers() -> None:
    lc = Lifecycle()
    lc.install()
    lc.remove()

    # Re-installing must not raise — the handlers were genuinely detached
    lc2 = Lifecycle()
    lc2.install()
    lc2.remove()


# ------------------------------------------------------------------
# Shutdown sequence
# ------------------------------------------------------------------


def _companion(pending: int = 0) -> MagicMock:
    companion = MagicMock()
    companion.accepting = True
    companion.pending = MagicMock(return_value=pending)
    companion.notify_lifecycle = MagicMock(return_value=None)
    companion.drain = AsyncMock()
    return companion


async def _never_finishes() -> None:
    await asyncio.sleep(30)


def _scheduler(unfinished: int = 0) -> MagicMock:
    sched = MagicMock()
    sched.drain = AsyncMock(return_value=unfinished)
    return sched


async def test_shutdown_stops_accepting_then_drains_web_and_scheduler_together() -> None:
    companion = _companion()
    sched = _scheduler()

    async def drain() -> None:
        assert companion.accepting is False

    companion.drain = AsyncMock(side_effect=drain)
    await graceful_shutdown(companion=companion, scheduler=sched, force=asyncio.Event())

    companion.drain.assert_awaited_once()
    sched.drain.assert_awaited_once()
    assert companion.accepting is False


async def test_shutdown_reports_running_messages_in_the_notice() -> None:
    companion = _companion(pending=3)

    await graceful_shutdown(companion=companion, scheduler=_scheduler(), force=asyncio.Event())

    text, kwargs = companion.notify_lifecycle.call_args_list[0].args[0], companion.notify_lifecycle.call_args_list[0].kwargs
    assert text.startswith("Restarting") and "3" in text and kwargs == {"important": False}


async def test_shutdown_notice_is_plain_when_nothing_is_running() -> None:
    companion = _companion(pending=0)

    await graceful_shutdown(companion=companion, scheduler=_scheduler(), force=asyncio.Event())

    text = companion.notify_lifecycle.call_args_list[0].args[0]
    assert "message" not in text
    companion.notify_lifecycle.assert_called_once()


async def test_shutdown_gives_up_at_the_deadline_and_returns() -> None:
    companion = _companion()
    companion.drain = AsyncMock(side_effect=_never_finishes)

    await asyncio.wait_for(
        graceful_shutdown(companion=companion, scheduler=_scheduler(), force=asyncio.Event(), budget=0.05),
        timeout=5,
    )


async def test_interrupted_messages_push_to_every_device_and_are_waited_for(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The important push runs after the drain was given up, so nothing else awaits it."""
    delivered = asyncio.Event()

    async def push() -> None:
        await asyncio.sleep(0.01)
        delivered.set()

    companion = _companion(pending=2)
    companion.notify_lifecycle = MagicMock(
        side_effect=lambda text, important: asyncio.create_task(push()) if important else None)
    companion.drain = AsyncMock(side_effect=_never_finishes)

    with caplog.at_level(logging.ERROR, logger="assistant.lifecycle"):
        await graceful_shutdown(companion=companion, scheduler=_scheduler(), force=asyncio.Event(), budget=0.05)

    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    assert delivered.is_set()
    last = companion.notify_lifecycle.call_args_list[-1]
    assert "2" in last.args[0] and "retry" in last.args[0].lower()
    assert last.kwargs == {"important": True}


async def test_second_signal_short_circuits_the_drain() -> None:
    companion = _companion()
    companion.drain = AsyncMock(side_effect=_never_finishes)
    force = asyncio.Event()

    async def press_again() -> None:
        await asyncio.sleep(0.01)
        force.set()

    asyncio.create_task(press_again())
    await asyncio.wait_for(
        graceful_shutdown(companion=companion, scheduler=_scheduler(), force=force, budget=30),
        timeout=5,
    )


async def test_push_failure_does_not_block_shutdown() -> None:
    companion = _companion()
    companion.notify_lifecycle = MagicMock(side_effect=RuntimeError("no key"))
    sched = _scheduler()

    await graceful_shutdown(companion=companion, scheduler=sched, force=asyncio.Event())

    companion.drain.assert_awaited_once()
    sched.drain.assert_awaited_once()


async def test_drain_failure_does_not_block_the_other_drain(caplog: pytest.LogCaptureFixture) -> None:
    companion = _companion()
    companion.drain = AsyncMock(side_effect=RuntimeError("server not running"))
    sched = _scheduler(unfinished=1)

    with caplog.at_level(logging.WARNING, logger="assistant.lifecycle"):
        await graceful_shutdown(companion=companion, scheduler=sched, force=asyncio.Event())

    sched.drain.assert_awaited_once()
    assert "Drain step failed" in caplog.text
    assert "1 scheduled job(s) did not finish" in caplog.text
