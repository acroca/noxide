"""Tests for inbox ingestion: offline captures in inbox.md processed at startup."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.inbox import INBOX_FILENAME, clear_processed, ingest, read_inbox


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _inbox(vault: Path) -> Path:
    return vault / INBOX_FILENAME


# ------------------------------------------------------------------
# read_inbox


def test_read_missing_file_returns_none(vault: Path) -> None:
    assert read_inbox(vault) is None


def test_read_whitespace_only_returns_none(vault: Path) -> None:
    _inbox(vault).write_text("  \n\n\t\n", encoding="utf-8")
    assert read_inbox(vault) is None


def test_read_returns_raw_content(vault: Path) -> None:
    content = "picked up the keys\n\n2026-07-29 18:30 — called the plumber\n"
    _inbox(vault).write_text(content, encoding="utf-8")
    assert read_inbox(vault) == content


# ------------------------------------------------------------------
# clear_processed


def test_clear_unchanged_file_truncates(vault: Path) -> None:
    _inbox(vault).write_text("entry\n", encoding="utf-8")

    outcome = clear_processed(vault, "entry\n")

    assert outcome == "cleared"
    assert _inbox(vault).read_text(encoding="utf-8") == ""


def test_clear_appended_file_keeps_remainder(vault: Path) -> None:
    _inbox(vault).write_text("entry\nlater entry\n", encoding="utf-8")

    outcome = clear_processed(vault, "entry\n")

    assert outcome == "trimmed"
    assert _inbox(vault).read_text(encoding="utf-8") == "later entry\n"


def test_clear_diverged_file_is_left_untouched(vault: Path) -> None:
    _inbox(vault).write_text("rewritten while running\n", encoding="utf-8")

    outcome = clear_processed(vault, "entry\n")

    assert outcome == "left"
    assert _inbox(vault).read_text(encoding="utf-8") == "rewritten while running\n"


def test_clear_missing_file_reports_left(vault: Path) -> None:
    assert clear_processed(vault, "entry\n") == "left"


# ------------------------------------------------------------------
# ingest


class RecordingJob:
    """Fake run_job_fn: records prompts; optional side effect mid-run."""

    def __init__(self, side_effect: Callable[[], None] | None = None) -> None:
        self.prompts: list[str] = []
        self._side_effect = side_effect

    async def __call__(self, prompt: str) -> None:
        self.prompts.append(prompt)
        if self._side_effect is not None:
            self._side_effect()


async def test_ingest_missing_file_runs_nothing(vault: Path) -> None:
    job = RecordingJob()

    await ingest(vault, job)

    assert job.prompts == []


async def test_ingest_embeds_the_snapshot_in_one_job_prompt(vault: Path) -> None:
    content = "picked up the keys\n\n2026-07-29 18:30 — called the plumber\n"
    _inbox(vault).write_text(content, encoding="utf-8")
    job = RecordingJob()

    await ingest(vault, job)

    assert len(job.prompts) == 1
    assert content in job.prompts[0]
    assert "inbox.md" in job.prompts[0]


async def test_ingest_clears_the_file_on_success(vault: Path) -> None:
    _inbox(vault).write_text("entry\n", encoding="utf-8")

    await ingest(vault, RecordingJob())

    assert _inbox(vault).read_text(encoding="utf-8") == ""


async def test_ingest_keeps_entries_appended_during_the_run(vault: Path) -> None:
    _inbox(vault).write_text("entry\n", encoding="utf-8")

    def append_late() -> None:
        with _inbox(vault).open("a", encoding="utf-8") as f:
            f.write("late entry\n")

    await ingest(vault, RecordingJob(side_effect=append_late))

    assert _inbox(vault).read_text(encoding="utf-8") == "late entry\n"


async def test_ingest_leaves_file_untouched_when_the_run_fails(vault: Path) -> None:
    _inbox(vault).write_text("entry\n", encoding="utf-8")

    async def failing_job(prompt: str) -> None:
        raise RuntimeError("copilot unreachable")

    await ingest(vault, failing_job)

    assert _inbox(vault).read_text(encoding="utf-8") == "entry\n"


async def test_ingest_clears_only_under_the_lock(vault: Path) -> None:
    _inbox(vault).write_text("entry\n", encoding="utf-8")
    lock = asyncio.Lock()
    await lock.acquire()

    task = asyncio.create_task(ingest(vault, RecordingJob(), lock=lock))
    for _ in range(10):
        await asyncio.sleep(0)
    assert _inbox(vault).read_text(encoding="utf-8") == "entry\n"

    lock.release()
    await task
    assert _inbox(vault).read_text(encoding="utf-8") == ""
