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


def _bot(pending: int = 0) -> MagicMock:
    bot = MagicMock()
    bot.pending_updates = MagicMock(return_value=pending)
    bot.notify_lifecycle = AsyncMock()
    bot.stop_polling = AsyncMock()
    bot.drain = AsyncMock()
    bot.close = AsyncMock()
    return bot


async def _never_finishes() -> None:
    await asyncio.sleep(30)


def _scheduler(unfinished: int = 0) -> MagicMock:
    sched = MagicMock()
    sched.drain = AsyncMock(return_value=unfinished)
    return sched


async def test_shutdown_stops_fetching_before_draining() -> None:
    """updater.stop() is the ack point; it must precede the drain."""
    order: list[str] = []
    bot = _bot()
    bot.stop_polling = AsyncMock(side_effect=lambda: order.append("stop_polling"))
    bot.drain = AsyncMock(side_effect=lambda: order.append("drain"))
    bot.close = AsyncMock(side_effect=lambda: order.append("close"))

    await graceful_shutdown(bot=bot, scheduler=_scheduler(), force=asyncio.Event())

    assert order == ["stop_polling", "drain", "close"]


async def test_shutdown_drains_scheduler_and_bot_together() -> None:
    bot = _bot()
    sched = _scheduler()

    await graceful_shutdown(bot=bot, scheduler=sched, force=asyncio.Event())

    bot.drain.assert_awaited_once()
    sched.drain.assert_awaited_once()


async def test_shutdown_reports_queue_depth_in_the_notice() -> None:
    bot = _bot(pending=3)

    await graceful_shutdown(bot=bot, scheduler=_scheduler(), force=asyncio.Event())

    text = bot.notify_lifecycle.await_args_list[0].args[0]
    assert "3" in text


async def test_shutdown_notice_is_plain_when_nothing_is_queued() -> None:
    bot = _bot(pending=0)

    await graceful_shutdown(bot=bot, scheduler=_scheduler(), force=asyncio.Event())

    text = bot.notify_lifecycle.await_args_list[0].args[0]
    assert "queued" not in text


async def test_shutdown_gives_up_at_the_deadline_and_still_closes() -> None:
    bot = _bot()
    bot.drain = AsyncMock(side_effect=_never_finishes)

    await graceful_shutdown(
        bot=bot, scheduler=_scheduler(), force=asyncio.Event(), budget=0.05
    )

    bot.close.assert_awaited_once()


async def test_shutdown_reports_dropped_messages_when_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Acked-but-undrained updates are gone for good — say so, loudly."""
    bot = _bot(pending=2)
    bot.drain = AsyncMock(side_effect=_never_finishes)

    with caplog.at_level(logging.ERROR, logger="assistant.lifecycle"):
        await graceful_shutdown(
            bot=bot, scheduler=_scheduler(), force=asyncio.Event(), budget=0.05
        )

    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    last = bot.notify_lifecycle.await_args_list[-1].args[0]
    assert "2" in last and "resend" in last.lower()


async def test_second_signal_short_circuits_the_drain() -> None:
    bot = _bot()
    bot.drain = AsyncMock(side_effect=_never_finishes)
    force = asyncio.Event()

    async def press_again() -> None:
        await asyncio.sleep(0.01)
        force.set()

    asyncio.create_task(press_again())
    await asyncio.wait_for(
        graceful_shutdown(bot=bot, scheduler=_scheduler(), force=force, budget=30),
        timeout=5,
    )

    bot.close.assert_awaited_once()


async def test_notify_failure_does_not_block_shutdown() -> None:
    bot = _bot()
    bot.notify_lifecycle = AsyncMock(side_effect=Exception("network down"))

    await graceful_shutdown(bot=bot, scheduler=_scheduler(), force=asyncio.Event())

    bot.close.assert_awaited_once()


async def test_drain_failure_does_not_block_close() -> None:
    bot = _bot()
    bot.drain = AsyncMock(side_effect=RuntimeError("application not running"))

    await graceful_shutdown(bot=bot, scheduler=_scheduler(), force=asyncio.Event())

    bot.close.assert_awaited_once()
