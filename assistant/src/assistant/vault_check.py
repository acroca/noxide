"""Deterministic vault consistency checks.

Mechanical invariants from ``prompts/wiki.md`` that a model sweep enforces
unreliably — a model samples where these checks enumerate, and it computes
weekdays by arithmetic it is explicitly told not to trust. Each check returns
``path:line`` findings for the agent to fix; the module never writes to the
vault.

Scope is the live wiki only: ``raw/`` and ``system/`` are append-only or
bot-managed, and ``wiki/log.md`` is append-only like the journal — a finding
in any of them would nag forever with no fix allowed.
"""

from __future__ import annotations

import re
from datetime import date
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
    findings = _mirror_findings(root, wiki_files) + _weekday_findings(wiki_files)
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

    findings = []
    normalized_now = [_normalize(line) for line in now_lines]
    for rel, line_no, text in tasks:
        needle = _normalize(text)
        if not any(needle in haystack for haystack in normalized_now):
            findings.append((
                _MIRROR_HEADING,
                f'{rel}:{line_no}: open task not mirrored in {_NOW_PATH}: "{text}"',
            ))

    # Reverse direction: a now.md checkbox no page backs is a stale mirror
    # line. Containment runs both ways so decoration on either side (page
    # label in the mirror, marker added on the page) does not false-positive.
    task_texts = [_normalize(text) for _, _, text in tasks]
    for i, line in enumerate(now_lines, 1):
        if m := _OPEN_TASK.match(line):
            mirrored = _normalize(m.group("text"))
            if not any(t in mirrored or mirrored in t for t in task_texts):
                findings.append((
                    _MIRROR_HEADING,
                    f"{_NOW_PATH}:{i}: mirror line matches no open task on any "
                    f'wiki page: "{m.group("text")}"',
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
