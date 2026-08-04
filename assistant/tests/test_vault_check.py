"""Tests for the deterministic vault consistency checks."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.vault_check import run_checks


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
    assert run_checks(tmp_path) == "[no findings]"


def test_open_task_missing_from_mirror_is_reported_with_location(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/health.md", "# Health\n\n## Tasks\n- [ ] book dentist\n")

    report = run_checks(tmp_path)

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

    report = run_checks(tmp_path)

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

    assert run_checks(tmp_path) == "[no findings]"


def test_prose_mention_in_now_md_is_not_a_mirror_line(tmp_path: Path) -> None:
    """A task named in a Last-7-days bullet (or any prose) is not mirrored —
    only a checkbox line in now.md satisfies the Tasks-inventory invariant."""
    _write(tmp_path, "wiki/areas/health.md", "## Tasks\n- [ ] book dentist\n")
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n\n## Last 7 days\n- 2026-08-02: decided to book dentist soon\n",
    )

    report = run_checks(tmp_path)

    assert "wiki/areas/health.md:2" in report
    assert "book dentist" in report


def test_done_tasks_need_no_mirror(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/projects/attic.md", "## Tasks\n- [x] clear attic (done 2026-08-01)\n")

    assert run_checks(tmp_path) == "[no findings]"


def test_archived_pages_are_exempt_from_the_mirror(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(
        tmp_path,
        "wiki/archive/projects/old.md",
        "## Tasks\n- [ ] never finished, deliberately dropped\n",
    )

    assert run_checks(tmp_path) == "[no findings]"


def test_missing_now_md_is_one_finding_not_one_per_task(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/projects/a.md", "## Tasks\n- [ ] first\n")
    _write(tmp_path, "wiki/projects/b.md", "## Tasks\n- [ ] second\n")

    report = run_checks(tmp_path)

    assert "wiki/now.md" in report
    assert "first" not in report
    assert "second" not in report


def test_empty_vault_reports_no_findings(tmp_path: Path) -> None:
    assert run_checks(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Weekday labels: a weekday written beside an ISO date must match it
# ---------------------------------------------------------------------------


def test_wrong_weekday_beside_date_is_reported(tmp_path: Path) -> None:
    # 2026-08-04 is a Tuesday.
    _write(tmp_path, "wiki/now.md", "# Now — Monday — 2026-08-04\n")

    report = run_checks(tmp_path)

    assert "wiki/now.md:1" in report
    assert "Monday" in report
    assert "Tuesday" in report


def test_correct_weekday_beside_date_passes(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "# Now — Tuesday — 2026-08-04\n")

    assert run_checks(tmp_path) == "[no findings]"


def test_weekday_after_the_date_is_also_checked(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/projects/trip.md", "Flight home 2026-08-09 (Saturday)\n")

    report = run_checks(tmp_path)

    assert "wiki/projects/trip.md:1" in report
    assert "Sunday" in report


def test_localized_weekday_names_are_checked(tmp_path: Path) -> None:
    # 2026-08-05 is a Wednesday — "dimarts" (Catalan Tuesday) is wrong,
    # and the correction is offered in the same language.
    _write(tmp_path, "wiki/areas/family.md", "Sopar dimarts 2026-08-05\n")

    report = run_checks(tmp_path)

    assert "wiki/areas/family.md:1" in report
    assert "dimecres" in report


def test_weekday_far_from_a_date_is_not_paired_with_it(tmp_path: Path) -> None:
    """'Monday' names the standup day, not the due date — no pairing across
    intervening words."""
    _write(
        tmp_path,
        "wiki/projects/work.md",
        "## Tasks\n- [ ] prepare Monday standup notes (due 2026-08-01)\n",
    )
    _write(
        tmp_path,
        "wiki/now.md",
        "## Tasks\n- [ ] prepare Monday standup notes (due 2026-08-01)\n",
    )

    assert run_checks(tmp_path) == "[no findings]"


def test_invalid_calendar_date_beside_weekday_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "Review on Monday — 2026-02-30\n")

    report = run_checks(tmp_path)

    assert "wiki/now.md:1" in report
    assert "2026-02-30" in report


def test_append_only_files_are_out_of_scope(tmp_path: Path) -> None:
    """Journal, system files and wiki/log.md are append-only or bot-managed:
    a retroactive finding there would nag forever with no fix allowed."""
    _clean_vault(tmp_path)
    _write(tmp_path, "raw/journal/2026-08-01.md", "- 09:00 met Ana on Monday 2026-08-04\n")
    _write(tmp_path, "system/schedule.md", "| job | Monday 2026-08-04 |\n")
    _write(tmp_path, "wiki/log.md", "## [2026-08-02] lint | flagged Monday 2026-08-04 clash\n")

    assert run_checks(tmp_path) == "[no findings]"


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_counts_findings(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/health.md", "## Tasks\n- [ ] book dentist\n")
    _write(tmp_path, "wiki/projects/trip.md", "Flight 2026-08-09 (Saturday)\n")

    report = run_checks(tmp_path)

    assert report.startswith("2 findings")


def test_report_caps_findings_and_says_so(tmp_path: Path) -> None:
    _write(tmp_path, "wiki/now.md", "## Tasks\n")
    lines = "\n".join(f"- [ ] task number {i}" for i in range(120))
    _write(tmp_path, "wiki/projects/big.md", f"## Tasks\n{lines}\n")

    report = run_checks(tmp_path)

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

    report = run_checks(tmp_path)

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

    assert run_checks(tmp_path) == "[no findings]"


def test_recurring_jobs_need_no_marker(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _recurring_row("11223344"))

    assert run_checks(tmp_path) == "[no findings]"


def test_stale_marker_is_reported_with_location(tmp_path: Path) -> None:
    _clean_vault(tmp_path)
    _schedule(tmp_path, _one_off_row("ab12cd34"))
    _write(
        tmp_path,
        "wiki/areas/salud.md",
        "# Salud\n\nVisita [reminder:ab12cd34]\n\nOtra cosa [reminder:deadbeef]\n",
    )

    report = run_checks(tmp_path)

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

    assert run_checks(tmp_path) == "[no findings]"


def test_no_schedule_file_skips_reminder_checks(tmp_path: Path) -> None:
    """Scheduling may be disabled; a marker cannot be judged stale then."""
    _clean_vault(tmp_path)
    _write(tmp_path, "wiki/areas/salud.md", "# Salud\n\nVisita [reminder:deadbeef]\n")

    assert run_checks(tmp_path) == "[no findings]"
