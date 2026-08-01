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


def test_entry_round_trips_pipes_and_literal_backslashes() -> None:
    entry = ScheduleEntry(
        id="abc123",
        when="0 8 * * *",
        recurring=True,
        prompt=r"Check C:\notes\report.txt | then send a \n summary",
        created="2024-01-01T00:00:00+00:00",
    )

    row = entry.to_row()
    parsed = ScheduleEntry.from_row(row)

    assert parsed is not None
    assert parsed.when == entry.when
    assert parsed.prompt == entry.prompt


def test_entry_refuses_multiline_cells() -> None:
    entry = ScheduleEntry(
        id="abc123",
        when="0 8 * * *",
        recurring=True,
        prompt="line one\nline two",
        created="2024-01-01T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="single-line"):
        entry.to_row()


def test_from_row_preserves_legacy_backslash_sequences() -> None:
    row = r"| abc123 | 0 8 * * * | true | Check C:\notes and send a \r summary | 2024-01-01 |"

    parsed = ScheduleEntry.from_row(row)

    assert parsed is not None
    assert parsed.prompt == r"Check C:\notes and send a \r summary"


def test_from_row_malformed() -> None:
    result = ScheduleEntry.from_row("| not enough |")
    assert result is None


def test_from_row_rejects_extra_columns() -> None:
    result = ScheduleEntry.from_row(
        "| abc123 | 0 8 * * * | true | Brief | 2024-01-01 | next | injected |"
    )

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


def test_schedule_rejects_multiline_time(
    scheduler: Scheduler, vault: VaultTools
) -> None:
    result = scheduler.schedule(
        "0 8 * * *\n| injected | * * * * * | true | hidden job | 2024-01-01",
        "Legitimate job",
        True,
    )

    assert result == "[error: schedule time must be a single line]"
    assert vault.read_file("system/schedule.md").startswith("[file not found")


def test_schedule_rejects_multiline_prompt(
    scheduler: Scheduler, vault: VaultTools
) -> None:
    result = scheduler.schedule("0 8 * * *", "Legitimate\n| injected | row", True)

    assert result == "[error: schedule prompt must be a single line]"
    assert vault.read_file("system/schedule.md").startswith("[file not found")


def test_schedule_rejects_invalid_recurring_expression(
    scheduler: Scheduler, vault: VaultTools
) -> None:
    result = scheduler.schedule("not a cron expression", "Legitimate job", True)

    assert result.startswith("[error: invalid cron expression")
    assert vault.read_file("system/schedule.md").startswith("[file not found")


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


# ------------------------------------------------------------------
# A write must not lose what the read could not parse
# ------------------------------------------------------------------

_TABLE_HEAD = (
    "# Schedule\n\n"
    "| id | when | recurring | prompt | created |\n"
    "|-----|------|-----------|--------|---------|\n"
)


def test_schedule_preserves_rows_it_could_not_parse(vault: VaultTools) -> None:
    """Every mutation read-modify-writes the whole file, and the read drops rows
    it cannot parse — so one malformed row used to erase the entire schedule."""
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD
        + "| keep | 0 8 * * * | true | Weekly brief | 2024-01-01 |\n"
        + "| oops | only-three-columns |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    scheduler.schedule("2099-12-31T23:59:59+00:00", "New job", False)

    text = vault.read_file("system/schedule.md")
    assert "| oops | only-three-columns |" in text, "unparseable row was erased"
    assert "| keep |" in text
    assert "New job" in text


def test_cancel_preserves_rows_it_could_not_parse(vault: VaultTools) -> None:
    """cancel_scheduled() rewrites the file too, so it must not lose rows either."""
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD
        + "| keep | 0 8 * * * | true | Weekly brief | 2024-01-01 |\n"
        + "| oops | only-three-columns |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    scheduler.cancel_scheduled("keep")

    text = vault.read_file("system/schedule.md")
    assert "| oops | only-three-columns |" in text, "unparseable row was erased"
    assert "| keep |" not in text


# ------------------------------------------------------------------
# One bad cron must not take down the reload pass
# ------------------------------------------------------------------

def test_reload_survives_an_invalid_cron_expression(
    vault: VaultTools, caplog: pytest.LogCaptureFixture
) -> None:
    """`0 8 * * 7` is Sunday to crontab but out of range for APScheduler, which
    raises. Unguarded, that aborted the whole pass — and crashed startup, where
    reload() is called without a try."""
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD
        + "| badcron | 0 8 * * 7 | true | Broken cron | 2024-01-01 |\n"
        + "| later | 0 9 * * * | true | Row after the bad one | 2024-01-01 |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    with caplog.at_level(logging.WARNING):
        scheduler.reload()  # must not raise

    assert "badcron" in caplog.text
    assert "later" in scheduler._entries, "a bad row stopped the rows after it"


# ------------------------------------------------------------------
# Missed-run catch-up: the `next` column
# ------------------------------------------------------------------
#
# Recurring rows carry the next computed occurrence, advanced after each
# completed run. On startup, a `next` in the past means the service was down
# when the job was due — catch_up() fires one late run for it.


def test_entry_row_roundtrips_next() -> None:
    entry = ScheduleEntry(
        id="rec1",
        when="0 8 * * *",
        recurring=True,
        prompt="Morning brief",
        created="2024-01-01T00:00:00+00:00",
        next="2026-01-02T08:00:00+00:00",
    )
    parsed = ScheduleEntry.from_row(entry.to_row())
    assert parsed is not None
    assert parsed.next == "2026-01-02T08:00:00+00:00"


def test_from_row_accepts_legacy_five_column_rows() -> None:
    """Pre-`next` schedule files must keep parsing; next defaults empty."""
    parsed = ScheduleEntry.from_row("| abc | 0 8 * * * | true | Brief | 2024-01-01 |")
    assert parsed is not None
    assert parsed.next == ""


def test_schedule_recurring_stores_next_occurrence(scheduler: Scheduler) -> None:
    scheduler.schedule("0 8 * * *", "Morning brief", True)

    entry = scheduler._read_entries()[0]
    nxt = datetime.fromisoformat(entry.next)
    assert nxt > datetime.now(tz=UTC)
    assert nxt - datetime.now(tz=UTC) <= timedelta(days=1)


def test_schedule_one_off_leaves_next_empty(scheduler: Scheduler) -> None:
    scheduler.schedule("2099-12-31T23:59:59+00:00", "One off", False)

    assert scheduler._read_entries()[0].next == ""


async def test_fire_advances_next_after_a_recurring_run(vault: VaultTools) -> None:
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")
    scheduler.schedule("0 8 * * *", "Morning brief", True)
    entries = scheduler._read_entries()
    entries[0].next = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
    scheduler._write_entries(entries)

    await scheduler._fire(entries[0].id, "Morning brief", recurring=True)

    entry = scheduler._read_entries()[0]
    assert datetime.fromisoformat(entry.next) > datetime.now(tz=UTC)


def test_reload_backfills_next_for_hand_added_recurring_rows(vault: VaultTools) -> None:
    """A hand-added row has no baseline, so nothing counts as missed —
    reload establishes one so a *later* downtime is detectable."""
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD + "| hand | 0 8 * * * | true | Brief | 2024-01-01 |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    scheduler.reload()

    entry = scheduler._read_entries()[0]
    assert datetime.fromisoformat(entry.next) > datetime.now(tz=UTC)


def test_reload_keeps_a_past_next_untouched(vault: VaultTools) -> None:
    """A parseable past `next` is the evidence catch_up() needs — reload
    must never 'fix' it forward."""
    past = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD.replace(" created |", " created | next |")
        + f"| rec1 | 0 8 * * * | true | Brief | 2024-01-01 | {past} |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    scheduler.reload()

    assert scheduler._read_entries()[0].next == past


async def test_catch_up_fires_a_recurring_job_whose_next_is_past(
    vault: VaultTools,
) -> None:
    ran = AsyncMock()
    scheduler = Scheduler(vault_tools=vault, run_job_fn=ran, tz_name="UTC")
    scheduler.schedule("0 8 * * *", "Morning brief", True)
    entries = scheduler._read_entries()
    entries[0].next = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
    scheduler._write_entries(entries)

    fired = scheduler.catch_up()
    await asyncio.gather(*scheduler._inflight)

    assert fired == 1
    ran.assert_awaited_once()
    prompt = ran.await_args.args[0]
    assert "catch-up" in prompt
    assert "Morning brief" in prompt
    # The completed run advances `next`, so a second catch_up is a no-op.
    assert datetime.fromisoformat(scheduler._read_entries()[0].next) > datetime.now(tz=UTC)
    assert scheduler.catch_up() == 0


async def test_catch_up_note_carries_the_due_time_in_local_tz(
    vault: VaultTools,
) -> None:
    ran = AsyncMock()
    scheduler = Scheduler(vault_tools=vault, run_job_fn=ran, tz_name="Europe/Madrid")
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD.replace(" created |", " created | next |")
        + "| rec1 | 0 8 * * * | true | Brief | 2024-01-01 | 2026-07-30T06:00:00+00:00 |\n",
    )

    scheduler.catch_up()
    await asyncio.gather(*scheduler._inflight)

    prompt = ran.await_args.args[0]
    assert "2026-07-30 08:00" in prompt  # 06:00 UTC is 08:00 in summer Madrid


async def test_fire_keeps_next_when_the_run_raised(vault: VaultTools) -> None:
    """A failed run must not consume the missed-run evidence — advancing
    `next` past a run that never happened silently loses the reminder."""
    ran = AsyncMock(side_effect=RuntimeError("copilot down"))
    scheduler = Scheduler(vault_tools=vault, run_job_fn=ran, tz_name="UTC")
    scheduler.schedule("0 8 * * *", "Morning brief", True)
    entries = scheduler._read_entries()
    past = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat()
    entries[0].next = past
    scheduler._write_entries(entries)

    await scheduler._fire(entries[0].id, "Morning brief", recurring=True)

    assert scheduler._read_entries()[0].next == past


async def test_fire_skips_when_a_run_for_the_same_job_is_in_flight(
    vault: VaultTools,
) -> None:
    """catch_up() and a live APScheduler fire submitted in the same startup
    instant must not both run the job."""
    calls = 0
    release = asyncio.Event()

    async def _slow(prompt: str) -> None:
        nonlocal calls
        calls += 1
        await release.wait()

    scheduler = Scheduler(vault_tools=vault, run_job_fn=_slow, tz_name="UTC")
    first = asyncio.create_task(scheduler._fire("rec1", "Brief", recurring=True))
    second = asyncio.create_task(scheduler._fire("rec1", "Brief", recurring=True))
    await asyncio.sleep(0)  # let both tasks start
    release.set()
    await asyncio.gather(first, second)

    assert calls == 1


def test_reload_backfill_preserves_rows_it_could_not_parse(vault: VaultTools) -> None:
    """The backfill write is a table rewrite like any other — it must not
    erase rows the parser skipped."""
    vault.write_file(
        "system/schedule.md",
        _TABLE_HEAD
        + "| hand | 0 8 * * * | true | Brief | 2024-01-01 |\n"
        + "| oops | only-three-columns |\n",
    )
    scheduler = Scheduler(vault_tools=vault, run_job_fn=AsyncMock(), tz_name="UTC")

    scheduler.reload()

    text = vault.read_file("system/schedule.md")
    assert "| oops | only-three-columns |" in text, "unparseable row was erased"
    assert datetime.fromisoformat(scheduler._read_entries()[0].next) > datetime.now(tz=UTC)


async def test_catch_up_ignores_future_one_off_and_unparseable_rows(
    vault: VaultTools,
) -> None:
    ran = AsyncMock()
    scheduler = Scheduler(vault_tools=vault, run_job_fn=ran, tz_name="UTC")
    scheduler.schedule("0 8 * * *", "Future brief", True)  # next is in the future
    vault_text = vault.read_file("system/schedule.md")
    vault.write_file(
        "system/schedule.md",
        vault_text
        + "| old1 | 2020-01-01T00:00:00+00:00 | false | Old one-off | 2020-01-01 |\n"
        + "| bad1 | 0 9 * * * | true | Garbage next | 2020-01-01 | not-a-date |\n",
    )

    fired = scheduler.catch_up()

    assert fired == 0
    ran.assert_not_awaited()


# ------------------------------------------------------------------
# A one-off must not fire twice
# ------------------------------------------------------------------

async def test_reload_skips_a_one_off_that_is_still_running(vault: VaultTools) -> None:
    """_fire deletes the row only after the run finishes, so a poll landing
    mid-run found a live row with no APScheduler job and re-registered it —
    submitting a second execution of a job that had already fired."""
    from unittest.mock import patch as mock_patch

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(prompt: str) -> None:
        started.set()
        await release.wait()

    scheduler = Scheduler(vault_tools=vault, run_job_fn=_slow, tz_name="UTC")
    result = scheduler.schedule("2099-01-01T09:00:00+00:00", "One off", False)
    job_id = result.split()[2]

    fire = asyncio.create_task(scheduler._fire(job_id, "One off", recurring=False))
    await started.wait()
    # APScheduler drops a date job as soon as it has fired; production logs show
    # "Removed job <id>" at the firing instant. Model that here.
    job = scheduler._apscheduler.get_job(job_id)
    if job is not None:
        job.remove()

    with mock_patch.object(scheduler, "_register", wraps=scheduler._register) as spy:
        scheduler.reload()

    assert spy.call_count == 0, "re-registered a one-off whose run was in flight"

    release.set()
    await fire
