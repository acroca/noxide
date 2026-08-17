"""Tests for the Copilot-outage retry queue."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant import retry_queue
from assistant.copilot import CopilotUnavailableError
from assistant.retry_queue import QUEUE_FILENAME, RetryQueue


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry_queue, "_BACKOFF_INITIAL", 0.005)
    monkeypatch.setattr(retry_queue, "_BACKOFF_MAX", 0.02)


def _queue(
    state_dir: Path,
    replay_message_fn: AsyncMock | None = None,
    replay_job_fn: AsyncMock | None = None,
    notify_drop_fn: AsyncMock | None = None,
) -> RetryQueue:
    return RetryQueue(
        state_dir,
        replay_message_fn=replay_message_fn or AsyncMock(),
        replay_job_fn=replay_job_fn or AsyncMock(),
        notify_drop_fn=notify_drop_fn,
        tz_name="UTC",
    )


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------

def test_enqueue_message_persists_jsonl(state_dir: Path) -> None:
    q = _queue(state_dir)
    q.enqueue_message(5, 7, "hello there")

    lines = (state_dir / QUEUE_FILENAME).read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "message"
    assert record["chat_id"] == 5
    assert record["thread_id"] == 7
    assert record["text"] == "hello there"
    assert record["queued_at"]


def test_enqueue_job_persists_jsonl(state_dir: Path) -> None:
    q = _queue(state_dir)
    q.enqueue_job("Remind the user to stretch")

    record = json.loads((state_dir / QUEUE_FILENAME).read_text().splitlines()[0])
    assert record["kind"] == "job"
    assert record["text"] == "Remind the user to stretch"


def test_load_skips_malformed_lines(state_dir: Path) -> None:
    (state_dir / QUEUE_FILENAME).write_text(
        'not json at all\n'
        '{"kind": "job", "text": "valid", "queued_at": "2026-08-17 10:00 local"}\n'
        '{"kind": "bogus", "text": "x", "queued_at": "y"}\n'
    )
    q = _queue(state_dir)
    assert q.pending() == 1


# ------------------------------------------------------------------
# Drain behavior
# ------------------------------------------------------------------

async def test_live_message_replays_hot_and_clears_file(state_dir: Path) -> None:
    replay = AsyncMock()
    q = _queue(state_dir, replay_message_fn=replay)
    task = asyncio.create_task(q.run())
    try:
        q.enqueue_message(5, None, "hello")
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    replay.assert_awaited_once()
    kwargs = replay.await_args.kwargs
    assert kwargs["chat_id"] == 5
    assert kwargs["thread_id"] is None
    assert kwargs["text"] == "hello"
    assert kwargs["hot"] is True
    assert (state_dir / QUEUE_FILENAME).read_text() == ""


async def test_reloaded_message_replays_cold(state_dir: Path) -> None:
    _queue(state_dir).enqueue_message(5, 7, "from before the restart")

    replay = AsyncMock()
    q = _queue(state_dir, replay_message_fn=replay)
    task = asyncio.create_task(q.run())
    try:
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    assert replay.await_args.kwargs["hot"] is False


async def test_job_replay_carries_catchup_provenance(state_dir: Path) -> None:
    replay_job = AsyncMock()
    q = _queue(state_dir, replay_job_fn=replay_job)
    task = asyncio.create_task(q.run())
    try:
        q.enqueue_job("Remind the user about the dentist")
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    (prompt,) = replay_job.await_args.args
    # queued_at is when the run failed, not the row's due time — the row may
    # have fired late already, so "was due" would be wrong.
    assert prompt.startswith("[catch-up: failed at ")
    assert "Copilot outage" in prompt
    assert prompt.endswith("Remind the user about the dentist")


async def test_items_drain_in_fifo_order_across_kinds(state_dir: Path) -> None:
    order: list[str] = []

    async def replay_message(**kwargs) -> None:
        order.append(kwargs["text"])

    async def replay_job(prompt: str) -> None:
        order.append(prompt)

    q = RetryQueue(
        state_dir,
        replay_message_fn=replay_message,
        replay_job_fn=replay_job,
        notify_drop_fn=None,
        tz_name="UTC",
    )
    q.enqueue_message(5, None, "first")
    q.enqueue_job("second")
    q.enqueue_message(5, None, "third")

    task = asyncio.create_task(q.run())
    try:
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    assert order[0] == "first"
    assert "second" in order[1]
    assert order[2] == "third"


async def test_outage_during_replay_keeps_item_and_retries(state_dir: Path) -> None:
    replay = AsyncMock(side_effect=[CopilotUnavailableError("502"), CopilotUnavailableError("502"), None])
    q = _queue(state_dir, replay_message_fn=replay)
    task = asyncio.create_task(q.run())
    try:
        q.enqueue_message(5, None, "hello")
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    assert replay.await_count == 3
    assert (state_dir / QUEUE_FILENAME).read_text() == ""


async def test_non_outage_replay_failure_drops_item_and_notifies(state_dir: Path) -> None:
    replay = AsyncMock(side_effect=[ValueError("boom"), None])
    notify = AsyncMock()
    q = _queue(state_dir, replay_message_fn=replay, notify_drop_fn=notify)
    task = asyncio.create_task(q.run())
    try:
        q.enqueue_message(5, None, "poison")
        q.enqueue_message(5, None, "fine")
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    # The poison item was dropped, the next one still processed.
    assert replay.await_count == 2
    notify.assert_awaited_once()
    dropped_item = notify.await_args.args[0]
    assert dropped_item.text == "poison"


async def test_cancel_mid_replay_leaves_item_on_disk(state_dir: Path) -> None:
    started = asyncio.Event()

    async def hang(**kwargs) -> None:
        started.set()
        await asyncio.sleep(60)

    q = RetryQueue(
        state_dir,
        replay_message_fn=hang,
        replay_job_fn=AsyncMock(),
        notify_drop_fn=None,
        tz_name="UTC",
    )
    q.enqueue_message(5, None, "in flight at shutdown")
    task = asyncio.create_task(q.run())
    await started.wait()
    task.cancel()

    assert "in flight at shutdown" in (state_dir / QUEUE_FILENAME).read_text()


def test_backoff_doubles_to_cap() -> None:
    assert retry_queue._next_backoff(0.005) == 0.01
    assert retry_queue._next_backoff(1000.0) == retry_queue._BACKOFF_MAX


# ------------------------------------------------------------------
# Persistence failures: memory state must survive, the drain must not die
# ------------------------------------------------------------------

def test_enqueue_survives_persist_failure(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk error while persisting must not escape into the Telegram error
    handler (the user would get no reply at all) nor drop the in-memory item —
    the file is rewritten from memory on the next successful persist."""
    q = _queue(state_dir)
    monkeypatch.setattr(
        retry_queue.os, "replace", MagicMock(side_effect=OSError("disk full"))
    )

    q.enqueue_message(5, None, "hello")

    assert q.pending() == 1


async def test_drain_survives_persist_failure(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay = AsyncMock()
    q = _queue(state_dir, replay_message_fn=replay)
    q.enqueue_message(5, None, "first")
    q.enqueue_message(5, None, "second")
    monkeypatch.setattr(
        retry_queue.os, "replace", MagicMock(side_effect=OSError("disk full"))
    )

    task = asyncio.create_task(q.run())
    try:
        await _wait_until(lambda: q.pending() == 0)
    finally:
        task.cancel()

    assert replay.await_count == 2
