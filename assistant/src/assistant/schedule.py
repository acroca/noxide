"""Scheduling: APScheduler backed by vault/system/schedule.md as source of truth.

The schedule.md file is a Markdown table:

| id | when | recurring | prompt | created | next |
|----|------|-----------|--------|---------|------|
| abc123 | 2024-01-01T09:00:00 | false | Send reminder | 2024-01-01T08:00:00 |  |
| xyz | 0 8 * * * | true | Morning brief | 2024-01-01T08:00:00 | 2024-01-02T08:00:00+00:00 |

On startup, parse the file and register jobs with APScheduler (in-memory).
The schedule tools edit this file AND the live scheduler.
A watchfiles watcher (or 60s poll) reloads on change.

`next` is bookkeeping for downtime detection, not the live trigger (APScheduler
still fires off the cron expression): it holds the next computed occurrence of
a recurring job and is advanced only after a run completes, so on startup a
`next` in the past means the service was down when the job was due —
catch_up() fires one late run for it. One-off rows leave it empty; their
`when` already is the timestamp and the row itself is deleted after the run.
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
from zoneinfo import ZoneInfo

import dateparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .copilot import CopilotUnavailableError

logger = logging.getLogger(__name__)

_SCHEDULE_FILE = "system/schedule.md"
_TABLE_HEADER = "| id | when | recurring | prompt | created | next |"
_TABLE_SEP = "|-----|------|-----------|--------|---------|------|"
_MISFIRE_GRACE = 12 * 3600  # 12 hours in seconds
# Cell boundaries are bare pipes; an escaped \| belongs to the prompt text.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# Rejected in new job prompts (see the refusals in Scheduler.schedule);
# vault_check flags the same patterns in existing/hand-written rows.
_SCHEDULED_RUN_TAG_RX = re.compile(r"\[scheduled run\]", re.IGNORECASE)
_SILENT_SENTINEL_RX = re.compile(r"\[silent\]", re.IGNORECASE)


def _has_numeric_cron_dow(cron: str) -> bool:
    """True when a 5-field cron expression's day-of-week field contains digits."""
    fields = cron.split()
    return len(fields) == 5 and bool(re.search(r"\d", fields[4]))


def _escape_cell(value: str) -> str:
    """Escape cell delimiters and refuse values that could create another row."""
    if "\r" in value or "\n" in value:
        raise ValueError("schedule table cells must be single-line")
    return value.replace("|", "\\|")


def _unescape_cell(value: str) -> str:
    # Pipe escaping predates the strict writer; preserve every other backslash
    # sequence verbatim so existing prompts such as C:\notes are not corrupted.
    return value.replace("\\|", "|")


class ScheduleEntry:
    __slots__ = ("id", "when", "recurring", "prompt", "created", "next")

    def __init__(
        self,
        id: str,
        when: str,
        recurring: bool,
        prompt: str,
        created: str,
        next: str = "",
    ) -> None:
        self.id = id
        self.when = when
        self.recurring = recurring
        self.prompt = prompt
        self.created = created
        self.next = next

    def to_row(self) -> str:
        rec = "true" if self.recurring else "false"
        cells = (self.id, self.when, rec, self.prompt, self.created, self.next)
        return "| " + " | ".join(_escape_cell(cell) for cell in cells) + " |"

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

        # 5 columns is the pre-`next` format, still valid in hand-edited files.
        if len(cols) == 5:
            cols.append("")
        if len(cols) != 6:
            return None
        id_, when, rec, prompt, created, next_ = (_unescape_cell(col) for col in cols)
        if not id_ or not when:
            return None
        return cls(
            id=id_,
            when=when,
            recurring=(rec.lower() == "true"),
            prompt=prompt,
            created=created,
            next=next_,
        )


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
        queue_job_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._vault = vault_tools
        self._run_job = run_job_fn
        # Hands a one-off's prompt to the outage retry queue when its run
        # failed because Copilot was unreachable — the row is still removed,
        # so the reminder must survive somewhere.
        self._queue_job_fn = queue_job_fn
        self._tz = tz_name
        self._apscheduler = AsyncIOScheduler(timezone=tz_name)
        self._entries: dict[str, ScheduleEntry] = {}
        # Jobs run outside the Telegram update queue, so shutdown has to wait
        # on them separately or a mid-run reminder dies with the process.
        self._inflight: set[asyncio.Task[None]] = set()
        # Ids with a run in flight. A one-off's row is deleted only once its run
        # finishes, so a poll in between finds a live row with no APScheduler job
        # and would re-register — firing a job that already fired.
        self._running: set[str] = set()

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

    def _read_table(self) -> tuple[list[ScheduleEntry], list[str]]:
        """Parse schedule.md into entries plus the raw rows that would not parse.

        Callers that rewrite the file need both halves. Every mutation writes the
        table back in full, so a row the parser skipped is a row the next write
        erases — that is how a single malformed row cost an entire schedule.
        """
        text = self._vault.read_file(_SCHEDULE_FILE)
        if text.startswith("[file not found"):
            return [], []
        entries = []
        unparsed: list[str] = []
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
                    unparsed.append(stripped)
                    # The file is hand-editable, so a typo here is a job that
                    # silently never runs. Say so rather than dropping it mutely.
                    logger.warning(
                        "Ignoring unparseable row in %s (needs 6 columns: "
                        "id | when | recurring | prompt | created | next): %s",
                        _SCHEDULE_FILE,
                        stripped,
                    )
            elif in_table and not stripped:
                pass  # allow blank lines
        return entries, unparsed

    def _read_entries(self) -> list[ScheduleEntry]:
        return self._read_table()[0]

    def _write_entries(
        self, entries: list[ScheduleEntry], preserved: list[str] | None = None
    ) -> None:
        lines = [
            "# Schedule\n",
            _TABLE_HEADER,
            _TABLE_SEP,
        ]
        for e in entries:
            lines.append(e.to_row())
        # Rows the parser could not read go back verbatim, so a write never
        # costs more than it was asked to change. They stay inside the table,
        # where the next read keeps warning about them until a human fixes them.
        lines.extend(preserved or [])
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
            try:
                trigger = CronTrigger.from_crontab(entry.when, timezone=self._tz)
            except ValueError as exc:
                # APScheduler numbers weekdays 0=Mon..6=Sun, so crontab's Sunday
                # `7` is out of range and raises. Unguarded that aborted the rest
                # of the reload pass and crashed startup, where reload() has no
                # try around it — one typo took the whole schedule down.
                logger.warning(
                    "Invalid cron expression for job %s (%r): %s", job_id, entry.when, exc
                )
                return
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
        if job_id in self._running:
            # catch_up() and a live APScheduler fire can be submitted in the
            # same startup instant; whichever starts second stands down.
            logger.info("Skipping job %s: a run is already in flight", job_id)
            return
        logger.info("Firing job %s: %r", job_id, prompt)
        task = asyncio.current_task()
        if task is not None:
            self._inflight.add(task)
        self._running.add(job_id)
        completed = False
        try:
            try:
                await self._run_job(prompt)
                completed = True
            except CopilotUnavailableError as exc:
                if not recurring and self._queue_job_fn is not None:
                    # A one-off's row is removed below either way; without this
                    # hand-off the reminder would be silently lost. Recurring
                    # jobs self-heal: `next` stays past and catch_up() retries.
                    logger.warning(
                        "Copilot unavailable (%s); queueing one-off job %s for retry",
                        exc, job_id,
                    )
                    self._queue_job_fn(prompt)
                else:
                    logger.exception("Error in scheduled job %s", job_id)
            except Exception:
                logger.exception("Error in scheduled job %s", job_id)
            finally:
                if task is not None:
                    self._inflight.discard(task)
            if not recurring:
                # Removed even after a failure: left in place, the reload poll
                # would re-register and hot-loop a persistently failing one-off
                # every minute for the rest of its 12h grace.
                self._remove_entry(job_id)
            elif completed:
                # Advanced only on success: a past `next` is the missed-run
                # evidence catch_up() reads, and a failed run must not consume
                # it. No hot-loop risk here — catch-up fires once per startup.
                self._advance_next(job_id)
        finally:
            # Released only after the row is gone, so no poll can see a live row
            # for a run that has already happened.
            self._running.discard(job_id)

    def _remove_entry(self, job_id: str) -> None:
        entries, preserved = self._read_table()
        entries = [e for e in entries if e.id != job_id]
        self._write_entries(entries, preserved)
        self._entries.pop(job_id, None)

    # ------------------------------------------------------------------
    # Missed-run bookkeeping: the `next` column
    # ------------------------------------------------------------------

    def _next_occurrence(self, cron: str) -> datetime | None:
        """Next occurrence of a cron expression from now, UTC-aware."""
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=self._tz)
        except ValueError:
            return None
        fire = trigger.get_next_fire_time(None, datetime.now(tz=UTC))
        return fire.astimezone(UTC) if fire is not None else None

    def _parse_next(self, value: str) -> datetime | None:
        """Parse a `next` cell; a naive hand-edited time is read as local."""
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(self._tz))
        return dt.astimezone(UTC)

    def _advance_next(self, job_id: str) -> None:
        """After a completed recurring run, move `next` past now.

        Advancing only on completion mirrors the one-off row deletion: a crash
        mid-run leaves `next` in the past, so the run is retried on startup.
        """
        entries, preserved = self._read_table()
        entry = next((e for e in entries if e.id == job_id), None)
        if entry is None:
            return  # cancelled while the run was in flight
        nxt = self._next_occurrence(entry.when)
        if nxt is None:
            logger.warning(
                "Cannot compute next occurrence for job %s (%r)", job_id, entry.when
            )
            return
        entry.next = nxt.isoformat()
        self._write_entries(entries, preserved)
        cached = self._entries.get(job_id)
        if cached is not None:
            cached.next = entry.next

    def catch_up(self) -> int:
        """Fire one late run per recurring job missed while the service was down.

        A recurring row whose `next` is in the past was due when nothing was
        running to fire it. Occurrences are coalesced — no matter how many were
        missed or how long ago, the job gets a single run, told how late it is
        so the model can adapt or stay silent. Returns the number fired.
        """
        now = datetime.now(tz=UTC)
        fired = 0
        for entry in self._read_entries():
            if not entry.recurring or entry.id in self._running:
                continue
            nxt = self._parse_next(entry.next)
            if nxt is None or nxt > now:
                continue
            due_local = nxt.astimezone(ZoneInfo(self._tz)).strftime("%Y-%m-%d %H:%M")
            prompt = (
                f"[catch-up: this job was due at {due_local} but the assistant "
                f"was offline] {entry.prompt}"
            )
            logger.info("Catch-up fire for job %s (was due %s)", entry.id, due_local)
            fire_task = asyncio.create_task(self._fire(entry.id, prompt, recurring=True))
            # _fire tracks itself in _inflight, but only once it starts running;
            # holding the task here keeps it referenced (and drainable) until then.
            self._inflight.add(fire_task)
            fire_task.add_done_callback(self._inflight.discard)
            fired += 1
        return fired

    # ------------------------------------------------------------------
    # Startup reload
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Parse schedule.md and sync APScheduler. Safe to call multiple times."""
        entries, preserved = self._read_table()
        seen_ids: set[str] = set()
        backfilled = False
        for entry in entries:
            seen_ids.add(entry.id)
            if entry.id in self._running:
                continue  # mid-run; its row is still on disk by design
            # A recurring row with no usable `next` (hand-added, or the legacy
            # 5-column format) has no baseline, so nothing counts as missed —
            # establish one so a *later* downtime is detectable. A parseable
            # past value is exactly the evidence catch_up() reads: never
            # "fix" it forward here.
            if entry.recurring and self._parse_next(entry.next) is None:
                nxt = self._next_occurrence(entry.when)
                if nxt is not None:
                    entry.next = nxt.isoformat()
                    backfilled = True
            prev = self._entries.get(entry.id)
            unchanged = prev is not None and (
                (prev.when, prev.recurring, prev.prompt)
                == (entry.when, entry.recurring, entry.prompt)
            )
            if unchanged and self._apscheduler.get_job(entry.id):
                continue
            self._entries[entry.id] = entry
            self._register(entry)
        if backfilled:
            self._write_entries(entries, preserved)
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
        if "\r" in when or "\n" in when:
            return "[error: schedule time must be a single line]"
        if "\r" in prompt or "\n" in prompt:
            return "[error: schedule prompt must be a single line]"
        # Refusals for what prompts/schedule.md used to merely warn about:
        # per-job contract copies drift, and a hand-written "[scheduled run]"
        # tag doubles the one the runner prepends at fire time.
        if _SCHEDULED_RUN_TAG_RX.search(prompt):
            return (
                '[error: do not write "[scheduled run]" into the job prompt — '
                "the runner prepends it at fire time; drop it and reschedule]"
            )
        if _SILENT_SENTINEL_RX.search(prompt):
            return (
                '[error: the prompt restates the close contract ("[silent]") — '
                "it applies to every scheduled run on its own; drop it and reschedule]"
            )
        # APScheduler numbers weekdays from Monday=0 while classic cron uses
        # Sunday=0, so a numeric day-of-week silently fires on the wrong day.
        if recurring and _has_numeric_cron_dow(when):
            return (
                "[error: cron day-of-week must use names (SUN, MON, ...), never "
                "numbers — the numbering is ambiguous and fires on the wrong day]"
            )

        job_id = _generate_id()
        now_iso = datetime.now(tz=UTC).isoformat()

        # For one-off jobs, normalize 'when' to ISO
        next_stored = ""
        if not recurring:
            dt = parse_when(when, self._tz)
            if dt is None:
                return f"[error: could not parse time: {when!r}]"
            when_stored = dt.isoformat()
        else:
            try:
                CronTrigger.from_crontab(when, timezone=self._tz)
            except ValueError as exc:
                return f"[error: invalid cron expression {when!r}: {exc}]"
            when_stored = when  # store cron expression as-is
            nxt = self._next_occurrence(when)
            if nxt is not None:
                next_stored = nxt.isoformat()

        entry = ScheduleEntry(
            id=job_id,
            when=when_stored,
            recurring=recurring,
            prompt=prompt,
            created=now_iso,
            next=next_stored,
        )
        entries, preserved = self._read_table()
        entries.append(entry)
        self._write_entries(entries, preserved)
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
        entries, preserved = self._read_table()
        original_count = len(entries)
        entries = [e for e in entries if e.id != job_id]
        if len(entries) == original_count:
            return f"[job {job_id!r} not found]"
        self._write_entries(entries, preserved)
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
                            "when": {
                                "type": "string",
                                "description": "Single-line time expression or cron expression.",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Single-line agent prompt to run at that time.",
                            },
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
