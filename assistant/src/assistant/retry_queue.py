"""Durable retry queue for work that failed while Copilot was unreachable.

When a user message or a one-off scheduled job fails with a
``CopilotUnavailableError`` (5xx or network failure after retries), it is
queued here instead of being lost. A background drain loop retries the head
of the queue with exponential backoff — the retry itself is the health probe,
so there is no separate health check that could pass while chat still fails.

The queue is persisted to ``state_dir/pending_runs.jsonl`` and reloaded at
startup, so a restart mid-outage loses nothing. An item is removed only after
its replay fully succeeds: a crash mid-replay reprocesses (the replay prompts
warn the model the work may have been partially done) rather than loses.

Items enqueued in this process are *hot* — the failed turn still sits in the
conversation's in-memory history, so replay can resume it in place. Items
loaded from disk are *cold* — history is gone and the original text must be
replayed from scratch. The distinction is passed to the message replay
callback; how to act on it is the agent's business (see Agent.retry_message).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .atomic import atomic_write_text
from .copilot import CopilotUnavailableError

logger = logging.getLogger(__name__)

QUEUE_FILENAME = "pending_runs.jsonl"

_BACKOFF_INITIAL = 30.0
_BACKOFF_MAX = 300.0

# Replays a queued user message: (chat_id, thread_id, text, queued_at, hot).
# Raises CopilotUnavailableError while the outage lasts.
ReplayMessageFn = Callable[..., Awaitable[None]]
# Replays a queued one-off job prompt (already carrying its catch-up prefix).
ReplayJobFn = Callable[[str], Awaitable[None]]
# Notifies about an item dropped after a non-outage replay failure.
NotifyDropFn = Callable[["PendingItem", Exception], Awaitable[None]]


def _next_backoff(delay: float) -> float:
    return min(delay * 2, _BACKOFF_MAX)


@dataclass(frozen=True)
class PendingItem:
    kind: str  # "message" | "job"
    text: str  # message text, or the job's raw prompt
    queued_at: str  # local stamp, e.g. "2026-08-17 15:08 local"
    chat_id: int | None = None
    thread_id: int | None = None
    # In-memory only, never serialized: items reloaded from disk are cold.
    hot: bool = False

    def to_json(self) -> str:
        record = {"kind": self.kind, "text": self.text, "queued_at": self.queued_at}
        if self.kind == "message":
            record["chat_id"] = self.chat_id
            record["thread_id"] = self.thread_id
        return json.dumps(record, ensure_ascii=False)


def _parse_item(line: str) -> PendingItem | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    kind, text, queued_at = record.get("kind"), record.get("text"), record.get("queued_at")
    if not isinstance(text, str) or not isinstance(queued_at, str):
        return None
    if kind == "job":
        return PendingItem(kind="job", text=text, queued_at=queued_at)
    if kind == "message" and isinstance(record.get("chat_id"), int):
        thread_id = record.get("thread_id")
        if thread_id is None or isinstance(thread_id, int):
            return PendingItem(
                kind="message",
                text=text,
                queued_at=queued_at,
                chat_id=record["chat_id"],
                thread_id=thread_id,
            )
    return None


class RetryQueue:
    """FIFO of outage-failed work, drained with backoff once Copilot answers."""

    def __init__(
        self,
        state_dir: Path,
        replay_message_fn: ReplayMessageFn,
        replay_job_fn: ReplayJobFn,
        notify_drop_fn: NotifyDropFn | None = None,
        tz_name: str = "UTC",
    ) -> None:
        self._path = state_dir / QUEUE_FILENAME
        self._replay_message_fn = replay_message_fn
        self._replay_job_fn = replay_job_fn
        self._notify_drop_fn = notify_drop_fn
        self._tz = ZoneInfo(tz_name)
        self._items: deque[PendingItem] = deque()
        self._wake = asyncio.Event()
        self._load()

    def pending(self) -> int:
        return len(self._items)

    def _local_stamp(self) -> str:
        return datetime.now(tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M local")

    def enqueue_message(self, chat_id: int, thread_id: int | None, text: str) -> None:
        item = PendingItem(
            kind="message",
            text=text,
            queued_at=self._local_stamp(),
            chat_id=chat_id,
            thread_id=thread_id,
            hot=True,
        )
        self._append(item)
        logger.info(
            "Queued message for retry (chat_id=%d thread_id=%s, %d pending)",
            chat_id, thread_id, len(self._items),
        )

    def enqueue_job(self, prompt: str) -> None:
        item = PendingItem(kind="job", text=prompt, queued_at=self._local_stamp(), hot=True)
        self._append(item)
        logger.info("Queued one-off job for retry (%d pending): %.80r", len(self._items), prompt)

    async def run(self) -> None:
        """Drain loop: retry the head item, backing off while Copilot is down."""
        delay = _BACKOFF_INITIAL
        while True:
            await self._wake.wait()
            if not self._items:
                self._wake.clear()
                continue
            item = self._items[0]
            try:
                await self._replay(item)
            except CopilotUnavailableError as exc:
                logger.warning(
                    "Copilot still unavailable (%s); %d queued, retrying in %.0fs",
                    exc, len(self._items), delay,
                )
                await asyncio.sleep(delay)
                delay = _next_backoff(delay)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A poison item must not wedge the queue behind it.
                logger.exception("Dropping queued %s after non-outage replay failure", item.kind)
                self._pop_head()
                if self._notify_drop_fn is not None:
                    try:
                        await self._notify_drop_fn(item, exc)
                    except Exception:
                        logger.warning("notify_drop_fn failed", exc_info=True)
                continue
            delay = _BACKOFF_INITIAL
            self._pop_head()
            logger.info("Replayed queued %s from %s (%d left)", item.kind, item.queued_at, len(self._items))

    async def _replay(self, item: PendingItem) -> None:
        if item.kind == "message":
            await self._replay_message_fn(
                chat_id=item.chat_id,
                thread_id=item.thread_id,
                text=item.text,
                queued_at=item.queued_at,
                hot=item.hot,
            )
        else:
            # queued_at is when the run failed, not the row's due time — the
            # row may already have fired late under the misfire grace.
            await self._replay_job_fn(
                f"[catch-up: failed at {item.queued_at} during a Copilot outage] {item.text}"
            )

    # ------------------------------------------------------------------
    # Persistence: whole-file atomic rewrite, items are few and small
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            if not line.strip():
                continue
            item = _parse_item(line)
            if item is None:
                logger.warning("Skipping malformed retry-queue line: %.200r", line)
                continue
            self._items.append(item)
        if self._items:
            logger.info("Loaded %d pending item(s) from %s", len(self._items), self._path.name)
            self._wake.set()

    def _append(self, item: PendingItem) -> None:
        self._items.append(item)
        self._persist()
        self._wake.set()

    def _pop_head(self) -> None:
        self._items.popleft()
        self._persist()

    def _persist(self) -> None:
        # Never raises: a disk error mid-enqueue would rob the user of any
        # reply, and mid-drain it would kill the drain task. The file is
        # rewritten from memory every time, so the next successful persist
        # heals; until then the queue lives in memory only.
        try:
            atomic_write_text(
                self._path, "".join(item.to_json() + "\n" for item in self._items)
            )
        except OSError:
            logger.exception("Could not persist retry queue to %s", self._path)
