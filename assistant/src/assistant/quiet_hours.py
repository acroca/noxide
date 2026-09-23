"""Quiet hours: the local-time window in which proactive pushes are held.

Pure time arithmetic; the companion decides what to do with the answer. A
window is ``HH:MM-HH:MM`` on the user's clock and may cross midnight
(``23:00-07:30``). Only reminders are held — a reply to a message the user
just sent, and lifecycle notices, are theirs to receive whenever they arrive.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_WINDOW_RX = re.compile(r"^\s*(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})\s*$")


def parse_window(spec: str) -> tuple[time, time] | None:
    """``"23:00-07:30"`` → ``(time(23, 0), time(7, 30))``; None for empty or malformed specs."""
    m = _WINDOW_RX.match(spec or "")
    if not m:
        return None
    try:
        start, end = time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None
    if start == end:
        return None
    return start, end


def window_end(now: datetime, window: tuple[time, time] | None) -> datetime | None:
    """When the window *now* falls in ends, or None when *now* is outside it.

    ``now`` must be timezone-aware in the user's zone; the result is the next
    end time in that same zone. The end is exclusive: at exactly the end time
    the window is over.
    """
    if window is None:
        return None
    start, end = window
    clock = now.timetz().replace(tzinfo=None)
    if start < end:
        inside = start <= clock < end
        end_day = now.date()
    else:  # crosses midnight
        inside = clock >= start or clock < end
        end_day = now.date() + timedelta(days=1) if clock >= start else now.date()
    if not inside:
        return None
    return datetime.combine(end_day, end, tzinfo=now.tzinfo)
