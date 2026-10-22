"""Weekday labels, run by ``vault_check.run_checks``.

A weekday name written beside an ISO date must name that date's weekday. The
model is told never to compute weekdays by mental arithmetic, and a wrong
label beside a correct date reads as a contradiction that gets one of the two
"corrected" into being wrong. Names are known in English, Catalan and
Spanish; adjacency is punctuation-only, so "Monday standup (due 2026-08-01)"
pairs nothing. On ``wiki/now.md`` a flat bullet opening with a weekday and
closing with ``(YYYY-MM-DD)`` is paired across the text between them, the
form **Upcoming** used before its days were grouped.

**Upcoming** now groups the next 7 days by day: a bold top-level line
(``- **Saturday**``, the first one ``- **Tomorrow — Friday**``, localized as
the vault says) labels every item nested one level below it, and each item
keeps its own ``(YYYY-MM-DD)`` so it still pairs with its page. Within a 7-day
window a weekday names exactly one day — its next occurrence after today —
and the tomorrow label names today + 1, so every item's date must be that
day. When all of a group's dated items disagree with it on the same date, the
label is what is wrong and the finding goes on the group line; otherwise on
each misfiled item. Deeper bullets are notes on an item, not items.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .vault_check_pages import _NOW_PATH, _content_lines, _iso_date

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

# now.md's flat Upcoming form — "- Tuesday — text (2026-09-08) [reminder:…]":
# the weekday opens the bullet and the date closes it, with the event text in
# between. Paired only on now.md, where the prompt used to mandate this shape;
# elsewhere a leading weekday is prose ("Monday notes (2026-08-01)").
_LABELLED_LINE = re.compile(
    rf"^\s*- (?P<wd>{_NAMES_ALT})\b.*\((?P<date>{_DATE})\)(?:\s*\[reminder:[0-9a-f]{{8}}\])*\s*$",
    re.IGNORECASE,
)

# The tomorrow label per language, canonical spelling first (used when a
# finding asks for the label), then lookup-only unaccented spellings.
_TOMORROW: dict[str, tuple[str, ...]] = {
    "en": ("Tomorrow",),
    "ca": ("Demà", "dema"),
    "es": ("Mañana", "manana"),
}
_TOMORROW_ALT = "|".join(word for words in _TOMORROW.values() for word in words)
# "- **Tomorrow — Friday**", "- **Sábado**", "- **Saturday:**".
_GROUP_LINE = re.compile(
    rf"^- \*\*(?P<label>(?:(?P<tomorrow>{_TOMORROW_ALT})\s*[—–,:-]\s*)?(?P<wd>{_NAMES_ALT})):?\*\*:?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^(?P<indent>[ \t]+)[-*] ")
# An item's own day: its closing "(2026-10-24)", the start of a
# "(2026-10-24 → 2026-10-26)" range, "(~2026-10-24)", or a task's
# "(due 2026-10-24)". The last one on the line counts.
_ITEM_DATE = re.compile(rf"\((?:\w+\s+)?~?(?P<date>{_DATE})[^)]*\)")
_WINDOW_DAYS = 7

_WEEKDAY_HEADING = "Weekday labels"
_GROUP_HEADING = "Upcoming day groups (wiki/now.md)"


def weekday_findings(
    wiki_files: list[tuple[str, list[str]]], today: date
) -> list[tuple[str, str]]:
    """Weekday names beside ISO dates that name another day, and now.md items
    grouped under a day they are not dated on."""
    findings = []
    for rel, lines in wiki_files:
        content = _content_lines(lines)
        for i, line in content:
            for weekday, iso in _weekday_pairs(rel, line):
                if issue := _check_pair(weekday, iso):
                    findings.append((_WEEKDAY_HEADING, f"{rel}:{i}: {issue}"))
        if rel == _NOW_PATH:
            findings.extend((_GROUP_HEADING, f) for f in _group_findings(content, today))
    return findings


def _day_groups(
    content: list[tuple[int, str]],
) -> list[tuple[int, re.Match[str], list[tuple[int, str]]]]:
    """(line, label match, items) for every day group; items are the bullets
    nested one level below the label, until the next unindented line."""
    groups: list[tuple[int, re.Match[str], list[tuple[int, str]]]] = []
    items: list[tuple[int, str]] | None = None
    item_indent = 0
    for i, line in content:
        if m := _GROUP_LINE.match(line):
            items = []
            groups.append((i, m, items))
            continue
        if items is None or not line.strip():
            continue
        bullet = _BULLET.match(line)
        if not line[0].isspace():
            items = None
        elif bullet and (not items or len(bullet["indent"]) <= item_indent):
            item_indent = len(bullet["indent"])
            items.append((i, line))
    return groups


def _group_findings(content: list[tuple[int, str]], today: date) -> list[str]:
    tomorrow = today + timedelta(days=1)
    findings = []
    for i, m, items in _day_groups(content):
        label = m["label"]
        lang, weekday = _WEEKDAY_LOOKUP[m["wd"].lower()]
        day = today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
        if m["tomorrow"] and day != tomorrow:
            findings.append(
                f'{_NOW_PATH}:{i}: "{label}" — tomorrow is {tomorrow.isoformat()}, '
                f"a {_WEEKDAY_NAMES[lang][tomorrow.weekday()]}"
            )
            continue
        if not m["tomorrow"] and day == tomorrow:
            findings.append(
                f'{_NOW_PATH}:{i}: "{label}" is tomorrow ({day.isoformat()}) — '
                f'label it "{_TOMORROW[lang][0]} — {m["wd"]}"'
            )
        findings.extend(_item_findings(i, m, lang, day, items, today))
    return findings


# What to do about an item dated off its group's day: (one item, the whole group).
_FIXES = {
    "day": ("move it under its own day", "relabel the group"),
    "beyond": (
        "list it flat below the groups, date up front",
        "list them flat below the groups, date up front",
    ),
    "past": (
        "it belongs under Today, or nowhere once past",
        "they belong under Today, or nowhere once past",
    ),
    "invalid": ("fix the date", "fix the dates"),
}


def _item_findings(
    group_line: int,
    m: re.Match[str],
    lang: str,
    day: date,
    items: list[tuple[int, str]],
    today: date,
) -> list[str]:
    label = m["label"]
    findings = []
    dated = []
    for i, line in items:
        if dates := _ITEM_DATE.findall(line):
            dated.append((i, dates[-1]))
        else:
            findings.append(
                f'{_NOW_PATH}:{i}: item under "{label}" has no (YYYY-MM-DD) — keep its ISO '
                "date at the end so it pairs with its page"
            )
    wrong = [(i, iso) for i, iso in dated if iso != day.isoformat()]
    if wrong and len(wrong) == len(dated) and len({iso for _, iso in wrong}) == 1:
        iso = wrong[0][1]
        what, kind = _misdated(iso, lang, today)
        findings.append(
            f'{_NOW_PATH}:{group_line}: "{label}" is {day.isoformat()} but heads items '
            f"dated {iso}{what} — {_FIXES[kind][1]}"
        )
        return findings
    for i, iso in wrong:
        what, kind = _misdated(iso, lang, today)
        findings.append(
            f'{_NOW_PATH}:{i}: item dated {iso}{what}, is filed under "{label}" '
            f"({day.isoformat()}) — {_FIXES[kind][0]}"
        )
    return findings


def _misdated(iso: str, lang: str, today: date) -> tuple[str, str]:
    """How a date its group's label does not name reads, and which fix applies."""
    d = _iso_date(iso)
    if d is None:
        return ", not a valid calendar date", "invalid"
    if d <= today:
        return ", today or earlier", "past"
    if d > today + timedelta(days=_WINDOW_DAYS):
        return f", beyond the next {_WINDOW_DAYS} days", "beyond"
    return f", a {_WEEKDAY_NAMES[lang][d.weekday()]}", "day"


def _weekday_pairs(rel: str, line: str) -> list[tuple[str, str]]:
    """(weekday, ISO date) pairs the line claims — adjacent anywhere, or, on
    now.md, a bullet opening with the weekday and closing with the date."""
    pairs = [
        (m.group("wd"), m.group("date"))
        for rx in (_WEEKDAY_THEN_DATE, _DATE_THEN_WEEKDAY)
        for m in rx.finditer(line)
    ]
    if not pairs and rel == _NOW_PATH and (m := _LABELLED_LINE.match(line)):
        pairs.append((m.group("wd"), m.group("date")))
    return pairs


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
