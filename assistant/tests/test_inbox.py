"""Offline captures are consumed by checkpoint, never by rewriting the inbox."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from assistant import inbox
from assistant.agent import MAX_ITERATIONS_REPLY
from assistant.backup import VaultBackup
from assistant.inbox import STATE_FILENAME, ingest, read_inbox


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_read_missing_or_whitespace_inbox(vault: Path) -> None:
    assert read_inbox(vault) is None
    (vault / "inbox.md").write_text(" \n\t")
    assert read_inbox(vault) is None


async def test_missing_inbox_runs_nothing(vault: Path, state_dir: Path) -> None:
    job = AsyncMock()
    await ingest(vault, job, state_dir=state_dir)
    job.assert_not_awaited()
    assert not (state_dir / STATE_FILENAME).exists()


async def test_success_retains_raw_inbox_and_skips_it_on_restart(
    vault: Path, state_dir: Path,
) -> None:
    text = "2026-07-29 18:30 - called the plumber\n"
    (vault / "inbox.md").write_text(text)
    job = AsyncMock(return_value="done")
    await ingest(vault, job, state_dir=state_dir)
    assert text in job.await_args.args[0]
    assert (vault / "inbox.md").read_text() == text
    assert (state_dir / STATE_FILENAME).read_text() == text
    await ingest(vault, job, state_dir=state_dir)
    job.assert_awaited_once()


@pytest.mark.parametrize("when", ["during_run", "during_checkpoint"])
@pytest.mark.parametrize("later", ["entry\nnew capture\n", "rewritten capture\n"])
async def test_external_writes_survive_and_are_consumed_next_startup(
    vault: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch, when: str, later: str,
) -> None:
    path = vault / "inbox.md"
    path.write_text("entry\n")
    real_write = inbox.atomic_write_text

    def checkpoint(path: Path, text: str) -> None:
        # Exactly the old loss window: after the last input read, before replace.
        (vault / "inbox.md").write_text(later)
        real_write(path, text)

    async def job(prompt: str) -> str:
        if when == "during_run":
            path.write_text(later)
        return "done"

    with monkeypatch.context() as m:
        if when == "during_checkpoint":
            m.setattr(inbox, "atomic_write_text", checkpoint)
        await ingest(vault, job, state_dir=state_dir)
    assert path.read_text() == later
    assert (state_dir / STATE_FILENAME).read_text() == "entry\n"
    next_job = AsyncMock(return_value="done")
    await ingest(vault, next_job, state_dir=state_dir)
    content = next_job.await_args.args[0].split("--- inbox.md ---\n", 1)[1]
    assert content == later.removeprefix("entry\n")
    assert (state_dir / STATE_FILENAME).read_text() == later


@pytest.mark.parametrize("failure", [RuntimeError("down"), MAX_ITERATIONS_REPLY])
async def test_failed_or_capped_run_keeps_old_checkpoint(
    vault: Path, state_dir: Path, failure: Exception | str,
) -> None:
    (vault / "inbox.md").write_text("old\n")
    await ingest(vault, AsyncMock(return_value="done"), state_dir=state_dir)
    (vault / "inbox.md").write_text("old\nnew\n")
    job = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(
        return_value=failure
    )
    await ingest(vault, job, state_dir=state_dir)
    assert (state_dir / STATE_FILENAME).read_text() == "old\n"
    assert (vault / "inbox.md").read_text() == "old\nnew\n"


async def test_cancelled_run_does_not_checkpoint(vault: Path, state_dir: Path) -> None:
    (vault / "inbox.md").write_text("entry\n")
    started = asyncio.Event()

    async def hang(prompt: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return "done"

    task = asyncio.create_task(ingest(vault, hang, state_dir=state_dir))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (state_dir / STATE_FILENAME).exists()
    assert (vault / "inbox.md").read_text() == "entry\n"


async def test_unreadable_inbox_is_left_untouched(vault: Path, state_dir: Path) -> None:
    (vault / "inbox.md").write_bytes(b"\xff\xfe")
    job = AsyncMock()
    await ingest(vault, job, state_dir=state_dir)
    job.assert_not_awaited()
    assert (vault / "inbox.md").read_bytes() == b"\xff\xfe"
    assert not (state_dir / STATE_FILENAME).exists()


async def test_checkpoint_write_failure_retries_without_touching_inbox(
    vault: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (vault / "inbox.md").write_text("entry\n")
    job = AsyncMock(return_value="done")
    with monkeypatch.context() as m:
        def fail(*args: object) -> None:
            raise OSError("disk full")
        m.setattr(inbox, "atomic_write_text", fail)
        await ingest(vault, job, state_dir=state_dir)
    assert (vault / "inbox.md").read_text() == "entry\n"
    assert not (state_dir / STATE_FILENAME).exists()
    await ingest(vault, job, state_dir=state_dir)
    assert job.await_count == 2


@pytest.mark.parametrize("deleted", [False, True])
async def test_observed_user_clear_resets_checkpoint(
    vault: Path, state_dir: Path, deleted: bool,
) -> None:
    path = vault / "inbox.md"
    path.write_text("entry\n")
    job = AsyncMock(return_value="done")
    await ingest(vault, job, state_dir=state_dir)
    if deleted:
        path.unlink()
    else:
        path.write_text("")
    await ingest(vault, job, state_dir=state_dir)
    assert (state_dir / STATE_FILENAME).read_text() == ""
    path.write_text("entry\n")
    await ingest(vault, job, state_dir=state_dir)
    assert job.await_count == 2


async def test_backup_failure_withholds_checkpoint_until_retry(
    vault: Path, state_dir: Path,
) -> None:
    backup = VaultBackup(vault, state_dir / "vault.git")
    await backup.init_repo()
    (vault / "inbox.md").write_text("entry\n")
    lock = state_dir / "vault.git" / "index.lock"
    lock.write_text("")
    job = AsyncMock(return_value="done")
    await ingest(vault, job, backup, state_dir=state_dir)
    assert not (state_dir / STATE_FILENAME).exists()
    assert (vault / "inbox.md").read_text() == "entry\n"
    lock.unlink()
    await ingest(vault, job, backup, state_dir=state_dir)
    assert (state_dir / STATE_FILENAME).read_text() == "entry\n"
    code, text = await backup._git("show", "HEAD:inbox.md")
    assert code == 0 and text == "entry\n"
    assert job.await_count == 2
