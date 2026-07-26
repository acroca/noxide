"""Scheduling: APScheduler backed by vault/system/schedule.md as source of truth.

The schedule.md file is a Markdown table:

| id | when | recurring | prompt | created |
|----|------|-----------|--------|---------|
| abc123 | 2024-01-01T09:00:00 | false | Send reminder | 2024-01-01T08:00:00 |
| xyz | 0 8 * * * | true | Morning brief | 2024-01-01T08:00:00 |

On startup, parse the file and register jobs with APScheduler (in-memory).
The schedule tools edit this file AND the live scheduler.
A watchfiles watcher (or 60s poll) reloads on change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)

_SCHEDULE_FILE = "system/schedule.md"
_TABLE_HEADER = "| id | when | recurring | prompt | created |"
_TABLE_SEP = "|-----|------|-----------|--------|---------|"
_MISFIRE_GRACE = 12 * 3600  # 12 hours in seconds
# Cell boundaries are bare pipes; an escaped \| belongs to the prompt text.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


class ScheduleEntry:
    __slots__ = ("id", "when", "recurring", "prompt", "created")

    def __init__(
        self,
        id: str,
        when: str,
        recurring: bool,
        prompt: str,
        created: str,
    ) -> None:
        self.id = id
        self.when = when
        self.recurring = recurring
        self.prompt = prompt
        self.created = created

    def to_row(self) -> str:
        rec = "true" if self.recurring else "false"
        # Escape pipes in prompt
        prompt_safe = self.prompt.replace("|", "\\|")
        return f"| {self.id} | {self.when} | {rec} | {prompt_safe} | {self.created} |"

    @classmethod
    def from_row(cls, row: str) -> ScheduleEntry | None:
        """Parse a Markdown table row. Returns None if malformed."""
        cols = [c.strip() for c in _CELL_SPLIT_RE.split(row.strip())]
        # `| a | b |` splits to an empty element at each end, from the outer
        # pipes. Drop exactly those two — an interior blank is a real column,
        # and collapsing it would shift every later field one place left.
        if cols and not cols[0]:
            cols.pop(0)
        if cols and not cols[-1]:
            cols.pop()

        if len(cols) < 5:
            return None
        id_, when, rec, prompt, created = cols[:5]
        if not id_ or not when:
            return None
        # Unescape pipes
        prompt = prompt.replace("\\|", "|")
        return cls(id=id_, when=when, recurring=(rec.lower() == "true"), prompt=prompt, created=created)


def _generate_id() -> str:
    return secrets.token_hex(4)


def parse_when(when: str, tz_name: str = "UTC") -> datetime | None:
    """Parse an ISO datetime or relative expression ('in 10 minutes', 'tomorrow at 9am').

    Returns a UTC-aware datetime, or None if parsing fails.
    """
    settings: dict[str, Any] = {
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": tz_name,
        "TO_TIMEZONE": "UTC",
    }
    result = dateparser.parse(when, settings=settings)
    return result


class Scheduler:
    """Wraps APScheduler and the schedule.md file."""

    def __init__(
        self,
        vault_tools: Any,  # VaultTools instance
        run_job_fn: Callable[[str], Coroutine[Any, Any, None]],
        tz_name: str = "UTC",
    ) -> None:
        self._vault = vault_tools
        self._run_job = run_job_fn
        self._tz = tz_name
        self._apscheduler = AsyncIOScheduler(timezone=tz_name)
        self._entries: dict[str, ScheduleEntry] = {}
        # Jobs run outside the Telegram update queue, so shutdown has to wait
        # on them separately or a mid-run reminder dies with the process.
        self._inflight: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._apscheduler.start()

    async def drain(self, timeout: float) -> int:
        """Stop firing new jobs, then wait for in-flight ones.

        Returns the number of jobs still unfinished when `timeout` expired
        (0 when everything completed, which is the common idle case).
        """
        # SchedulerNotRunningError when shutdown races a failed startup
        with contextlib.suppress(Exception):
            self._apscheduler.shutdown(wait=False)
            # AsyncIOScheduler._shutdown is @run_in_event_loop, so it only takes
            # effect on the next tick — yield, or jobs can still fire under us.
            await asyncio.sleep(0)
        if not self._inflight:
            return 0
        logger.info("Waiting for %d in-flight scheduled job(s)", len(self._inflight))
        _, pending = await asyncio.wait(set(self._inflight), timeout=timeout)
        return len(pending)

    # ------------------------------------------------------------------
    # schedule.md I/O
    # ------------------------------------------------------------------

    def _read_entries(self) -> list[ScheduleEntry]:
        text = self._vault.read_file(_SCHEDULE_FILE)
        if text.startswith("[file not found"):
            return []
        entries = []
        in_table = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("| id |"):
                in_table = True
                continue
            if in_table and stripped.startswith("|---"):
                continue
            if in_table and stripped.startswith("|"):
                entry = ScheduleEntry.from_row(stripped)
                if entry:
                    entries.append(entry)
                else:
                    # The file is hand-editable, so a typo here is a job that
                    # silently never runs. Say so rather than dropping it mutely.
                    logger.warning(
                        "Ignoring unparseable row in %s (needs 5 columns: "
                        "id | when | recurring | prompt | created): %s",
                        _SCHEDULE_FILE,
                        stripped,
                    )
            elif in_table and not stripped:
                pass  # allow blank lines
        return entries

    def _write_entries(self, entries: list[ScheduleEntry]) -> None:
        lines = [
            "# Schedule\n",
            _TABLE_HEADER,
            _TABLE_SEP,
        ]
        for e in entries:
            lines.append(e.to_row())
        lines.append("")  # trailing newline
        self._vault.write_file(_SCHEDULE_FILE, "\n".join(lines))

    # ------------------------------------------------------------------
    # APScheduler registration
    # ------------------------------------------------------------------

    def _register(self, entry: ScheduleEntry) -> None:
        """Add entry to APScheduler (idempotent)."""
        job_id = entry.id
        # Remove stale job first
        existing = self._apscheduler.get_job(job_id)
        if existing:
            existing.remove()

        if entry.recurring:
            trigger = CronTrigger.from_crontab(entry.when, timezone=self._tz)
        else:
            dt = parse_when(entry.when, self._tz)
            if dt is None:
                logger.warning("Cannot parse 'when' for job %s: %r", job_id, entry.when)
                return
            now = datetime.now(tz=UTC)
            age = (now - dt).total_seconds()
            if age > _MISFIRE_GRACE:
                logger.info("Dropping stale job %s (overdue by %.0fs)", job_id, age)
                return
            trigger = DateTrigger(run_date=dt)

        self._apscheduler.add_job(
            self._fire,
            trigger=trigger,
            id=job_id,
            args=[entry.id, entry.prompt, entry.recurring],
            misfire_grace_time=_MISFIRE_GRACE,
            replace_existing=True,
        )
        logger.info("Registered job %s (%s) at %r", job_id, "recurring" if entry.recurring else "one-off", entry.when)

    async def _fire(self, job_id: str, prompt: str, recurring: bool) -> None:
        logger.info("Firing job %s: %r", job_id, prompt)
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        try:
            await self._run_job(prompt)
        except Exception:
            logger.exception("Error in scheduled job %s", job_id)
        finally:
            if task is not None:
                self._inflight.discard(task)
        if not recurring:
            self._remove_entry(job_id)

    def _remove_entry(self, job_id: str) -> None:
        entries = self._read_entries()
        entries = [e for e in entries if e.id != job_id]
        self._write_entries(entries)
        self._entries.pop(job_id, None)

    # ------------------------------------------------------------------
    # Startup reload
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Parse schedule.md and sync APScheduler. Safe to call multiple times."""
        entries = self._read_entries()
        seen_ids: set[str] = set()
        for entry in entries:
            seen_ids.add(entry.id)
            prev = self._entries.get(entry.id)
            unchanged = prev is not None and (
                (prev.when, prev.recurring, prev.prompt)
                == (entry.when, entry.recurring, entry.prompt)
            )
            if unchanged and self._apscheduler.get_job(entry.id):
                continue
            self._entries[entry.id] = entry
            self._register(entry)
        # Remove jobs no longer in file
        for jid in list(self._entries.keys()):
            if jid not in seen_ids:
                job = self._apscheduler.get_job(jid)
                if job:
                    job.remove()
                self._entries.pop(jid)

    # ------------------------------------------------------------------
    # Tool interface
    # ------------------------------------------------------------------

    def schedule(self, when: str, prompt: str, recurring: bool) -> str:
        """Create a new scheduled job. Returns the job id."""
        job_id = _generate_id()
        now_iso = datetime.now(tz=UTC).isoformat()

        # For one-off jobs, normalize 'when' to ISO
        if not recurring:
            dt = parse_when(when, self._tz)
            if dt is None:
                return f"[error: could not parse time: {when!r}]"
            when_stored = dt.isoformat()
        else:
            when_stored = when  # store cron expression as-is

        entry = ScheduleEntry(
            id=job_id,
            when=when_stored,
            recurring=recurring,
            prompt=prompt,
            created=now_iso,
        )
        entries = self._read_entries()
        entries.append(entry)
        self._write_entries(entries)
        self._entries[job_id] = entry
        self._register(entry)
        return f"Scheduled job {job_id} at {when_stored}"

    def list_scheduled(self) -> str:
        entries = self._read_entries()
        if not entries:
            return "[no scheduled jobs]"
        lines = [f"{e.id}: [{('recurring' if e.recurring else 'one-off')}] {e.when!r} → {e.prompt!r}" for e in entries]
        return "\n".join(lines)

    def cancel_scheduled(self, job_id: str) -> str:
        entries = self._read_entries()
        original_count = len(entries)
        entries = [e for e in entries if e.id != job_id]
        if len(entries) == original_count:
            return f"[job {job_id!r} not found]"
        self._write_entries(entries)
        job = self._apscheduler.get_job(job_id)
        if job:
            job.remove()
        self._entries.pop(job_id, None)
        return f"Cancelled job {job_id}"

    # ------------------------------------------------------------------
    # OpenAI tool schemas
    # ------------------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "schedule",
                    "description": (
                        "Schedule a future agent run. For one-off: when accepts ISO datetime "
                        "or relative ('in 10 minutes', 'tomorrow at 9am'). "
                        "For recurring: when is a cron expression (e.g. '0 8 * * *')."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "when": {"type": "string"},
                            "prompt": {"type": "string", "description": "The agent prompt to run at that time"},
                            "recurring": {"type": "boolean", "description": "True for cron, False for one-off"},
                        },
                        "required": ["when", "prompt", "recurring"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled",
                    "description": "List all scheduled jobs.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled",
                    "description": "Cancel a scheduled job by its id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                },
            },
        ]

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "schedule":
            return self.schedule(args["when"], args["prompt"], args.get("recurring", False))
        elif name == "list_scheduled":
            return self.list_scheduled()
        elif name == "cancel_scheduled":
            return self.cancel_scheduled(args.get("id", args.get("job_id", "")))
        return f"[unknown schedule tool: {name}]"
