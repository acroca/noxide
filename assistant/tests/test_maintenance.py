"""Tests for the built-in maintenance jobs (nightly compile, weekly lint)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.maintenance import (
    COMPILE_ID,
    COMPILE_PROMPT,
    LINT_ID,
    MaintenanceState,
    builtin_jobs,
)
from assistant.schedule import Scheduler
from assistant.tools import VaultTools


def _scheduler(
    tmp_path: Path,
    run: AsyncMock | None = None,
    compile_cron: str = "0 3 * * *",
    lint_cron: str = "0 4 * * SUN",
) -> tuple[Scheduler, MaintenanceState]:
    state = MaintenanceState(tmp_path / "state" / "maintenance.json")
    scheduler = Scheduler(
        vault_tools=VaultTools(tmp_path / "vault"),
        run_job_fn=run or AsyncMock(),
        tz_name="UTC",
        builtins=builtin_jobs(compile_cron, lint_cron),
        maintenance_state=state,
    )
    return scheduler, state


# ------------------------------------------------------------------
# Definitions
# ------------------------------------------------------------------

def test_empty_cron_disables_a_builtin_job() -> None:
    jobs = builtin_jobs("", "0 4 * * SUN")

    assert [job.id for job in jobs] == [LINT_ID]


def test_builtin_prompts_do_not_restate_run_plumbing() -> None:
    """The runner prepends [scheduled run]; the close contract applies on its
    own — the schedule tool refuses prompts that restate either."""
    for job in builtin_jobs("0 3 * * *", "0 4 * * SUN"):
        assert "[scheduled run]" not in job.prompt
        assert "[silent]" not in job.prompt


# ------------------------------------------------------------------
# Registration: present without any schedule.md row, immune to reload
# ------------------------------------------------------------------

def test_builtins_are_registered_without_a_schedule_row(tmp_path: Path) -> None:
    scheduler, _ = _scheduler(tmp_path)
    scheduler.reload()

    assert scheduler._apscheduler.get_job(COMPILE_ID) is not None
    assert scheduler._apscheduler.get_job(LINT_ID) is not None
    assert scheduler._read_entries() == []


def test_reload_keeps_builtins_when_the_table_is_empty(tmp_path: Path) -> None:
    scheduler, _ = _scheduler(tmp_path)
    scheduler.schedule("0 8 * * *", "Brief", True)
    scheduler.reload()
    scheduler.cancel_scheduled(scheduler._read_entries()[0].id)
    scheduler.reload()

    assert scheduler._apscheduler.get_job(COMPILE_ID) is not None


def test_list_scheduled_shows_builtins_as_built_in(tmp_path: Path) -> None:
    scheduler, _ = _scheduler(tmp_path)

    listing = scheduler.list_scheduled()

    assert COMPILE_ID in listing
    assert "built-in" in listing
    assert "0 3 * * *" in listing


def test_cancelling_a_builtin_is_refused(tmp_path: Path) -> None:
    scheduler, _ = _scheduler(tmp_path)

    result = scheduler.cancel_scheduled(COMPILE_ID)

    assert "built-in" in result
    assert "config" in result
    assert scheduler._apscheduler.get_job(COMPILE_ID) is not None


# ------------------------------------------------------------------
# Completion bookkeeping and catch-up after downtime
# ------------------------------------------------------------------

async def test_completed_builtin_run_is_recorded_and_persisted(tmp_path: Path) -> None:
    scheduler, state = _scheduler(tmp_path)
    before = datetime.now(tz=UTC)

    await scheduler._fire(COMPILE_ID, COMPILE_PROMPT, recurring=True)

    recorded = state.last_success(COMPILE_ID)
    assert recorded is not None and recorded >= before
    reloaded = MaintenanceState(tmp_path / "state" / "maintenance.json")
    assert reloaded.last_success(COMPILE_ID) == recorded


async def test_failed_builtin_run_is_not_recorded(tmp_path: Path) -> None:
    scheduler, state = _scheduler(tmp_path, run=AsyncMock(side_effect=RuntimeError("down")))

    await scheduler._fire(COMPILE_ID, COMPILE_PROMPT, recurring=True)

    assert state.last_success(COMPILE_ID) is None


async def test_catch_up_fires_a_builtin_missed_while_down(tmp_path: Path) -> None:
    ran = AsyncMock()
    scheduler, state = _scheduler(tmp_path, run=ran)
    state.mark_success(COMPILE_ID, datetime.now(tz=UTC) - timedelta(days=2))
    state.mark_success(LINT_ID, datetime.now(tz=UTC))

    fired = scheduler.catch_up()
    await asyncio.gather(*scheduler._inflight)

    assert fired == 1
    prompt = ran.await_args.args[0]
    assert "catch-up" in prompt
    assert COMPILE_PROMPT in prompt
    # The completed run advances the baseline, so a second catch_up is a no-op.
    assert scheduler.catch_up() == 0


def test_first_start_establishes_a_baseline_instead_of_firing(tmp_path: Path) -> None:
    """No record means no evidence of a missed run — but a baseline is set
    so a *later* downtime is detectable, mirroring the `next` backfill."""
    scheduler, state = _scheduler(tmp_path)

    fired = scheduler.catch_up()

    assert fired == 0
    assert state.last_success(COMPILE_ID) is not None
    assert state.last_success(LINT_ID) is not None


def test_state_tolerates_a_missing_or_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.json"
    assert MaintenanceState(path).last_success(COMPILE_ID) is None

    path.write_text("{not json")
    state = MaintenanceState(path)
    assert state.last_success(COMPILE_ID) is None

    state.mark_success(COMPILE_ID, datetime(2026, 9, 3, 1, 0, tzinfo=UTC))
    assert MaintenanceState(path).last_success(COMPILE_ID) == datetime(2026, 9, 3, 1, 0, tzinfo=UTC)


def test_builtins_require_the_maintenance_state(tmp_path: Path) -> None:
    """Registered built-ins with no bookkeeping would be a silent mode where
    nothing ever catches up; refuse it at construction."""
    import pytest

    with pytest.raises(ValueError, match="maintenance_state"):
        Scheduler(
            vault_tools=VaultTools(tmp_path / "vault"),
            run_job_fn=AsyncMock(),
            builtins=builtin_jobs("0 3 * * *", ""),
        )


def test_a_schedule_row_using_a_builtin_id_is_ignored_and_preserved(tmp_path: Path) -> None:
    """Ids are free text in the hand-editable table; a row named `compile`
    must neither replace the built-in nor be lost by the next table write."""
    scheduler, _ = _scheduler(tmp_path)
    vault = scheduler._vault
    vault.write_file(
        "system/schedule.md",
        "# Schedule\n\n| id | when | recurring | prompt | created | next |\n"
        "|-----|------|-----------|--------|---------|------|\n"
        "| compile | 0 5 * * * | true | Impostor | 2024-01-01 |  |\n",
    )

    scheduler.reload()
    scheduler.schedule("0 8 * * *", "Brief", True)  # rewrites the table

    assert scheduler._apscheduler.get_job(COMPILE_ID).args[1] == COMPILE_PROMPT
    assert "Impostor" in vault.read_file("system/schedule.md")
    assert all(e.id != COMPILE_ID for e in scheduler._read_entries())
