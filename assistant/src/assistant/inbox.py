"""Startup ingestion of offline captures without rewriting the user's inbox.

The last successfully consumed snapshot lives in state_dir. On the next run,
an unchanged inbox is skipped and an appended inbox contributes only its new
suffix. A divergent inbox is reprocessed in full: duplication is preferable to
losing a capture. No read/compare/replace can safely clear a file whose external
writers do not cooperate, so inbox.md itself is never written here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import MAX_ITERATIONS_REPLY
from .atomic import atomic_write_text

if TYPE_CHECKING:
    from .backup import VaultBackup

logger = logging.getLogger(__name__)

INBOX_FILENAME = "inbox.md"
STATE_FILENAME = "inbox.processed.md"

_PROMPT_TEMPLATE = """\
[inbox ingestion] While you were offline, the user wrote the entries below directly \
into `inbox.md` at the vault root. Process each entry now as if the user had just \
sent it as a message: record notes in the journal and wiki, schedule anything \
time-based, and use send_message for anything that needs an answer. Entries may \
date from earlier days - an entry starting with a date/time was written then; honor \
those dates when recording. Do not edit `inbox.md` itself: processed entries stay \
in the file and are tracked automatically so unchanged content is not ingested \
again. The entries below contain only the unprocessed suffix when the user has \
appended to the previous inbox. Close by telling the user briefly what was done \
with their inbox.

--- inbox.md ---
{content}"""


def read_inbox(vault_path: Path) -> str | None:
    """Return the inbox's raw content, or None when missing or whitespace-only."""
    try:
        content = (vault_path / INBOX_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return content if content.strip() else None


async def ingest(
    vault_path: Path,
    run_job_fn: Callable[[str], Coroutine[Any, Any, str | None]],
    backup: VaultBackup | None = None,
    *,
    state_dir: Path,
) -> None:
    """Consume new captures and persist the completed snapshot, never clear it.

    Failed, cancelled and iteration-capped runs leave the checkpoint unchanged.
    With backup enabled, failed git operations also withhold the checkpoint.
    The checkpoint stores the exact input snapshot, independently of git's
    view of a possibly externally modified inbox. Call once per startup.
    """
    try:
        checkpoint = state_dir / STATE_FILENAME
        try:
            processed = checkpoint.read_text(encoding="utf-8")
        except FileNotFoundError:
            processed = ""
        snapshot = read_inbox(vault_path) or ""
        content = snapshot[len(processed):] if snapshot.startswith(processed) else snapshot
        if content.strip():
            reply = await run_job_fn(_PROMPT_TEMPLATE.format(content=content))
            if reply == MAX_ITERATIONS_REPLY:
                logger.warning("inbox ingestion abandoned at the iteration cap; checkpoint unchanged")
                return
            if backup is not None and not await backup.commit_run(
                [INBOX_FILENAME],
                trigger="inbox ingestion",
                response="processed offline captures; original snapshot retained in state",
            ):
                logger.warning("inbox backup failed; checkpoint unchanged, next startup retries")
                return
        if snapshot != processed:
            # Only this observed snapshot is consumed. External appends/edits,
            # including ones arriving during this write, remain in inbox.md.
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(checkpoint, snapshot)
            logger.info("inbox ingestion checkpoint updated")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inbox ingestion failed; %s left untouched", INBOX_FILENAME)
