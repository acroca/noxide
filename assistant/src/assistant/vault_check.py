"""Deterministic vault consistency checks.

Mechanical invariants from ``prompts/wiki.md`` that a model sweep enforces
unreliably — a model samples where these checks enumerate, and it computes
weekdays by arithmetic it is explicitly told not to trust. Each check returns
``path:line`` findings for the agent to fix; the module never writes to the
vault.

Scope is ``wiki/`` minus ``wiki/log.md``: ``raw/`` and ``system/`` are
append-only or bot-managed, and ``log.md`` is append-only like the journal —
a finding in any of them would nag forever with no fix allowed. Archived
pages (``wiki/archive/``) stay in scope for the weekday check (they remain
editable, so a wrong label there is fixable) but are exempt from the task
mirror, which covers live work only. The reminder-marker check additionally
*reads* ``system/schedule.md`` as input (the pending-job list) — findings
still point at wiki pages or at the schedule row to fix.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from pathlib import Path

_MAX_FINDINGS = 100

_NOW_PATH = "wiki/now.md"

# Weekday names per language, Monday-first to match date.weekday(). The
# canonical spelling (used in corrections) comes first; extra lookup-only
# spellings (unaccented) follow in _EXTRA_SPELLINGS.
_WEEKDAY_NAMES: dict[str, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "ca": ("dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
}
_EXTRA_SPELLINGS: dict[str, tuple[str, int]] = {
    "miercoles": ("es", 2),
    "sabado": ("es", 5),
}

# name (lowercased) → (language, Monday-first index)
_WEEKDAY_LOOKUP: dict[str, tuple[str, int]] = {
    name.lower(): (lang, i)
    for lang, names in _WEEKDAY_NAMES.items()
    for i, name in enumerate(names)
} | _EXTRA_SPELLINGS

_NAMES_ALT = "|".join(sorted(_WEEKDAY_LOOKUP, key=len, reverse=True))
# Punctuation/whitespace only — intervening words ("Monday standup notes,
# due 2026-08-01") mean the weekday does not label that date.
_SEP = r"[ \t,.:;()\[\]—–\-→]{1,10}"
_DATE = r"\d{4}-\d{2}-\d{2}"
_WEEKDAY_THEN_DATE = re.compile(
    rf"\b(?P<wd>{_NAMES_ALT})\b{_SEP}(?P<date>{_DATE})", re.IGNORECASE
)
_DATE_THEN_WEEKDAY = re.compile(
    rf"(?P<date>{_DATE}){_SEP}\b(?P<wd>{_NAMES_ALT})\b", re.IGNORECASE
)

_OPEN_TASK = re.compile(r"^\s*- \[ \]\s+(?P<text>.+?)\s*$")


def run_checks(root: Path) -> str:
    """Run every check over the vault at *root* and return a findings report.

    Returns the sentinel ``"[no findings]"`` when the vault is consistent.
    """
    wiki_files = _wiki_files(root)
    findings = (
        _mirror_findings(root, wiki_files)
        + _weekday_findings(wiki_files)
        + _reminder_findings(root, wiki_files)
        + _schedule_hygiene_findings(root)
        + _routine_findings(root)
        + _version_token_findings(wiki_files)
    )
    if not findings:
        return "[no findings]"

    total = len(findings)
    lines = [f"{total} findings." if total > 1 else "1 finding."]
    for heading, items in _grouped(findings[:_MAX_FINDINGS]):
        lines.append("")
        lines.append(f"{heading}:")
        lines.extend(f"- {item}" for item in items)
    if total > _MAX_FINDINGS:
        lines.append(f"... (truncated at {_MAX_FINDINGS} of {total} findings)")
    return "\n".join(lines)


def _grouped(findings: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Group (heading, finding) pairs by heading, preserving order."""
    groups: dict[str, list[str]] = {}
    for heading, item in findings:
        groups.setdefault(heading, []).append(item)
    return list(groups.items())


def _wiki_files(root: Path) -> list[tuple[str, list[str]]]:
    """(vault-relative path, lines) for every live wiki page, sorted by path."""
    files = []
    for p in sorted(root.glob("wiki/**/*.md")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel == "wiki/log.md":
            continue
        try:
            files.append((rel, p.read_text(encoding="utf-8").splitlines()))
        except OSError:
            continue
    return files


# ---------------------------------------------------------------------------
# Task mirror: every open task on a live wiki page appears in wiki/now.md,
# and every wiki/now.md checkbox line corresponds to an open task somewhere.
# ---------------------------------------------------------------------------

_MIRROR_HEADING = "Task mirror (wiki/now.md vs wiki pages)"


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _mirror_findings(root: Path, wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    tasks: list[tuple[str, int, str]] = []  # (rel path, line no, text)
    now_lines: list[str] | None = None
    for rel, lines in wiki_files:
        if rel == _NOW_PATH:
            now_lines = lines
            continue
        if rel.startswith("wiki/archive/"):
            continue
        for i, line in enumerate(lines, 1):
            if m := _OPEN_TASK.match(line):
                tasks.append((rel, i, m.group("text")))

    if now_lines is None:
        if not tasks:
            return []
        return [(
            _MIRROR_HEADING,
            f"{_NOW_PATH} not found — the Tasks mirror cannot be verified",
        )]

    # Only checkbox lines are mirror entries — a task named in now.md prose
    # (a Last-7-days bullet, an Upcoming event) does not satisfy the Tasks
    # inventory. Containment runs both ways so decoration on either side
    # (page label in the mirror, marker added on the page) does not
    # false-positive.
    findings = []
    mirror_lines = [
        (i, m.group("text"))
        for i, line in enumerate(now_lines, 1)
        if (m := _OPEN_TASK.match(line))
    ]
    mirror_texts = [_normalize(text) for _, text in mirror_lines]
    for rel, line_no, text in tasks:
        needle = _normalize(text)
        if not any(needle in mirrored for mirrored in mirror_texts):
            findings.append((
                _MIRROR_HEADING,
                f'{rel}:{line_no}: open task not mirrored in {_NOW_PATH}: "{text}"',
            ))

    task_texts = [_normalize(text) for _, _, text in tasks]
    for i, text in mirror_lines:
        mirrored = _normalize(text)
        if not any(t in mirrored or mirrored in t for t in task_texts):
            findings.append((
                _MIRROR_HEADING,
                f"{_NOW_PATH}:{i}: mirror line matches no open task on any "
                f'wiki page: "{text}"',
            ))
    return findings


# ---------------------------------------------------------------------------
# Weekday labels: a weekday name written beside an ISO date must match it.
# ---------------------------------------------------------------------------

_WEEKDAY_HEADING = "Weekday labels"


def _weekday_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        for i, line in enumerate(lines, 1):
            for rx in (_WEEKDAY_THEN_DATE, _DATE_THEN_WEEKDAY):
                for m in rx.finditer(line):
                    if issue := _check_pair(m.group("wd"), m.group("date")):
                        findings.append((_WEEKDAY_HEADING, f"{rel}:{i}: {issue}"))
    return findings


# ---------------------------------------------------------------------------
# Reminder markers: every pending one-off scheduled job leaves a
# [reminder:<id>] marker on a wiki page (so ingest trips over it right where
# it edits), and every marker references a still-pending job (a fired or
# cancelled job's marker is stale and should be removed or rescheduled).
# ---------------------------------------------------------------------------

_REMINDER_HEADING = "Reminder markers (system/schedule.md vs wiki pages)"

_SCHEDULE_PATH = "system/schedule.md"
_REMINDER_MARKER = re.compile(r"\[reminder:([0-9a-f]{8})\]")
_JOB_ID = re.compile(r"[0-9a-f]{8}")


def _schedule_rows(root: Path) -> list[tuple[int, str, str, str, str]] | None:
    """(line no, id, when, recurring, raw line) per table row; None when absent.

    Only the first three columns are parsed positionally — id, when and
    recurring cells cannot contain pipes, so prompt-cell pipes further right
    are harmless.
    """
    path = root / _SCHEDULE_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rows = []
    for i, line in enumerate(lines, 1):
        cells = line.split("|")
        if len(cells) < 4:
            continue
        job_id = cells[1].strip()
        if not _JOB_ID.fullmatch(job_id):
            continue
        rows.append((i, job_id, cells[2].strip(), cells[3].strip().lower(), line))
    return rows


def _reminder_findings(root: Path, wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    rows = _schedule_rows(root)
    if rows is None:
        # Scheduling not in use — a marker cannot be judged stale, and there
        # are no pending jobs to demand markers for.
        return []
    all_ids = {job_id for _, job_id, _, _, _ in rows}
    one_off = {job_id for _, job_id, _, recurring, _ in rows if recurring == "false"}

    findings = []
    marked: set[str] = set()
    for rel, lines in wiki_files:
        for i, line in enumerate(lines, 1):
            for job_id in _REMINDER_MARKER.findall(line):
                marked.add(job_id)
                if job_id not in all_ids:
                    findings.append((
                        _REMINDER_HEADING,
                        f"{rel}:{i}: marker [reminder:{job_id}] references no "
                        f"pending job — remove it or reschedule",
                    ))
    for job_id in sorted(one_off - marked):
        findings.append((
            _REMINDER_HEADING,
            f"{_SCHEDULE_PATH}: one-off job {job_id} has no [reminder:{job_id}] "
            f"marker on any wiki page — add one beside the item it concerns, "
            f"or cancel the job if it is obsolete",
        ))
    return findings


# ---------------------------------------------------------------------------
# Schedule hygiene: rows carrying what the schedule tool now refuses to
# create — hand-written [scheduled run] tags, restated close contract,
# numeric cron weekdays (APScheduler numbers from Monday=0, classic cron
# from Sunday=0, so a numeric day-of-week fires on the wrong day). The tool
# guards new jobs; this catches rows that predate it or were hand-edited.
# ---------------------------------------------------------------------------

_HYGIENE_HEADING = "Schedule hygiene (system/schedule.md)"

_SCHEDULED_RUN_TAG = re.compile(r"\[scheduled run\]", re.IGNORECASE)
_SILENT_SENTINEL = re.compile(r"\[silent\]", re.IGNORECASE)


def _schedule_hygiene_findings(root: Path) -> list[tuple[str, str]]:
    rows = _schedule_rows(root)
    if rows is None:
        return []
    findings = []
    for i, job_id, when, recurring, line in rows:
        # Scanning the raw line is safe: id/when/recurring cells can't
        # contain these patterns, so a hit is always in the prompt cell.
        if _SCHEDULED_RUN_TAG.search(line):
            findings.append((
                _HYGIENE_HEADING,
                f'{_SCHEDULE_PATH}:{i}: job {job_id} prompt hand-writes '
                f'"[scheduled run]" — the runner prepends it; rewrite the prompt without it',
            ))
        elif _SILENT_SENTINEL.search(line):
            findings.append((
                _HYGIENE_HEADING,
                f'{_SCHEDULE_PATH}:{i}: job {job_id} prompt restates the close '
                f'contract ("[silent]") — it applies on its own; rewrite the prompt without it',
            ))
        cron_fields = when.split()
        if recurring == "true" and len(cron_fields) == 5 and re.search(r"\d", cron_fields[4]):
            findings.append((
                _HYGIENE_HEADING,
                f"{_SCHEDULE_PATH}:{i}: job {job_id} cron day-of-week uses numbers "
                f"— write names (SUN, MON, ...): the numbering is ambiguous",
            ))
    return findings


# ---------------------------------------------------------------------------
# Routine due dates: "Next due = last done + frequency" is date arithmetic
# the compile prompt asks the model to recompute — the same arithmetic it is
# told not to trust for weekdays. Frequencies are semi-structured text;
# common en/es forms are parsed, everything else (approximate "~N", paused
# rows, missing last-done) is skipped rather than guessed at.
# ---------------------------------------------------------------------------

_ROUTINE_HEADING = "Routine due dates (wiki/routines.md)"

_ROUTINES_PATH = "wiki/routines.md"
_DAILY_RX = re.compile(r"^(?:diaria|diario|daily)$", re.IGNORECASE)
_WEEKLY_RX = re.compile(r"^(?:semanal|weekly)(?:\s*\(([^)]+)\))?$", re.IGNORECASE)
_EVERY_N_RX = re.compile(r"^(?:cada|every)\s+(\d+)(?:-(\d+))?\s+(?:días|dias|days)$", re.IGNORECASE)
_MONTHLY_RX = re.compile(r"^(?:mensual|monthly)$", re.IGNORECASE)
_CELL_DATE_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+\d{2}:\d{2})?$")


def _parse_cell_date(cell: str) -> date | None:
    m = _CELL_DATE_RX.match(cell)
    if not m:
        return None
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _expected_next(freq: str, last: date) -> tuple[date, date] | None:
    """Acceptable [earliest, latest] next-due window, or None when unparseable."""
    f = freq.strip()
    if _DAILY_RX.match(f):
        d = last + timedelta(days=1)
        return d, d
    if m := _WEEKLY_RX.match(f):
        anchor = (m.group(1) or "").strip().lower()
        if not anchor:
            d = last + timedelta(days=7)
            return d, d
        entry = _WEEKDAY_LOOKUP.get(anchor)
        if entry is None:
            return None
        # The next occurrence of the anchor weekday strictly after last done.
        d = last + timedelta(days=(entry[1] - last.weekday()) % 7 or 7)
        return d, d
    if m := _EVERY_N_RX.match(f):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo:
            return None
        return last + timedelta(days=lo), last + timedelta(days=hi)
    if _MONTHLY_RX.match(f):
        year, month = (last.year, last.month + 1) if last.month < 12 else (last.year + 1, 1)
        d = date(year, month, min(last.day, calendar.monthrange(year, month)[1]))
        return d, d
    return None


def _routine_findings(root: Path) -> list[tuple[str, str]]:
    try:
        lines = (root / _ROUTINES_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    findings = []
    for i, line in enumerate(lines, 1):
        cells = line.split("|")
        if len(cells) < 6:
            continue
        name, freq, last_cell, next_cell = (cells[j].strip() for j in range(1, 5))
        last = _parse_cell_date(last_cell)
        next_due = _parse_cell_date(next_cell)
        if last is None or next_due is None:
            continue
        window = _expected_next(freq, last)
        if window is None:
            continue
        lo, hi = window
        if lo <= next_due <= hi:
            continue
        expected = str(lo) if lo == hi else f"{lo}..{hi}"
        findings.append((
            _ROUTINE_HEADING,
            f'{_ROUTINES_PATH}:{i}: "{name}" next due {next_due} does not match '
            f"last done {last} + {freq} — expected {expected}",
        ))
    return findings


# ---------------------------------------------------------------------------
# Leaked version tokens: a whole-line "[version: ...]" in a wiki page is
# read_file tool output pasted back as content. The write tools strip the
# trailing case; this catches interior leaks and anything that predates the
# strip. Full-line matches only — prose *mentioning* the token is content.
# ---------------------------------------------------------------------------

_VERSION_TOKEN_HEADING = "Leaked version tokens"

_VERSION_TOKEN_LINE = re.compile(r"^\[version: [0-9a-f]{8}\]\s*$")


def _version_token_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    return [
        (
            _VERSION_TOKEN_HEADING,
            f"{rel}:{i}: leaked read_file version token — delete this line",
        )
        for rel, lines in wiki_files
        for i, line in enumerate(lines, 1)
        if _VERSION_TOKEN_LINE.match(line)
    ]


def _check_pair(weekday: str, iso: str) -> str | None:
    lang, claimed = _WEEKDAY_LOOKUP[weekday.lower()]
    year, month, day = (int(part) for part in iso.split("-"))
    try:
        actual = date(year, month, day).weekday()
    except ValueError:
        return f'"{weekday}" beside {iso} — not a valid calendar date'
    if claimed == actual:
        return None
    correct = _WEEKDAY_NAMES[lang][actual]
    return f'"{weekday}" beside {iso} — that date is a {correct}'
