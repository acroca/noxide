"""Routine check-ins: reminders a string comparison can decide.

A ``system/schedule.md`` row whose prompt has the form

    [routine: <name>; every <N> min; until <HH:MM>] <message>

is run by the scheduler itself, never by the model: at the cron time it reads
``wiki/routines.md``, and unless the named row's *Last done* is today it
delivers the message, then re-pushes it every N minutes until HH:MM or until
the row is updated (the user's reply goes through the normal agent, which
does the ingest). ``every``/``until`` are optional together (a single push);
``cada``/``hasta`` are accepted for Spanish prompts. The daily medication
reminder used to be an LLM job that re-scheduled a one-off re-aviso for each
nag — up to nine Opus runs a morning, wording that drifted daily, and one
2026-08-04 morning of nagging after the pill was confirmed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

_TAG_RX = re.compile(r"^\s*\[routine:(?P<body>[^\]]*)\]\s*(?P<text>.*)$", re.IGNORECASE | re.DOTALL)
_EVERY_RX = re.compile(r"^(?:every|cada)\s+(\d+)\s*min(?:utes|utos|s)?$", re.IGNORECASE)
_UNTIL_RX = re.compile(r"^(?:until|hasta)\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)
_MIN_EVERY_MINUTES = 5
_FIXED_FIELD_RX = re.compile(r"^\d{1,2}$")
_CELL_DATE_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s+\d{2}:\d{2})?$")


@dataclass(frozen=True)
class RoutineSpec:
    name: str
    text: str
    every: timedelta | None
    until: time | None


def is_routine_prompt(prompt: str) -> bool:
    return _TAG_RX.match(prompt) is not None


def parse_routine_prompt(prompt: str) -> RoutineSpec:
    """Parse a ``[routine: …]`` prompt; ValueError names what is wrong with it."""
    m = _TAG_RX.match(prompt)
    if m is None:
        raise ValueError("not a [routine: …] prompt")
    parts = [p.strip() for p in m.group("body").split(";")]
    name, options = parts[0], parts[1:]
    if not name:
        raise ValueError("the routine name is missing: [routine: <name as in wiki/routines.md>; …]")
    text = m.group("text").strip()
    if not text:
        raise ValueError("the message to deliver is missing after the closing bracket")
    every: timedelta | None = None
    until: time | None = None
    for option in options:
        if em := _EVERY_RX.match(option):
            minutes = int(em.group(1))
            if minutes < _MIN_EVERY_MINUTES:
                raise ValueError(f"every: at least {_MIN_EVERY_MINUTES} minutes between pushes")
            every = timedelta(minutes=minutes)
        elif um := _UNTIL_RX.match(option):
            try:
                until = time(int(um.group(1)), int(um.group(2)))
            except ValueError:
                raise ValueError(f"until: {option!r} is not a valid HH:MM") from None
        else:
            raise ValueError(f"unknown option {option!r}: use 'every N min' and 'until HH:MM'")
    if (every is None) != (until is None):
        raise ValueError("'every N min' and 'until HH:MM' go together: repeats need a bound")
    return RoutineSpec(name, text, every, until)


def fixed_time(cron: str) -> time | None:
    """The clock time of a cron whose minute and hour are plain numbers, else None."""
    fields = cron.split()
    if len(fields) != 5 or not all(_FIXED_FIELD_RX.match(f) for f in fields[:2]):
        return None
    try:
        return time(int(fields[1]), int(fields[0]))
    except ValueError:
        return None


def _normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


def routine_last_done(routines_text: str, name: str) -> date | None:
    """The *Last done* date of the routines-table row named *name*.

    None when the row exists but has no parseable date; LookupError when no
    row matches (or the file is missing), so a typo in a job is not mistaken
    for a routine never done.
    """
    wanted = _normalize(name)
    for line in routines_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or _normalize(cells[0]) != wanted:
            continue
        m = _CELL_DATE_RX.match(cells[2])
        if not m:
            return None
        try:
            return date(*(int(g) for g in m.groups()))
        except ValueError:
            return None
    raise LookupError(f"no routine named {name!r} in wiki/routines.md")
