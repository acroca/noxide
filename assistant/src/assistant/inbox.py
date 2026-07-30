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
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

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
        path.write_text("", encoding="utf-8")
        return "cleared"
    if current.startswith(snapshot):
        path.write_text(current[len(snapshot) :], encoding="utf-8")
        return "trimmed"
    return "left"


async def ingest(
    vault_path: Path,
    run_job_fn: Callable[[str], Coroutine[Any, Any, None]],
    lock: asyncio.Lock | None = None,
) -> None:
    """Process the inbox through one job-style agent run, then clear it.

    A failed run leaves the file exactly as it was — the next startup retries.
    *lock* is the backup's commit lock when backup is enabled, held around the
    clear so a sweep never snapshots the file mid-write.
    """
    snapshot = read_inbox(vault_path)
    if snapshot is None:
        return
    try:
        await run_job_fn(_PROMPT_TEMPLATE.format(content=snapshot))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inbox ingestion failed; %s left untouched", INBOX_FILENAME)
        return
    async with lock if lock is not None else contextlib.nullcontext():
        outcome = clear_processed(vault_path, snapshot)
    if outcome == "left":
        logger.warning(
            "%s changed during ingestion; left in place, next startup reprocesses",
            INBOX_FILENAME,
        )
    else:
        logger.info("inbox ingestion done (%s)", outcome)
