"""Process lifecycle: signal traps and the graceful shutdown sequence.

Process managers ask for a stop with SIGTERM, and a process running as PID 1
gets no default disposition — without a handler it dies without cleanup. The
first signal starts a drain that lets in-flight work finish; a second one
abandons the drain, so an impatient manual restart is never hostage to a
wedged run.

The drain lets web messages already accepted finish their run and lets a
mid-run scheduled job deliver its reminder. A message cut short is marked
interrupted and can be retried from the app; a job's work may have partially
completed, and its recurring row keeps a past ``next`` so the next start
retries it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .companion import Companion
    from .schedule import Scheduler

logger = logging.getLogger(__name__)

# How long in-flight work may take before we stop waiting. Must stay under
# whatever grace period the supervisor allows between SIGTERM and SIGKILL, so
# the process exits on its own terms — and can report what it dropped —
# instead of being killed mid-sentence. See docs/deployment.md for the value
# a deployment has to configure.
DRAIN_BUDGET = 270.0
# How long an important lifecycle push may hold the exit after a failed drain.
_IMPORTANT_PUSH_WAIT = 15.0

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


async def _notify(companion: Companion | Any, text: str, *, important: bool = False) -> None:
    """Lifecycle chatter must never be able to wedge the exit.

    The companion pushes the text to devices that asked for restart notices;
    an important one (work cut short) goes to every device and is waited for
    briefly, since the drain it follows has already been given up.
    """
    try:
        task = companion.notify_lifecycle(text, important=important)
        if important and task is not None:
            await asyncio.wait_for(asyncio.shield(task), _IMPORTANT_PUSH_WAIT)
    except Exception:
        logger.warning("Could not push lifecycle message %r", text, exc_info=True)


async def graceful_shutdown(
    *,
    companion: Companion | Any,
    scheduler: Scheduler | Any,
    force: asyncio.Event,
    budget: float = DRAIN_BUDGET,
) -> None:
    """Stop accepting work, let what is in flight finish, then return.

    The web and scheduler drains run concurrently — both are just awaiting
    in-flight agent runs, so serialising them would double the worst case for
    no benefit. Returns once the process is safe to exit, whether the drain
    completed, timed out, or was abandoned by a second signal.
    """
    companion.accepting = False
    running = companion.pending()
    note = f" Finishing {running} message(s) in progress first." if running else ""
    await _notify(companion, f"Restarting...{note}")

    drain = asyncio.create_task(_drain_all(companion, scheduler, budget))
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
        cut_short = companion.pending()
        reason = "abandoned" if force.is_set() else f"deadline ({budget:.0f}s)"
        logger.error("Shutdown drain %s with %d message(s) still running", reason, cut_short)
        if cut_short:
            await _notify(
                companion,
                f"Restart cut short — {cut_short} message(s) in progress were interrupted; "
                "retry them from the app.",
                important=True,
            )


async def _drain_all(companion: Companion | Any, scheduler: Scheduler | Any, budget: float) -> None:
    """Await in-flight web runs, their pushes, and any in-flight scheduled jobs."""
    results = await asyncio.gather(
        companion.drain(), scheduler.drain(timeout=budget), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Drain step failed during shutdown", exc_info=result)
    unfinished = results[1] if isinstance(results[1], int) else 0
    if unfinished:
        logger.warning("%d scheduled job(s) did not finish before shutdown", unfinished)
