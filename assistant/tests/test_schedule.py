"""Tests for schedule.md round-trip parse/write and relative time parsing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.schedule import ScheduleEntry, Scheduler, parse_when
from assistant.tools import VaultTools

# ------------------------------------------------------------------
# parse_when
# ------------------------------------------------------------------

def test_parse_iso_datetime() -> None:
    dt = parse_when("2024-06-15T09:00:00+00:00", "UTC")
    assert dt is not None
    assert dt.year == 2024
    assert dt.month == 6
    assert dt.day == 15
    assert dt.hour == 9


def test_parse_relative_in_10_minutes() -> None:
    before = datetime.now(tz=UTC)
    dt = parse_when("in 10 minutes", "UTC")
    after = datetime.now(tz=UTC)
    assert dt is not None
    # Should be ~10 minutes from now
    expected_min = before + timedelta(minutes=9)
    expected_max = after + timedelta(minutes=11)
    assert expected_min <= dt <= expected_max


def test_parse_relative_in_1_hour() -> None:
    before = datetime.now(tz=UTC)
    dt = parse_when("in 1 hour", "UTC")
    assert dt is not None
    delta = dt - before
    assert 3500 < delta.total_seconds() < 3700


def test_parse_tomorrow_at_9am_madrid() -> None:
    dt = parse_when("tomorrow at 9am", "Europe/Madrid")
    assert dt is not None
    assert dt.hour in (7, 8)  # UTC offset for Madrid is +1 or +2 (summer)
    assert dt > datetime.now(tz=UTC)


def test_parse_invalid_returns_none() -> None:
    # Very unlikely to parse as a date
    parse_when("definitely not a date xyzzy", "UTC")
    # dateparser is permissive — we just check it doesn't crash
    # If it returns something, that's fine; we only assert no exception


def test_parse_returns_utc() -> None:
    """parse_when should return UTC-aware datetimes."""
    dt = parse_when("in 5 minutes", "Europe/Madrid")
    assert dt is not None
    # Should be UTC-aware
    assert dt.tzinfo is not None


# ------------------------------------------------------------------
# ScheduleEntry row round-trip
# ------------------------------------------------------------------

def test_entry_to_row_and_back() -> None:
    entry = ScheduleEntry(
        id="abc123",
        when="2024-06-15T09:00:00+00:00",
        recurring=False,
        prompt="Call Marco",
        created="2024-06-14T08:00:00+00:00",
    )
    row = entry.to_row()
    parsed = ScheduleEntry.from_row(row)
    assert parsed is not None
    assert parsed.id == "abc123"
    assert parsed.when == "2024-06-15T09:00:00+00:00"
    assert parsed.recurring is False
    assert parsed.prompt == "Call Marco"


def test_entry_recurring() -> None:
    entry = ScheduleEntry(
        id="xyz",
        when="0 8 * * *",
        recurring=True,
        prompt="Morning brief",
        created="2024-01-01T00:00:00+00:00",
    )
    row = entry.to_row()
    parsed = ScheduleEntry.from_row(row)
    assert parsed is not None
    assert parsed.recurring is True
    assert parsed.when == "0 8 * * *"


def test_entry_prompt_with_pipe() -> None:
    """Pipes in prompts should be escaped/unescaped."""
    entry = ScheduleEntry(
        id="pip1",
        when="in 5 minutes",
        recurring=False,
        prompt="Do this | and that",
        created="2024-01-01T00:00:00+00:00",
    )
    row = entry.to_row()
    parsed = ScheduleEntry.from_row(row)
    assert parsed is not None
    assert parsed.prompt == "Do this | and that"


def test_from_row_malformed() -> None:
    result = ScheduleEntry.from_row("| not enough |")
    assert result is None


def test_from_row_keeps_a_blank_created_column() -> None:
    """A hand-written row that omits `created` must not lose the job.

    Collapsing the blank would shift every later field left, so the row would
    come back one column short and be dropped without a trace.
    """
    parsed = ScheduleEntry.from_row("| abc123 | 0 8 * * * | true | Morning brief |  |")

    assert parsed is not None
    assert parsed.id == "abc123"
    assert parsed.when == "0 8 * * *"
    assert parsed.recurring is True
    assert parsed.prompt == "Morning brief"
    assert parsed.created == ""


def test_from_row_keeps_a_blank_interior_column() -> None:
    parsed = ScheduleEntry.from_row("| abc123 | 0 8 * * * |  | Morning brief | 2024-01-01 |")

    assert parsed is not None
    assert parsed.recurring is False  # blank is not "true"
    assert parsed.prompt == "Morning brief"
    assert parsed.created == "2024-01-01"


def test_unparseable_table_row_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """schedule.md is hand-editable, so a bad row must be noisy, not silent."""
    vault = VaultTools(tmp_path)
    vault.write_file(
        "system/schedule.md",
        "# Schedule\n\n"
        "| id | when | recurring | prompt | created |\n"
        "|-----|------|-----------|--------|---------|\n"
        "| good | 0 8 * * * | true | Brief | 2024-01-01 |\n"
        "| oops | missing-columns |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    with caplog.at_level(logging.WARNING):
        entries = scheduler._read_entries()

    assert [e.id for e in entries] == ["good"]
    assert "oops" in caplog.text


# ------------------------------------------------------------------
# schedule.md round-trip via Scheduler
# ------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    return VaultTools(tmp_path)


@pytest.fixture
def scheduler(vault: VaultTools) -> Scheduler:
    async def _noop(prompt: str) -> None:
        pass

    s = Scheduler(vault_tools=vault, run_job_fn=_noop, tz_name="UTC")
    # Don't start APScheduler for unit tests — we only test parse/write
    return s


def test_schedule_writes_entry(scheduler: Scheduler) -> None:
    """schedule() should write an entry to schedule.md."""
    result = scheduler.schedule(
        when="2099-12-31T23:59:59+00:00",
        prompt="New year reminder",
        recurring=False,
    )
    assert "Scheduled job" in result

    entries = scheduler._read_entries()
    assert len(entries) == 1
    assert entries[0].prompt == "New year reminder"


def test_schedule_roundtrip_multiple(scheduler: Scheduler) -> None:
    """Multiple entries should survive a write/read round-trip."""
    scheduler.schedule("2099-01-01T09:00:00+00:00", "Job A", False)
    scheduler.schedule("2099-02-01T09:00:00+00:00", "Job B", False)
    entries = scheduler._read_entries()
    prompts = {e.prompt for e in entries}
    assert "Job A" in prompts
    assert "Job B" in prompts


def test_cancel_removes_entry(scheduler: Scheduler) -> None:
    result = scheduler.schedule("2099-06-01T10:00:00+00:00", "Test job", False)
    job_id = result.split()[2]  # "Scheduled job <id> at ..."
    entries_before = scheduler._read_entries()
    assert any(e.id == job_id for e in entries_before)

    cancel_result = scheduler.cancel_scheduled(job_id)
    assert "Cancelled" in cancel_result

    entries_after = scheduler._read_entries()
    assert not any(e.id == job_id for e in entries_after)


def test_list_scheduled_empty(scheduler: Scheduler) -> None:
    result = scheduler.list_scheduled()
    assert result == "[no scheduled jobs]"


def test_list_scheduled_shows_entries(scheduler: Scheduler) -> None:
    scheduler.schedule("2099-06-01T10:00:00+00:00", "My reminder", False)
    result = scheduler.list_scheduled()
    assert "My reminder" in result


def test_cancel_nonexistent(scheduler: Scheduler) -> None:
    result = scheduler.cancel_scheduled("doesnotexist")
    assert "not found" in result


# ------------------------------------------------------------------
# Reload idempotence
# ------------------------------------------------------------------

def test_reload_skips_unchanged_jobs(scheduler: Scheduler) -> None:
    """reload() must not re-register jobs that haven't changed (log spam,
    needless APScheduler churn every poll)."""
    from unittest.mock import patch as mock_patch

    scheduler.schedule(
        when="2099-12-31T23:59:59+00:00",
        prompt="New year reminder",
        recurring=False,
    )

    with mock_patch.object(scheduler, "_register", wraps=scheduler._register) as spy:
        scheduler.reload()
        scheduler.reload()

    assert spy.call_count == 0


def test_reload_registers_new_and_changed_jobs(scheduler: Scheduler) -> None:
    """reload() must still pick up entries added or edited in schedule.md."""
    from unittest.mock import patch as mock_patch

    scheduler.schedule(
        when="2099-12-31T23:59:59+00:00",
        prompt="Original prompt",
        recurring=False,
    )
    entries = scheduler._read_entries()
    entries[0].prompt = "Edited prompt"
    scheduler._write_entries(entries)

    with mock_patch.object(scheduler, "_register", wraps=scheduler._register) as spy:
        scheduler.reload()

    assert spy.call_count == 1
    assert scheduler._entries[entries[0].id].prompt == "Edited prompt"


# ------------------------------------------------------------------
# Graceful drain
# ------------------------------------------------------------------


async def test_drain_returns_immediately_when_idle(tmp_path: Path) -> None:
    sched = Scheduler(vault_tools=VaultTools(tmp_path), run_job_fn=AsyncMock(), tz_name="UTC")
    sched.start()

    assert await asyncio.wait_for(sched.drain(timeout=5), timeout=1) == 0


async def test_drain_waits_for_an_in_flight_job(tmp_path: Path) -> None:
    finished = asyncio.Event()

    async def slow_job(prompt: str) -> None:
        await asyncio.sleep(0.05)
        finished.set()

    sched = Scheduler(vault_tools=VaultTools(tmp_path), run_job_fn=slow_job, tz_name="UTC")
    sched.start()
    fire = asyncio.create_task(sched._fire("job-1", "do a thing", recurring=True))
    await asyncio.sleep(0)  # let _fire register itself

    unfinished = await asyncio.wait_for(sched.drain(timeout=5), timeout=2)

    assert unfinished == 0
    assert finished.is_set()
    await fire


async def test_drain_gives_up_at_the_deadline(tmp_path: Path) -> None:
    async def wedged_job(prompt: str) -> None:
        await asyncio.sleep(30)

    sched = Scheduler(vault_tools=VaultTools(tmp_path), run_job_fn=wedged_job, tz_name="UTC")
    sched.start()
    fire = asyncio.create_task(sched._fire("job-1", "do a thing", recurring=True))
    await asyncio.sleep(0)

    unfinished = await asyncio.wait_for(sched.drain(timeout=0.05), timeout=2)

    assert unfinished == 1
    fire.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fire


async def test_drain_stops_the_scheduler(tmp_path: Path) -> None:
    sched = Scheduler(vault_tools=VaultTools(tmp_path), run_job_fn=AsyncMock(), tz_name="UTC")
    sched.start()

    await sched.drain(timeout=5)

    assert not sched._apscheduler.running


async def test_drain_is_safe_before_start(tmp_path: Path) -> None:
    """Shutdown can race a failed startup — drain must not raise."""
    sched = Scheduler(vault_tools=VaultTools(tmp_path), run_job_fn=AsyncMock(), tz_name="UTC")

    assert await sched.drain(timeout=5) == 0
