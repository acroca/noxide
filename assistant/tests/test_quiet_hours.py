"""Quiet hours: when a proactive push is held, and until when."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from assistant.quiet_hours import parse_window, window_end

TZ = ZoneInfo("Europe/Madrid")


def at(hh: int, mm: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=TZ)


def test_parse_window_reads_start_and_end() -> None:
    assert parse_window("23:00-07:30") == (time(23, 0), time(7, 30))
    assert parse_window(" 13:00 - 14:15 ") == (time(13, 0), time(14, 15))


@pytest.mark.parametrize("spec", ["", "23:00", "25:00-07:30", "23:00-07:60", "7-8", "23:00–07:30", "23:00-23:00"])
def test_parse_window_rejects_bad_or_empty_specs(spec: str) -> None:
    assert parse_window(spec) is None


def test_overnight_window_end_is_the_next_end_time() -> None:
    window = parse_window("23:00-07:30")
    assert window_end(at(3, 1), window) == at(7, 30)
    assert window_end(at(23, 30), window) == at(7, 30, day=24)


def test_outside_the_window_there_is_no_end() -> None:
    window = parse_window("23:00-07:30")
    assert window_end(at(7, 30), window) is None
    assert window_end(at(12, 0), window) is None
    assert window_end(at(22, 59), window) is None


def test_same_day_window() -> None:
    window = parse_window("13:00-14:00")
    assert window_end(at(13, 30), window) == at(14, 0)
    assert window_end(at(14, 0), window) is None
    assert window_end(at(3, 0), window) is None
