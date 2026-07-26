"""Process lifecycle: signal traps and the graceful shutdown sequence.

Process managers ask for a stop with SIGTERM, and a process running as PID 1
gets no default disposition — without a handler it dies without cleanup. The
first signal starts a drain that lets in-flight work finish; a second one
abandons the drain, so an impatient manual restart is never hostage to a
wedged run.

The drain matters more than it looks. ``Updater.stop()`` makes one final
``getUpdates`` with the advanced offset, which confirms to Telegram every
update already fetched into the local queue — those messages will never be
redelivered. Dying between that ack and the handler that processes them loses
them for good, so the shutdown path exists to close that window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .schedule import Scheduler
    from .telegram_bot import TelegramBot

logger = logging.getLogger(__name__)

# How long in-flight work may take before we stop waiting. Must stay under
# whatever grace period the supervisor allows between SIGTERM and SIGKILL, so
# the process exits on its own terms — and can report what it dropped —
# instead of being killed mid-sentence. See the README for the value a
# deployment has to configure.
DRAIN_BUDGET = 270.0

_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Lifecycle:
    """Signal traps for a graceful, then impatient, shutdown."""

    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.force = asyncio.Event()

    def install(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in _SIGNALS:
            # e.g. Windows event loops have no signal support
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal, sig)

    def remove(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in _SIGNALS:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        if self.stop.is_set():
            logger.warning("Second %s — abandoning the drain", sig.name)
            self.force.set()
            return
        logger.info("%s received — draining before shutdown", sig.name)
        self.stop.set()

    async def wait(self) -> None:
        """Block until the first signal arrives."""
        with contextlib.suppress(asyncio.CancelledError):
            await self.stop.wait()


async def _notify(bot: TelegramBot | Any, text: str) -> None:
    """Lifecycle chatter must never be able to wedge the exit."""
    try:
        await bot.notify_lifecycle(text)
    except Exception:
        logger.warning("Could not send lifecycle message %r", text, exc_info=True)


async def graceful_shutdown(
    *,
    bot: TelegramBot | Any,
    scheduler: Scheduler | Any,
    force: asyncio.Event,
    budget: float = DRAIN_BUDGET,
) -> None:
    """Stop accepting work, let what is in flight finish, then tear down.

    Telegram and scheduler drains run concurrently — both are just awaiting
    in-flight agent runs, so serialising them would double the worst case for
    no benefit. Returns once the process is safe to exit, whether the drain
    completed, timed out, or was abandoned by a second signal.
    """
    queued = bot.pending_updates()
    note = f" Finishing {queued} queued message(s) first." if queued else ""
    await _notify(bot, f"Restarting...{note}")

    # The ack point: everything already fetched is confirmed to Telegram here,
    # so from now on the only copy of those messages is the local queue.
    with contextlib.suppress(Exception):
        await bot.stop_polling()

    drain = asyncio.create_task(_drain_all(bot, scheduler, budget))
    abandoned = asyncio.create_task(force.wait())
    try:
        done, _ = await asyncio.wait(
            {drain, abandoned}, timeout=budget, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        abandoned.cancel()

    if drain not in done:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        dropped = bot.pending_updates()
        reason = "abandoned" if force.is_set() else f"deadline ({budget:.0f}s)"
        logger.error(
            "Shutdown drain %s with %d update(s) unprocessed — already acked to "
            "Telegram, so they are lost",
            reason,
            dropped,
        )
        if dropped:
            await _notify(
                bot,
                f"Restart cut short — {dropped} queued message(s) were dropped, "
                "please resend.",
            )

    with contextlib.suppress(Exception):
        await bot.close()


async def _drain_all(bot: TelegramBot | Any, scheduler: Scheduler | Any, budget: float) -> None:
    """Await the Telegram queue and any in-flight scheduled jobs."""
    results = await asyncio.gather(
        bot.drain(), scheduler.drain(timeout=budget), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Drain step failed during shutdown", exc_info=result)
    unfinished = next((r for r in results[1:] if isinstance(r, int)), 0)
    if unfinished:
        logger.warning("%d scheduled job(s) did not finish before shutdown", unfinished)
