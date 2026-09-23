"""Routine check-ins: the [routine: …] prompt form and the routines.md lookup."""

from datetime import date, time, timedelta

import pytest

from assistant.routines import (
    RoutineSpec,
    fixed_time,
    is_routine_prompt,
    parse_routine_prompt,
    routine_last_done,
)

TABLE = """# Rutinas

| Rutina | Frecuencia | Última vez | Próxima | Notas |
| --- | --- | --- | --- | --- |
| Pastilla (Dacortin) | Diaria | 2026-09-23 08:30 | 2026-09-24 | 10 mg |
| Gym | 2x/semana | 2026-09-22 | 2026-09-24 | bono |
| Hormigas — semillas | Cada 2-3 días |  | 2026-09-25 | |
"""


def test_parse_full_form() -> None:
    spec = parse_routine_prompt("[routine: Pastilla (Dacortin); every 30 min; until 12:00] Tómate las pastillas")
    assert spec == RoutineSpec("Pastilla (Dacortin)", "Tómate las pastillas", timedelta(minutes=30), time(12, 0))


def test_parse_spanish_and_bare_forms() -> None:
    assert parse_routine_prompt("[routine: Gym; cada 45 min; hasta 10:15] Al gym") == RoutineSpec(
        "Gym", "Al gym", timedelta(minutes=45), time(10, 15))
    assert parse_routine_prompt("[Routine: Gym] Al gym") == RoutineSpec("Gym", "Al gym", None, None)


@pytest.mark.parametrize("prompt,problem", [
    ("[routine: ] Tómate", "name"),
    ("[routine: Gym]", "message"),
    ("[routine: Gym; every 30 min] x", "until"),
    ("[routine: Gym; every 2 min; until 12:00] x", "at least 5"),
    ("[routine: Gym; until 12:00] x", "every"),
    ("[routine: Gym; every 30 min; until 25:00] x", "until"),
    ("[routine: Gym; twice] x", "twice"),
])
def test_parse_rejects_malformed_forms_naming_the_problem(prompt: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        parse_routine_prompt(prompt)


def test_is_routine_prompt() -> None:
    assert is_routine_prompt("[routine: Gym] x") and is_routine_prompt("  [ROUTINE: x] y")
    assert not is_routine_prompt("Remind me about [routine: Gym]") and not is_routine_prompt("plain")


def test_last_done_matches_the_name_loosely_and_reads_the_date() -> None:
    assert routine_last_done(TABLE, "Pastilla (Dacortin)") == date(2026, 9, 23)
    assert routine_last_done(TABLE, "  pastilla   (dacortin) ") == date(2026, 9, 23)
    assert routine_last_done(TABLE, "Gym") == date(2026, 9, 22)


def test_last_done_distinguishes_blank_from_missing() -> None:
    assert routine_last_done(TABLE, "Hormigas — semillas") is None  # row found, never done
    with pytest.raises(LookupError):
        routine_last_done(TABLE, "Piano")
    with pytest.raises(LookupError):
        routine_last_done("[file not found: wiki/routines.md]", "Gym")


def test_fixed_time_reads_a_cron_with_numeric_minute_and_hour() -> None:
    assert fixed_time("30 8 * * *") == time(8, 30)
    assert fixed_time("0 11 * * SUN") == time(11, 0)
    assert fixed_time("*/30 8 * * *") is None and fixed_time("0 8,9 * * *") is None and fixed_time("bad") is None
