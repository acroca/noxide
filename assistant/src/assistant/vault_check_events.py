"""Upcoming-event mirror, run by ``vault_check.run_checks``.

A dated event bullet on a live wiki page (``- 2026-09-08 — Inicio de curso``)
that falls within the next ``EVENT_HORIZON_DAYS`` must appear in
``wiki/now.md`` with its date and text. A school calendar recorded on an
area page sat there for three weeks without one nightly compile surfacing
its first day on the dashboard; the ingest that then heard "tomorrow is the
first day of school" read only the dashboard, took the fact as new, and
announced it as recorded (2026-09-07). The task mirror catches the same
failure shape for tasks — this is its twin for events.

Ranges count by their start date, an approximate date (``~2026-09-08``) is
read like a firm one, and past-dated bullets (History, Decisions) are not
events — a Decisions bullet dated *today* is the accepted residual, since
nothing but its section tells it from an event. Text is compared with
``[reminder:…]`` markers dropped and links reduced to their text, so a
marker on the page bullet or a link re-rooted for ``now.md`` never breaks
the pairing.

A ``now.md`` line mirrors an event when it carries the event's text and the
event's date *as a label* — leading (``- 2026-09-08 — …``) or parenthesized
(``- Tuesday — … (2026-09-08)``) — never a date mentioned mid-sentence in a
**Last 7 days** bullet or a task's ``(due …)`` marker. An event dated today
is instead matched by text against the section whose ``##`` heading carries
today's date (**Today**, whose lines have no date of their own); without
such a heading, against any line that carries no date at all. Lines inside
fenced code blocks are skipped on both sides.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .vault_check_pages import _NOW_PATH, _content_lines, _heading_level, _iso_date, _normalize

EVENT_HORIZON_DAYS = 14

_EVENTS_HEADING = "Upcoming events (wiki/now.md vs wiki pages)"

_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"(?:\s+\d{2}:\d{2})?"
# "- 2026-08-10 — text", "- ~2026-08-10 18:00 — text",
# "- 2026-08-15 → 2026-08-30 — text". Task checkboxes and bold journal dates
# start with "[" or "**", so they never match.
_EVENT_RX = re.compile(
    rf"^\s*- (?P<when>~?(?P<start>{_DATE}){_TIME}(?:\s*→\s*~?{_DATE}{_TIME})?)"
    rf"\s*[—–:-]\s*(?P<text>\S.*?)\s*$"
)
_DATE_RX = re.compile(_DATE)
_LEADING_DATE_RX = re.compile(rf"^\s*- ~?(?P<date>{_DATE})")
_PAREN_DATE_RX = re.compile(rf"\((?P<date>{_DATE})\)")
_REMINDER_RX = re.compile(r"\[reminder:[0-9a-f]{8}\]")
_MD_LINK_RX = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")


def _plain(text: str) -> str:
    """Normalized text with reminder markers dropped and links reduced to their text."""
    return _normalize(_MD_LINK_RX.sub(r"\1", _REMINDER_RX.sub("", text)))


def _label_dates(line: str) -> set[str]:
    """ISO dates a now.md line carries as its label (leading or parenthesized)."""
    dates = {m.group("date") for m in _PAREN_DATE_RX.finditer(line)}
    if m := _LEADING_DATE_RX.match(line):
        dates.add(m.group("date"))
    return dates


def _today_section(lines: list[tuple[int, str]], today: date) -> set[int] | None:
    """Line numbers under the ``##`` heading carrying today's date; None when absent."""
    iso = today.isoformat()
    inside = False
    found = False
    numbers: set[int] = set()
    for i, line in lines:
        level = _heading_level(line)
        if level and level <= 2:
            inside = level == 2 and iso in line
            found = found or inside
            continue
        if inside:
            numbers.add(i)
    return numbers if found else None


def _page_events(
    wiki_files: list[tuple[str, list[str]]], today: date
) -> tuple[list[tuple[str, int, date, str, str]], list[str] | None]:
    """Events within the horizon as (path, line, start, when, text), plus now.md's lines."""
    horizon_end = today + timedelta(days=EVENT_HORIZON_DAYS)
    events = []
    now_lines: list[str] | None = None
    for rel, lines in wiki_files:
        if rel == _NOW_PATH:
            now_lines = lines
            continue
        if rel.startswith("wiki/archive/"):
            continue
        for i, line in _content_lines(lines):
            m = _EVENT_RX.match(line)
            if m is None:
                continue
            start = _iso_date(m.group("start"))
            if start is None or not today <= start <= horizon_end:
                continue
            events.append((rel, i, start, m.group("when"), m.group("text")))
    return events, now_lines


def event_findings(wiki_files: list[tuple[str, list[str]]], today: date) -> list[tuple[str, str]]:
    """Events within the horizon that no ``now.md`` line mirrors."""
    events, now_lines = _page_events(wiki_files, today)
    if not events:
        return []
    if now_lines is None:
        return [(_EVENTS_HEADING, f"{_NOW_PATH} not found — upcoming events cannot be verified")]

    content = _content_lines(now_lines)
    today_lines = _today_section(content, today)
    mirror = []  # (plain text, label dates, counts as a Today line)
    for i, line in content:
        is_today_line = i in today_lines if today_lines is not None else not _DATE_RX.search(line)
        mirror.append((_plain(line), _label_dates(line), is_today_line))

    findings = []
    for rel, i, start, when, text in events:
        needle = _plain(text)
        iso = start.isoformat()
        if any(
            needle in plain and (iso in dates or (start == today and is_today_line))
            for plain, dates, is_today_line in mirror
        ):
            continue
        findings.append((
            _EVENTS_HEADING,
            f"{rel}:{i}: event within {EVENT_HORIZON_DAYS} days not mirrored in "
            f'{_NOW_PATH}: "{when} — {text}"',
        ))
    return findings
