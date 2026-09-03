"""Tests for the deterministic vault consistency checks."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.vault_check import run_checks

# The fixtures below date from 2026-08-03 (the now.md header in _clean_vault);
# pinning "today" there keeps their due dates in the future as real time passes.
_TODAY = date(2026, 8, 3)


def _check(root: Path) -> str:
    return run_checks(root, today=_TODAY)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _clean_vault(root: Path) -> None:
    """A minimal consistent vault: one open task, mirrored in now.md."""
    _write(root, "wiki/projects/garden.md", "# Garden\n\n## Tasks\n- [ ] buy compost (due 2026-08-05)\n")
    _write(
        root,
        "wiki/now.md",
        "# Now — Monday — 2026-08-03\n\n## Tasks\n[garden](projects/garden.md)\n- [ ] buy compost (due 2026-08-05)\n",
    )


# ---------------------------------------------------------------------------
# Task mirror: every open task on a wiki page must appear in wiki/now.md
# ---------------------------------------------------------------------------


def test_clean_vault_reports_no_findings(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    assert _check(tmp_path) == "[no findings]"


def test_open_task_missing_from_mirror_is_reported_with_location(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/health.md", "# Health\n\n## Tasks\n- [ ] book dentist\n")

    report = _check(tmp_path)

    assert "wiki/areas/health.md:4" in report
    assert "book dentist" in report
    assert "wiki/now.md" in report


def test_mirror_line_with_no_open_task_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/now.md",
        "# Now — Monday — 2026-08-03\n\n## Tasks\n"
        "- [ ] buy compost (due 2026-08-05)\n- [ ] task closed on its page\n",
    )

    report = _check(tmp_path)

    assert "wiki/now.md:5" in report
    assert "task closed on its page" in report


def test_mirror_match_tolerates_decoration_around_the_task_text(tmp_path: Path) -> None:
    """A mirror line may carry extra context (page label, arrows); the task
    counts as mirrored when its text is contained in some now.md line."""
    _write(tmp_path, "wiki/projects/garden.md", "## Tasks\n- [ ] buy compost (due 2026-08-05)\n")
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] buy compost (due 2026-08-05) — garden\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_prose_mention_in_now_md_is_not_a_mirror_line(tmp_path: Path) -> None:
    """A task named in a Last-7-days bullet (or any prose) is not mirrored —
    only a checkbox line in now.md satisfies the Tasks-inventory invariant."""
    _write(tmp_path, "wiki/areas/health.md", "## Tasks\n- [ ] book dentist\n")
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n\n## Last 7 days\n- 2026-08-02: decided to book dentist soon\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/health.md:2" in report
    assert "book dentist" in report


def test_done_tasks_need_no_mirror(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/projects/attic.md", "## Tasks\n- [x] clear attic (done 2026-08-01)\n")

    assert _check(tmp_path) == "[no findings]"


def test_archived_pages_are_exempt_from_the_mirror(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/archive/projects/old.md",
        "## Tasks\n- [ ] never finished, deliberately dropped\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_missing_now_md_is_one_finding_not_one_per_task(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/projects/a.md", "## Tasks\n- [ ] first\n")
    _write(tmp_path, "wiki/projects/b.md", "## Tasks\n- [ ] second\n")

    report = _check(tmp_path)

    assert "wiki/now.md" in report
    assert "first" not in report
    assert "second" not in report


def test_empty_vault_reports_no_findings(tmp_path: Path) -> None:
    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Weekday labels: a weekday written beside an ISO date must match it
# ---------------------------------------------------------------------------


def test_wrong_weekday_beside_date_is_reported(tmp_path: Path) -> None:
    # 2026-08-04 is a Tuesday.
    _write(tmp_path, "wiki/now.md", "# Now — Monday — 2026-08-04\n")

    report = _check(tmp_path)

    assert "wiki/now.md:1" in report
    assert "Monday" in report
    assert "Tuesday" in report


def test_correct_weekday_beside_date_passes(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "# Now — Tuesday — 2026-08-04\n")

    assert _check(tmp_path) == "[no findings]"


def test_weekday_after_the_date_is_also_checked(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/projects/trip.md", "Flight home 2026-08-09 (Saturday)\n")

    report = _check(tmp_path)

    assert "wiki/projects/trip.md:1" in report
    assert "Sunday" in report


def test_localized_weekday_names_are_checked(tmp_path: Path) -> None:
    # 2026-08-05 is a Wednesday — "dimarts" (Catalan Tuesday) is wrong,
    # and the correction is offered in the same language.
    _write(tmp_path, "wiki/areas/family.md", "Sopar dimarts 2026-08-05\n")

    report = _check(tmp_path)

    assert "wiki/areas/family.md:1" in report
    assert "dimecres" in report


def test_weekday_far_from_a_date_is_not_paired_with_it(tmp_path: Path) -> None:
    """'Monday' names the standup day, not the due date — no pairing across
    intervening words."""
    _write(
        tmp_path,
        "wiki/projects/work.md",
        "## Tasks\n- [ ] prepare Monday standup notes (due 2026-08-11)\n",
    )
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] prepare Monday standup notes (due 2026-08-11)\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_invalid_calendar_date_beside_weekday_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "Review on Monday — 2026-02-30\n")

    report = _check(tmp_path)

    assert "wiki/now.md:1" in report
    assert "2026-02-30" in report


def test_append_only_files_are_out_of_scope(tmp_path: Path) -> None:
    """Journal, system files and wiki/log.md are append-only or bot-managed:
    a retroactive finding there would nag forever with no fix allowed."""
    _clean_vault(tmp_path)
    _write(tmp_path, "raw/journal/2026-08-01.md", "- 09:00 met Ana on Monday 2026-08-04\n")
    _write(tmp_path, "system/schedule.md", "| job | Monday 2026-08-04 |\n")
    _write(
        tmp_path,
        "wiki/log.md",
        "## [2026-08-02] compile | rebuilt\n## [2026-08-02] lint | flagged Monday 2026-08-04 clash\n",
    )

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_counts_findings(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/health.md", "## Tasks\n- [ ] book dentist\n")
    _write(tmp_path, "wiki/projects/trip.md", "Flight 2026-08-09 (Saturday)\n")

    report = _check(tmp_path)

    assert report.startswith("2 findings")


def test_report_caps_findings_and_says_so(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "## Tasks\n")
    lines = "\n".join(f"- [ ] task number {i}" for i in range(120))
    _write(tmp_path, "wiki/projects/big.md", f"## Tasks\n{lines}\n")

    report = _check(tmp_path)

    assert "120 findings" in report
    assert "truncated" in report
    assert report.count("- [ ] task number") <= 100


# ---------------------------------------------------------------------------
# Reminder markers: every pending one-off job leaves a [reminder:<id>] marker
# on a wiki page, and every marker references a still-pending job.
# ---------------------------------------------------------------------------

_SCHEDULE_HEADER = (
    "# Schedule\n\n"
    "| id | when | recurring | prompt | created | next |\n"
    "|-----|------|-----------|--------|---------|------|\n"
)


def _schedule(root: Path, *rows: str) -> None:
    _write(root, "system/schedule.md", _SCHEDULE_HEADER + "".join(r + "\n" for r in rows))


def _one_off_row(job_id: str, prompt: str = "Ask how it went.") -> str:
    return f"| {job_id} | 2026-08-12T18:00:00+00:00 | false | {prompt} | 2026-08-01T00:00:00+00:00 |  |"


def _recurring_row(job_id: str) -> str:
    return f"| {job_id} | 0 7 * * * | true | Daily pill reminder. | 2026-08-01T00:00:00+00:00 | 2026-08-05T05:00:00+00:00 |"


def test_one_off_job_without_marker_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34"))

    report = _check(tmp_path)

    assert "system/schedule.md" in report
    assert "[reminder:ab12cd34]" in report


def test_one_off_job_with_marker_passes(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34"))
    _write(
        tmp_path,
        "wiki/areas/salud.md",
        "# Salud\n\nVisita hepatólogo 2026-08-12 [reminder:ab12cd34]\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_recurring_jobs_need_no_marker(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _recurring_row("11223344"))

    assert _check(tmp_path) == "[no findings]"


def test_stale_marker_is_reported_with_location(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34"))
    _write(
        tmp_path,
        "wiki/areas/salud.md",
        "# Salud\n\nVisita [reminder:ab12cd34]\n\nOtra cosa [reminder:deadbeef]\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/salud.md:5" in report
    assert "deadbeef" in report


def test_marker_for_recurring_job_is_not_stale(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _recurring_row("11223344"))
    _write(
        tmp_path,
        "wiki/routines.md",
        "# Rutinas\n\nPastilla diaria [reminder:11223344]\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_no_schedule_file_skips_reminder_checks(tmp_path: Path) -> None:
    """Scheduling may be disabled; a marker cannot be judged stale then."""
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/salud.md", "# Salud\n\nVisita [reminder:deadbeef]\n")

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Leaked version tokens: a "[version: ...]" line in a wiki page is read_file
# output pasted back as content, never something the page should contain.
# ---------------------------------------------------------------------------


def test_leaked_version_token_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/salud.md", "# Salud\n\nnotas\n[version: bad69be0]\n")

    report = _check(tmp_path)

    assert "wiki/areas/salud.md:4" in report
    assert "version token" in report


# ---------------------------------------------------------------------------
# Schedule hygiene: existing/hand-written rows carrying what the schedule
# tool now refuses — hand-written [scheduled run] tags, restated close
# contract, numeric cron weekdays.
# ---------------------------------------------------------------------------


def test_job_prompt_with_scheduled_run_tag_is_flagged(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34", "[scheduled run] Remind Albert of X."))

    report = _check(tmp_path)

    assert "system/schedule.md:5" in report
    assert "[scheduled run]" in report


def test_job_prompt_restating_silent_contract_is_flagged(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34", "Check state; if met, responde [silent]."))
    _write(tmp_path, "wiki/a.md", "# A\n\nitem [reminder:ab12cd34]\n")

    report = _check(tmp_path)

    assert "system/schedule.md:5" in report
    assert "close contract" in report


def test_numeric_cron_weekday_row_is_flagged(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, "| 11223344 | 0 8 * * 0 | true | Weekly check. | 2026-08-01T00:00:00+00:00 |  |")

    report = _check(tmp_path)

    assert "system/schedule.md:5" in report
    assert "day-of-week" in report


def test_clean_schedule_rows_pass_hygiene(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(
        tmp_path,
        _recurring_row("11223344"),
        "| 55667788 | 0 8 * * SUN | true | Weekly stock check. | 2026-08-01T00:00:00+00:00 |  |",
    )

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Routine due dates: Next due = last done + frequency is date arithmetic;
# check it like weekdays instead of trusting the model's recompute.
# ---------------------------------------------------------------------------


def _routines(root: Path, *rows: str) -> None:
    _write(
        root,
        "wiki/routines.md",
        "# Rutinas\n\n"
        "| Rutina | Frecuencia | Última vez | Próxima | Notas |\n"
        "| --- | --- | --- | --- | --- |\n" + "".join(r + "\n" for r in rows),
    )


def test_daily_routine_next_due_mismatch_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _routines(tmp_path, "| Pastilla | Diaria | 2026-08-04 09:23 | 2026-08-07 | x |")

    report = _check(tmp_path)

    assert "wiki/routines.md:5" in report
    assert "expected 2026-08-05" in report


def test_correct_next_due_values_pass(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _routines(
        tmp_path,
        "| Pastilla | Diaria | 2026-08-04 09:23 | 2026-08-05 | x |",
        "| Plain weekly | Semanal | 2026-08-01 | 2026-08-08 | x |",
        "| Stock | Semanal (domingo) | 2026-08-04 | 2026-08-09 | x |",
        "| Pill | Daily | 2026-08-04 | 2026-08-05 | x |",
        "| Water | Every 3 days | 2026-08-01 | 2026-08-04 | x |",
        "| Amazon | Mensual | 2026-07-27 | 2026-08-27 | x |",
    )

    assert _check(tmp_path) == "[no findings]"


def test_weekday_anchored_weekly_uses_next_occurrence(tmp_path: Path) -> None:
    """Semanal (domingo) from a Tuesday means the coming Sunday, not +7."""
    _clean_vault(tmp_path)
    _routines(tmp_path, "| Stock | Semanal (domingo) | 2026-08-04 | 2026-08-11 | x |")

    report = _check(tmp_path)

    assert "wiki/routines.md:5" in report
    assert "expected 2026-08-09" in report


def test_range_frequency_accepts_the_window(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _routines(tmp_path, "| Semillas | Cada 2-3 días | 2026-07-29 | 2026-08-01 | x |")
    assert _check(tmp_path) == "[no findings]"

    _routines(tmp_path, "| Semillas | Cada 2-3 días | 2026-07-29 | 2026-08-03 | x |")
    report = _check(tmp_path)
    assert "wiki/routines.md:5" in report
    assert "expected 2026-07-31..2026-08-01" in report


def test_unparseable_rows_are_skipped(tmp_path: Path) -> None:
    """Approximate frequencies, missing last-done, paused rows: no findings."""
    _clean_vault(tmp_path)
    _routines(
        tmp_path,
        "| Bebedero | Cada ~6 días | 2026-07-27 | 2026-08-02 | x |",
        "| Stock | Semanal (domingo) | — | 2026-08-02 | x |",
        "| Gusano | Semanal (lunes) | 2026-07-27 | En pausa | x |",
    )

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Index: every wiki page is linked from some index.md, and every index link
# points at a page that exists. Enumeration replaces the lint's sampled
# "orphan pages; index drift" sweep.
# ---------------------------------------------------------------------------


def _index(root: Path, *lines: str) -> None:
    _write(root, "wiki/index.md", "# Índice\n\n" + "".join(line + "\n" for line in lines))


def _indexed_clean_vault(root: Path) -> None:
    _clean_vault(root)
    _index(
        root,
        "- [now](now.md) — dashboard",
        "- [garden](projects/garden.md) — the garden",
    )


def test_unindexed_page_is_reported(tmp_path: Path) -> None:
    _indexed_clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/quiet.md", "# Quiet corner\n")

    report = _check(tmp_path)

    assert "wiki/areas/quiet.md" in report
    assert "index" in report


def test_fully_indexed_vault_passes(tmp_path: Path) -> None:
    _indexed_clean_vault(tmp_path)

    assert _check(tmp_path) == "[no findings]"


def test_dead_index_link_is_reported(tmp_path: Path) -> None:
    _indexed_clean_vault(tmp_path)
    _index(
        tmp_path,
        "- [now](now.md) — dashboard",
        "- [garden](projects/garden.md) — the garden",
        "- [ghost](projects/ghost.md) — page that no longer exists",
    )

    report = _check(tmp_path)

    assert "wiki/index.md:5" in report
    assert "projects/ghost.md" in report


def test_nested_index_covers_its_subtree(tmp_path: Path) -> None:
    """Sub-pages listed in a nested index need no top-level line — the
    busqueda-empleo pattern: empresas/* live in the project's own index."""
    _indexed_clean_vault(tmp_path)
    _index(
        tmp_path,
        "- [now](now.md) — dashboard",
        "- [garden](projects/garden.md) — the garden",
        "- [jobs](projects/jobs/index.md) — job hunt",
    )
    _write(
        tmp_path,
        "wiki/projects/jobs/index.md",
        "# Jobs\n\n- [acme](companies/acme.md) — applied\n",
    )
    _write(tmp_path, "wiki/projects/jobs/companies/acme.md", "# Acme\n\n**Status:** applied\n")

    assert _check(tmp_path) == "[no findings]"


def test_missing_index_skips_the_check(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/quiet.md", "# Quiet corner\n")

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Overdue tasks: an open task whose due date has passed needs the user's
# decision (reschedule, drop, or keep) — the compile escalates the ones that
# lapsed since it last ran, the lint escalates the rest. Enumerated by code
# because the compile prompt's "flag lapsed deadlines" rule was followed on
# one night in nine.
# ---------------------------------------------------------------------------


def test_overdue_task_is_reported_with_age(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/areas/finanzas.md", "## Tasks\n- [ ] pasar el gas (para 2026-08-24)\n")
    _write(tmp_path, "wiki/now.md", "## Tasks\n- [ ] pasar el gas (para 2026-08-24)\n")

    report = run_checks(tmp_path, today=date(2026, 9, 2))

    assert "wiki/areas/finanzas.md:2" in report
    assert "overdue 9 days" in report
    assert "pasar el gas" in report


def test_task_due_today_or_later_is_not_overdue(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "wiki/areas/casa.md",
        "## Tasks\n- [ ] cajones (due 2026-09-02)\n- [ ] alfombra (due 2026-09-09)\n",
    )
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] cajones (due 2026-09-02)\n- [ ] alfombra (due 2026-09-09)\n",
    )

    assert run_checks(tmp_path, today=date(2026, 9, 2)) == "[no findings]"


def test_approximate_due_date_still_counts(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/areas/finanzas.md", "## Tasks\n- [ ] gas (para ~2026-08-24)\n")
    _write(tmp_path, "wiki/now.md", "## Tasks\n- [ ] gas (para ~2026-08-24)\n")

    report = run_checks(tmp_path, today=date(2026, 8, 26))

    assert "wiki/areas/finanzas.md:2" in report
    assert "overdue 2 days" in report


def test_dates_that_are_not_due_dates_are_ignored(tmp_path: Path) -> None:
    """A date inside a task's parenthetical that isn't a due marker (when an
    offer was shared, when it was created) is not a deadline."""
    _write(
        tmp_path,
        "wiki/projects/jobs.md",
        "## Tasks\n- [ ] mirar oferta de Alistair (compartida por Irek, 2026-07-28)\n",
    )
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] mirar oferta de Alistair (compartida por Irek, 2026-07-28)\n",
    )

    assert run_checks(tmp_path, today=date(2026, 9, 2)) == "[no findings]"


def test_overdue_findings_are_only_reported_from_the_owning_page(tmp_path: Path) -> None:
    """now.md mirrors every task; reporting its copy too would double every finding."""
    _write(tmp_path, "wiki/areas/finanzas.md", "## Tasks\n- [ ] gas (para 2026-08-24)\n")
    _write(tmp_path, "wiki/now.md", "## Tasks\n- [ ] gas (para 2026-08-24)\n")

    report = run_checks(tmp_path, today=date(2026, 9, 2))

    assert report.startswith("1 finding.")
    assert "wiki/now.md" not in report.split("Overdue")[1]


def test_overdue_task_lapsed_since_last_compile_is_marked_new(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "wiki/areas/casa.md",
        "## Tasks\n- [ ] old one (due 2026-08-20)\n- [ ] fresh one (due 2026-09-01)\n",
    )
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] old one (due 2026-08-20)\n- [ ] fresh one (due 2026-09-01)\n",
    )
    _write(
        tmp_path,
        "wiki/log.md",
        "## [2026-08-31] compile | rebuilt\n## [2026-09-01] compile | rebuilt\n"
        "## [2026-08-30] lint | clean\n",
    )

    report = run_checks(tmp_path, today=date(2026, 9, 2))

    old, fresh = (line for line in report.splitlines() if "one (due" in line)
    assert "since the last compile" not in old
    assert "lapsed since the last compile (2026-09-01)" in fresh


# ---------------------------------------------------------------------------
# Maintenance freshness: the compile and lint entries in wiki/log.md are the
# evidence the built-in jobs succeeded. A lint that has not logged in two
# weeks (or never) is how six weeks of drift accumulated once.
# ---------------------------------------------------------------------------


def _log(root: Path, *entries: str) -> None:
    _write(root, "wiki/log.md", "# Operations log\n\n" + "".join(e + "\n" for e in entries))


def test_stale_lint_entry_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-08-02] compile | rebuilt", "## [2026-07-15] lint | clean")

    report = _check(tmp_path)

    assert "wiki/log.md" in report
    assert "lint" in report
    assert "2026-07-15" in report


def test_missing_entry_kind_is_not_a_finding(tmp_path: Path) -> None:
    """A fresh vault has a compile entry days before its first Sunday lint;
    only an entry that exists and went stale is evidence of a job failing."""
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-08-02] compile | rebuilt")

    assert _check(tmp_path) == "[no findings]"


def test_disabled_maintenance_job_is_not_checked(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-08-02] compile | rebuilt", "## [2026-07-01] lint | clean")

    assert run_checks(tmp_path, today=_TODAY, maintenance=("compile",)) == "[no findings]"


def test_log_entry_kind_is_case_insensitive(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-08-02] Compile | rebuilt", "## [2026-07-01] Lint | clean")

    report = _check(tmp_path)

    assert "compile" not in report.lower().split("maintenance")[1].split("\n")[1]
    assert "2026-07-01" in report


def test_stale_compile_entry_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-07-31] compile | rebuilt", "## [2026-08-01] lint | clean")

    report = _check(tmp_path)

    assert "wiki/log.md" in report
    assert "compile" in report
    assert "2026-07-31" in report


def test_fresh_maintenance_log_passes(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _log(tmp_path, "## [2026-08-02] compile | rebuilt", "## [2026-07-27] lint | clean")

    assert _check(tmp_path) == "[no findings]"


def test_missing_log_skips_maintenance_checks(tmp_path: Path) -> None:
    """A vault that keeps no operations log has no maintenance to verify."""
    _clean_vault(tmp_path)

    assert _check(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Page hygiene (vault_check_pages.py): what a full-wiki lint found by hand on
# 2026-09-02 — a dead link to a deleted legacy file, an empty "## Tasks",
# a duplicated bullet, placeholder Status paragraphs, and a Tasks list that
# had outgrown the archive threshold.
# ---------------------------------------------------------------------------


def test_dead_relative_link_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/hormigas.md",
        "# Hormigas\n\n**Estado:** viva.\n\nHistórico en [legacy](../../raw/legacy/log.md).\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/hormigas.md:5" in report
    assert "raw/legacy/log.md" in report


def test_resolving_and_external_links_pass(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/routines.md", "# Rutinas\n")
    _write(tmp_path, "attachments/2026-08-17-82e833.jpg", "binary")
    _write(
        tmp_path,
        "wiki/areas/familia.md",
        "# Familia\n\n**Estado:** bien.\n\n"
        "Ver [rutinas](../routines.md#tabla), [foto](../../attachments/2026-08-17-82e833.jpg), "
        "[web](https://example.com/x.md) y [arriba](#familia).\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_dead_index_link_is_reported_once(tmp_path: Path) -> None:
    """The index check already reports a missing page; the general link check
    must not double it."""
    _indexed_clean_vault(tmp_path)
    _index(
        tmp_path,
        "- [now](now.md) — dashboard",
        "- [garden](projects/garden.md) — the garden",
        "- [ghost](projects/ghost.md) — gone",
    )

    assert _check(tmp_path).startswith("1 finding.")


def test_links_inside_code_fences_are_ignored(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/projects/template.md",
        "# Template\n\n```markdown\nSee [example](missing/page.md)\n```\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_empty_section_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/deporte.md",
        "# Deporte\n\n**Estado:** activo.\n\n## Tasks\n\n## Historial\n- 2026-08-10 corrió\n\n## Notas\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/deporte.md:5" in report
    assert "wiki/areas/deporte.md:10" in report
    assert "empty section" in report
    assert "wiki/areas/deporte.md:7" not in report


def test_section_holding_only_subsections_is_not_empty(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/projects/jobs.md",
        "# Jobs\n\n**Status:** hunting.\n\n## Empresas\n\n### Encaje alto\n- Docker\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_dashboard_sections_may_be_empty(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "# Hoy\n\n## Esperando\n\n## Tareas\n")

    assert _check(tmp_path) == "[no findings]"


def test_duplicate_bullet_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/gaming.md",
        "# Gaming\n\n**Estado:** jugando.\n\n## Pendientes\n"
        "- **Cyberpunk 2077 — Phantom Liberty** (DLC) — comprado, sin jugar\n"
        "- **Kenshi** — pendiente con mods\n"
        "- **Cyberpunk 2077 — Phantom Liberty** (DLC) — comprado, sin jugar\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/gaming.md:8" in report
    assert "duplicate of line 6" in report


def test_short_repeated_bullets_are_not_duplicates(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/casa.md",
        "# Casa\n\n**Estado:** ok.\n\n## Lista\n- sí\n- no\n- sí\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_dashboard_may_repeat_a_line_across_sections(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "wiki/now.md",
        "# Hoy\n\n## Hoy\n- Cita en el SOC de Gavà a las 10:00 (código N9U5A)\n\n"
        "## Próximo\n- Cita en el SOC de Gavà a las 10:00 (código N9U5A)\n",
    )

    assert _check(tmp_path) == "[no findings]"


def _done_tasks(n: int, start: int = 0) -> str:
    return "".join(f"- [x] done thing number {i} (done 2026-07-{1 + i % 28:02d})\n" for i in range(start, start + n))


def test_more_than_fifteen_done_tasks_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/projects/jobs.md",
        "# Jobs\n\n**Status:** hunting.\n\n## Tasks\n" + _done_tasks(16),
    )

    report = _check(tmp_path)

    assert "wiki/projects/jobs.md" in report
    assert "16 done tasks" in report
    assert "Archive" in report


def test_done_tasks_under_an_archive_section_do_not_count(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/projects/jobs.md",
        "# Jobs\n\n**Status:** hunting.\n\n## Tasks\n" + _done_tasks(10)
        + "\n## Archivo\n" + _done_tasks(20, start=10),
    )

    assert _check(tmp_path) == "[no findings]"


def test_placeholder_status_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/finanzas.md",
        "# Finanzas\n\n**Estado:** Sin contenido todavía.\n\n## Tasks\n- [x] algo (hecho 2026-08-01)\n",
    )

    report = _check(tmp_path)

    assert "wiki/areas/finanzas.md:3" in report
    assert "placeholder" in report


def test_empty_status_is_reported(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/projects/kitchen.md", "# Kitchen\n\n**Status:**\n\n## Tasks\n- [x] x (done 2026-08-01)\n")

    report = _check(tmp_path)

    assert "wiki/projects/kitchen.md:3" in report
    assert "empty" in report


def test_real_status_paragraphs_pass(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/finanzas.md",
        "# Finanzas\n\n**Estado:** Luz y teléfono con Pepephone; pendiente valorar el gas.\n",
    )
    _write(
        tmp_path,
        "wiki/projects/framer.md",
        "**Estado:** Pendiente de respuesta de Framer tras la entrevista del 2026-07-29.\n",
    )
    _write(tmp_path, "wiki/people/eric.md", "# Eric\n\n**Relación:** Hijo de Albert.\n")

    assert _check(tmp_path) == "[no findings]"


def test_page_without_a_status_label_is_not_flagged(tmp_path: Path) -> None:
    """A template or scratch page stashed under projects/ carries no Status;
    only pages that have the label are judged, so it cannot nag forever."""
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/projects/coverletter.md", "# Cover letter template\n\nYou are helping...\n")

    assert _check(tmp_path) == "[no findings]"


def test_any_single_word_marker_before_a_date_is_a_due_marker(tmp_path: Path) -> None:
    """Vaults localize the due keyword freely (pour, bis, entro…); the schema
    pins the shape — one word, then the date — not the word."""
    _write(tmp_path, "wiki/areas/maison.md", "## Tasks\n- [ ] gaz (pour 2026-08-24)\n")
    _write(tmp_path, "wiki/now.md", "## Tasks\n- [ ] gaz (pour 2026-08-24)\n")

    report = run_checks(tmp_path, today=date(2026, 9, 2))

    assert "wiki/areas/maison.md:2" in report
    assert "overdue 9 days" in report


def test_vault_absolute_link_resolves_from_the_vault_root(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/casa.md",
        "# Casa\n\n**Estado:** ok.\n\nVer [dashboard](/wiki/now.md) y [nada](/wiki/ghost.md).\n",
    )

    report = _check(tmp_path)

    assert "/wiki/ghost.md" in report
    assert "/wiki/now.md" not in report


def test_section_holding_only_a_code_block_is_not_empty(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/areas/config.md",
        "# Config\n\n**Estado:** ok.\n\n## Snippet\n```toml\nkey = 1\n```\n\n## Notas\n- x\n",
    )

    assert _check(tmp_path) == "[no findings]"


def test_index_link_with_a_fragment_still_counts_as_indexed(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _index(
        tmp_path,
        "- [now](now.md) — dashboard",
        "- [garden](projects/garden.md#tasks) — the garden",
    )

    assert _check(tmp_path) == "[no findings]"
