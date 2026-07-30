"""Startup ingestion of the vault-root inbox file (offline capture).

While the bot is down the user writes entries directly into ``inbox.md`` at
the vault root. At the next startup the file is ingested: one job-style agent
run processes every entry as if it had arrived as a Telegram message, then
the processed content is cleared. The snapshot embedded in the prompt is the
unit of processing — clearing compares the file against it, so entries
appended mid-run survive for the next ingestion, and no entry is ever
deleted without a completed run over it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import MAX_ITERATIONS_REPLY

if TYPE_CHECKING:
    from .backup import VaultBackup

logger = logging.getLogger(__name__)

INBOX_FILENAME = "inbox.md"

# Rides run_job, so it lands after the [scheduled run] tag and the job-close
# contract in prompts/schedule.md applies as-is — which is also why it must
# not restate that contract (per-prompt copies drift; see that file).
_PROMPT_TEMPLATE = """\
[inbox ingestion] While you were offline, the user wrote the entries below directly \
into `inbox.md` at the vault root. Process each entry now as if the user had just \
sent it as a message: record notes in the journal and wiki, schedule anything \
time-based, and use send_message for anything that needs an answer. Entries may \
date from earlier days — an entry starting with a date/time was written then; honor \
those dates when recording. Do not edit `inbox.md` itself: the processed entries \
are cleared automatically after this run. Close by telling the user briefly what \
was done with their inbox.

--- inbox.md ---
{content}"""


def read_inbox(vault_path: Path) -> str | None:
    """Return the inbox's raw content, or None when missing or whitespace-only."""
    try:
        content = (vault_path / INBOX_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return content if content.strip() else None


def clear_processed(vault_path: Path, snapshot: str) -> str:
    """Remove the processed *snapshot* from the inbox; report what happened.

    Returns ``"cleared"`` (file matched the snapshot, truncated), ``"trimmed"``
    (entries were appended mid-run; only the snapshot prefix was removed) or
    ``"left"`` (file diverged from the snapshot, or is gone — nothing removed).
    """
    path = vault_path / INBOX_FILENAME
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "left"
    if current == snapshot:
        _replace_content(path, "")
        return "cleared"
    if current.startswith(snapshot):
        _replace_content(path, current[len(snapshot) :])
        return "trimmed"
    return "left"


def _replace_content(path: Path, text: str) -> None:
    """Atomic rewrite — a crash mid-clear must not delete unprocessed entries."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


async def ingest(
    vault_path: Path,
    run_job_fn: Callable[[str], Coroutine[Any, Any, str | None]],
    backup: VaultBackup | None = None,
) -> None:
    """Process the inbox through one job-style agent run, then clear it.

    A failed run leaves the file exactly as it was — the next startup retries.
    With *backup* enabled the pre-clear snapshot is committed to the backup
    repo first — the startup sweep usually already has it, but that sweep can
    refuse (iCloud eviction placeholders) or fail silently, and the promise is
    "in history before anything clears it" — and the clear itself runs under
    the backup lock so a sweep never snapshots the file mid-write.
    """
    try:
        await _ingest(vault_path, run_job_fn, backup)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Runs as a fire-and-forget task: an exception escaping here would
        # surface (at best) as an unretrieved-task warning at GC time.
        logger.exception("inbox ingestion failed; %s left untouched", INBOX_FILENAME)


async def _ingest(
    vault_path: Path,
    run_job_fn: Callable[[str], Coroutine[Any, Any, str | None]],
    backup: VaultBackup | None,
) -> None:
    snapshot = read_inbox(vault_path)
    if snapshot is None:
        return
    try:
        reply = await run_job_fn(_PROMPT_TEMPLATE.format(content=snapshot))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inbox ingestion failed; %s left untouched", INBOX_FILENAME)
        return
    # An abandoned run (iteration cap) may have processed only part of the
    # snapshot — clearing would delete entries no run completed over.
    if reply == MAX_ITERATIONS_REPLY:
        logger.warning(
            "inbox ingestion abandoned at the iteration cap; %s left untouched",
            INBOX_FILENAME,
        )
        return
    if backup is not None:
        # Outside the lock: commit_run acquires it internally.
        await backup.commit_run(
            [INBOX_FILENAME],
            trigger="inbox ingestion",
            response="pre-clear snapshot, committed before the processed entries are cleared",
        )
    async with backup.lock if backup is not None else contextlib.nullcontext():
        outcome = clear_processed(vault_path, snapshot)
    if outcome == "left":
        logger.warning(
            "%s changed during ingestion; left in place, next startup reprocesses",
            INBOX_FILENAME,
        )
    else:
        logger.info("inbox ingestion done (%s)", outcome)
