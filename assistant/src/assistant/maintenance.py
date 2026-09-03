"""Built-in maintenance jobs: the nightly compile and the weekly lint.

They used to be ordinary rows in ``system/schedule.md`` that the user had to
create by hand; a vault once ran 41 days without a lint because its row was
never scheduled and nothing noticed. Now their crons come from config
(``[maintenance] compile`` / ``lint``, empty to disable), their prompts ship
here — one line each, pointing at the Compile/Lint procedures in
``prompts/wiki.md`` so nothing is restated — and the scheduler registers them
beside the table's jobs.

Their last successful run is persisted in ``state_dir/maintenance.json``, the
built-in equivalent of a recurring row's ``next`` column: advanced only after
a run completes, so a crash or failed run leaves the old value and
``Scheduler.catch_up()`` fires one coalesced late run at the next startup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .atomic import atomic_write_text

logger = logging.getLogger(__name__)

COMPILE_ID = "compile"
LINT_ID = "lint"
STATE_FILENAME = "maintenance.json"

COMPILE_PROMPT = (
    "Nightly compile. Run the Compile procedure from the vault instructions, "
    "every step in order, and log it."
)
LINT_PROMPT = (
    "Weekly lint. Run the Lint procedure from the vault instructions, "
    "every step in order, and log it."
)


@dataclass(frozen=True)
class BuiltinJob:
    id: str
    cron: str
    prompt: str


def builtin_jobs(compile_cron: str, lint_cron: str) -> list[BuiltinJob]:
    """The built-in jobs enabled by the configured crons (empty disables one)."""
    candidates = (
        (COMPILE_ID, compile_cron, COMPILE_PROMPT),
        (LINT_ID, lint_cron, LINT_PROMPT),
    )
    return [BuiltinJob(job_id, cron, prompt) for job_id, cron, prompt in candidates if cron]


class MaintenanceState:
    """Last successful run per built-in job, persisted as a small JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable maintenance state %s: %s", self._path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def last_success(self, job_id: str) -> datetime | None:
        value = self._data.get(job_id)
        if not isinstance(value, str):
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    def mark_success(self, job_id: str, when: datetime) -> None:
        self._data = {**self._data, job_id: when.astimezone(UTC).isoformat()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._path, json.dumps(self._data, indent=2))
        except OSError:
            # The run itself succeeded; losing the record only means one
            # extra catch-up run after the next restart.
            logger.exception("Could not persist maintenance state to %s", self._path)
