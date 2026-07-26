"""Tests for token usage tracking: JSONL store, vault view rendering, background drain."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant import usage
from assistant.usage import UsageTracker

_TZ = "Europe/Madrid"


@pytest.fixture
def tracker(tmp_path: Path) -> UsageTracker:
    return UsageTracker(tmp_path / "state", tmp_path / "vault", _TZ)


def _freeze(tracker: UsageTracker, monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    """Pin the tracker's clock to a fixed local datetime."""
    frozen = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(_TZ))
    monkeypatch.setattr(tracker, "_now", lambda: frozen)


# ------------------------------------------------------------------
# record + drain → JSONL append
# ------------------------------------------------------------------

async def test_drain_appends_one_line_per_event_to_monthly_file(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T14:05:12")
    tracker.record(
        "agent", "claude-sonnet-4.6",
        {
            "prompt_tokens": 8123,
            "completion_tokens": 402,
            "prompt_tokens_details": {"cached_tokens": 7000},
        },
        chat_id=12345, thread_id=7,
    )
    tracker.record("research", "claude-sonnet-4.6", {"prompt_tokens": 900, "completion_tokens": 80})

    await tracker.drain()

    path = tmp_path / "state" / "usage" / "usage-2026-07.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "ts": "2026-07-23T14:05:12+02:00",
        "feature": "agent",
        "model": "claude-sonnet-4.6",
        "chat_id": 12345,
        "thread_id": 7,
        "prompt_tokens": 8123,
        "cached_tokens": 7000,
        "completion_tokens": 402,
    }
    second = json.loads(lines[1])
    assert second["chat_id"] is None
    assert second["thread_id"] is None
    assert second["cached_tokens"] == 0


async def test_drain_appends_to_existing_file(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T10:00:00")
    tracker.record("agent", "m", {"prompt_tokens": 1, "completion_tokens": 1})
    await tracker.drain()
    tracker.record("agent", "m", {"prompt_tokens": 2, "completion_tokens": 2})
    await tracker.drain()

    path = tmp_path / "state" / "usage" / "usage-2026-07.jsonl"
    assert len(path.read_text().splitlines()) == 2


async def test_missing_usage_records_zeros(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T10:00:00")
    tracker.record("vision", "gpt-4o", {})
    await tracker.drain()

    path = tmp_path / "state" / "usage" / "usage-2026-07.jsonl"
    event = json.loads(path.read_text().splitlines()[0])
    assert event["prompt_tokens"] == 0
    assert event["cached_tokens"] == 0
    assert event["completion_tokens"] == 0


async def test_drain_with_empty_queue_writes_nothing(
    tracker: UsageTracker, tmp_path: Path
) -> None:
    await tracker.drain()
    assert not (tmp_path / "state" / "usage").exists()


# ------------------------------------------------------------------
# module-level singleton
# ------------------------------------------------------------------

def test_module_record_is_noop_when_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage, "_tracker", None)
    usage.record("agent", "m", {"prompt_tokens": 1, "completion_tokens": 1})  # must not raise


def test_module_record_delegates_to_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = usage.init(tmp_path / "state", tmp_path / "vault", _TZ)
    assert usage.get_tracker() is tracker
    usage.record("agent", "m", {"prompt_tokens": 5, "completion_tokens": 3}, chat_id=1)
    assert tracker._queue.qsize() == 1
    monkeypatch.setattr(usage, "_tracker", None)  # don't leak into other tests


# ------------------------------------------------------------------
# vault view rendering
# ------------------------------------------------------------------

def _write_events(state_dir: Path, month: str, events: list[dict]) -> None:
    usage_dir = state_dir / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    with (usage_dir / f"usage-{month}.jsonl").open("a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _event(ts: str, feature: str = "agent", model: str = "m1",
           prompt: int = 100, completion: int = 10, cached: int = 0) -> dict:
    return {
        "ts": ts, "feature": feature, "model": model,
        "chat_id": 1, "thread_id": None,
        "prompt_tokens": prompt, "cached_tokens": cached,
        "completion_tokens": completion,
    }


def test_render_aggregates_by_day_feature_model(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T18:00:00")
    _write_events(tmp_path / "state", "2026-07", [
        _event("2026-07-23T10:00:00+02:00", "agent", "m1", 1000, 100, cached=800),
        _event("2026-07-23T11:00:00+02:00", "agent", "m1", 500, 50, cached=300),
        _event("2026-07-23T12:00:00+02:00", "research", "m1", 200, 20),
        _event("2026-07-22T09:00:00+02:00", "agent", "m2", 9000, 900),
    ])

    tracker._render()

    view = (tmp_path / "vault" / "system" / "usage.md").read_text()
    assert view == (
        "# Usage (last 7 days)\n"
        "\n"
        "| day | feature | model | requests | tokens in | cached | tokens out |\n"
        "|-----|---------|-------|----------|-----------|--------|------------|\n"
        "| 2026-07-23 | agent | m1 | 2 | 1,500 | 1,100 | 150 |\n"
        "| 2026-07-23 | research | m1 | 1 | 200 | 0 | 20 |\n"
        "| 2026-07-22 | agent | m2 | 1 | 9,000 | 0 | 900 |\n"
    )


def test_render_treats_missing_cached_tokens_as_zero(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSONL written before the cached column existed must still render."""
    _freeze(tracker, monkeypatch, "2026-07-23T18:00:00")
    legacy = _event("2026-07-23T10:00:00+02:00")
    del legacy["cached_tokens"]
    _write_events(tmp_path / "state", "2026-07", [legacy])

    tracker._render()

    view = (tmp_path / "vault" / "system" / "usage.md").read_text()
    assert "| 2026-07-23 | agent | m1 | 1 | 100 | 0 | 10 |" in view


def test_render_truncates_to_last_7_days_across_month_boundary(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-08-03T12:00:00")
    _write_events(tmp_path / "state", "2026-07", [
        _event("2026-07-25T10:00:00+02:00"),   # older than 7 days — dropped
        _event("2026-07-30T10:00:00+02:00"),   # within window — kept
    ])
    _write_events(tmp_path / "state", "2026-08", [
        _event("2026-08-02T10:00:00+02:00"),
    ])

    tracker._render()

    view = (tmp_path / "vault" / "system" / "usage.md").read_text()
    assert "2026-07-25" not in view
    assert "| 2026-08-02 | agent | m1 | 1 | 100 | 0 | 10 |" in view
    assert "| 2026-07-30 | agent | m1 | 1 | 100 | 0 | 10 |" in view


def test_render_skips_malformed_lines(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T18:00:00")
    usage_dir = tmp_path / "state" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "usage-2026-07.jsonl").write_text(
        "not json at all\n"
        + json.dumps({"ts": "2026-07-23T10:00:00+02:00"}) + "\n"      # missing keys
        + json.dumps(_event("2026-07-23T10:00:00+02:00")) + "\n"
    )

    tracker._render()

    view = (tmp_path / "vault" / "system" / "usage.md").read_text()
    assert "| 2026-07-23 | agent | m1 | 1 | 100 | 0 | 10 |" in view


def test_render_with_no_data_writes_empty_table(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T18:00:00")
    tracker._render()
    view = (tmp_path / "vault" / "system" / "usage.md").read_text()
    lines = view.splitlines()
    assert lines[0] == "# Usage (last 7 days)"
    assert len(lines) == 4  # title, blank, header, separator — no data rows


# ------------------------------------------------------------------
# background run() loop
# ------------------------------------------------------------------

async def test_run_flushes_queued_events_in_one_batch(
    tracker: UsageTracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(tracker, monkeypatch, "2026-07-23T10:00:00")
    monkeypatch.setattr(usage, "_DEBOUNCE_SECONDS", 0.0)
    flushes: list[int] = []
    real_flush = tracker._flush
    monkeypatch.setattr(
        tracker, "_flush", lambda events: (flushes.append(len(events)), real_flush(events))
    )

    tracker.record("agent", "m", {"prompt_tokens": 1, "completion_tokens": 1})
    tracker.record("agent", "m", {"prompt_tokens": 2, "completion_tokens": 2})
    tracker.record("agent", "m", {"prompt_tokens": 3, "completion_tokens": 3})

    task = asyncio.create_task(tracker.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if flushes:
            break
    task.cancel()

    assert flushes == [3]  # one batch, not three
    path = tmp_path / "state" / "usage" / "usage-2026-07.jsonl"
    assert len(path.read_text().splitlines()) == 3


async def test_run_survives_flush_exceptions(
    tracker: UsageTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "_DEBOUNCE_SECONDS", 0.0)
    calls: list[int] = []

    def boom(events: list) -> None:
        calls.append(len(events))
        raise OSError("disk full")

    monkeypatch.setattr(tracker, "_flush", boom)

    tracker.record("agent", "m", {})
    task = asyncio.create_task(tracker.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls:
            break

    # loop is still alive after the exception: a second event gets flushed too
    tracker.record("agent", "m", {})
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(calls) >= 2:
            break
    task.cancel()

    assert len(calls) >= 2
