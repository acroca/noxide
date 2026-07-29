"""Tests for the vault git backup: separate git dir, attributed commits, sweep."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.backup import VaultBackup


def _git(git_dir: Path, vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git against the backup repo the same way the module does."""
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), "--work-tree", str(vault), *args],
        capture_output=True,
        text=True,
        cwd=vault,
    )


def _log_messages(git_dir: Path, vault: Path) -> list[str]:
    """Full commit messages, newest first; empty list when there are no commits."""
    result = _git(git_dir, vault, "log", "--format=%B%x00")
    if result.returncode != 0:
        return []
    return [m.strip() for m in result.stdout.split("\x00") if m.strip()]


def _committed_files(git_dir: Path, vault: Path) -> set[str]:
    result = _git(git_dir, vault, "ls-tree", "-r", "--name-only", "HEAD")
    return set(result.stdout.split())


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def git_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "vault.git"


@pytest.fixture
async def backup(vault: Path, git_dir: Path) -> VaultBackup:
    b = VaultBackup(vault, git_dir)
    await b.init_repo()
    return b


# ------------------------------------------------------------------
# init_repo
# ------------------------------------------------------------------

async def test_init_repo_creates_git_dir_and_keeps_vault_clean(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    assert (git_dir / "HEAD").exists()
    assert not (vault / ".git").exists()


async def test_init_repo_commits_existing_vault_files_as_baseline(
    vault: Path, git_dir: Path
) -> None:
    (vault / "wiki").mkdir()
    (vault / "wiki" / "now.md").write_text("# Now\n")

    b = VaultBackup(vault, git_dir)
    await b.init_repo()

    assert "wiki/now.md" in _committed_files(git_dir, vault)


async def test_init_repo_is_idempotent(vault: Path, git_dir: Path) -> None:
    (vault / "a.md").write_text("a\n")
    b = VaultBackup(vault, git_dir)
    await b.init_repo()
    await b.init_repo()

    assert len(_log_messages(git_dir, vault)) == 1


# ------------------------------------------------------------------
# commit_run
# ------------------------------------------------------------------

async def test_commit_run_commits_only_the_given_paths(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "mine.md").write_text("mine\n")
    (vault / "other.md").write_text("someone else's in-flight write\n")

    await backup.commit_run({"mine.md"}, trigger="add a note", response="done")

    assert "mine.md" in _committed_files(git_dir, vault)
    assert "other.md" not in _committed_files(git_dir, vault)


async def test_commit_run_message_carries_trigger_and_response(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")

    await backup.commit_run(
        {"note.md"},
        trigger="remind me to water the plants tomorrow",
        response="Scheduled a reminder for tomorrow at 9:00.",
    )

    message = _log_messages(git_dir, vault)[0]
    assert message.splitlines()[0] == "remind me to water the plants tomorrow"
    assert "Scheduled a reminder for tomorrow at 9:00." in message


async def test_commit_run_truncates_long_trigger_and_response(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")

    await backup.commit_run({"note.md"}, trigger="t" * 5000, response="r" * 5000)

    message = _log_messages(git_dir, vault)[0]
    assert len(message) < 5000
    assert "t" * 100 in message
    assert "r" * 100 in message


async def test_commit_run_without_changes_creates_no_commit(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")
    await backup.commit_run({"note.md"}, trigger="first", response="ok")

    await backup.commit_run({"note.md"}, trigger="second", response="ok")

    assert len(_log_messages(git_dir, vault)) == 1  # only the first


async def test_commit_run_records_deletions(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")
    await backup.commit_run({"note.md"}, trigger="create", response="ok")

    (vault / "note.md").unlink()
    await backup.commit_run({"note.md"}, trigger="delete", response="ok")

    assert "note.md" not in _committed_files(git_dir, vault)


async def test_commit_run_survives_git_failure(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")
    # A stale index.lock (e.g. the user mid-command on the Mac) must not raise.
    (git_dir / "index.lock").write_text("")

    await backup.commit_run({"note.md"}, trigger="t", response="r")

    assert len(_log_messages(git_dir, vault)) == 0


# ------------------------------------------------------------------
# schedule_commit + drain
# ------------------------------------------------------------------

async def test_schedule_commit_runs_in_background_and_drain_awaits_it(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")

    backup.schedule_commit({"note.md"}, trigger="bg", response="ok")
    await backup.drain()

    assert "bg" in _log_messages(git_dir, vault)[0]


# ------------------------------------------------------------------
# lock
# ------------------------------------------------------------------

async def test_commit_waits_for_the_vault_lock(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "note.md").write_text("x\n")

    async with backup.lock:
        task = asyncio.create_task(
            backup.commit_run({"note.md"}, trigger="t", response="r")
        )
        await asyncio.sleep(0.05)
        assert not task.done()
    await task

    assert len(_log_messages(git_dir, vault)) == 1


# ------------------------------------------------------------------
# sweep
# ------------------------------------------------------------------

async def test_sweep_commits_unattributed_changes(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "external.md").write_text("edited on another device\n")

    await backup.sweep()

    assert "external.md" in _committed_files(git_dir, vault)
    assert "sweep" in _log_messages(git_dir, vault)[0]


async def test_sweep_without_changes_creates_no_commit(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    await backup.sweep()

    assert len(_log_messages(git_dir, vault)) == 0  # empty vault: no baseline either


async def test_sweep_skips_when_icloud_placeholder_present(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "real.md").write_text("x\n")
    (vault / ".evicted.md.icloud").write_text("")

    await backup.sweep()

    assert "real.md" not in _committed_files(git_dir, vault)


async def test_usage_view_is_excluded_from_backup(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    (vault / "system").mkdir()
    (vault / "system" / "usage.md").write_text("| tokens |\n")

    await backup.sweep()

    assert len(_log_messages(git_dir, vault)) == 0


async def test_init_repo_supports_persisted_core_worktree_inspection(
    backup: VaultBackup, vault: Path, git_dir: Path
) -> None:
    """The documented Mac-side recipe: set core.worktree once, then plain status works."""
    (vault / "note.md").write_text("x\n")

    config = _git(git_dir, vault, "config", "core.worktree", str(vault))
    status = subprocess.run(
        ["git", "--git-dir", str(git_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )

    assert config.returncode == 0
    assert status.returncode == 0, status.stderr
    assert "note.md" in status.stdout


async def test_init_repo_preserves_user_added_exclude_lines(
    vault: Path, git_dir: Path
) -> None:
    b = VaultBackup(vault, git_dir)
    await b.init_repo()
    exclude = git_dir / "info" / "exclude"
    exclude.write_text(exclude.read_text() + ".obsidian/workspace*\n")

    await b.init_repo()  # a restart must not clobber the user's line

    text = exclude.read_text()
    assert ".obsidian/workspace*" in text
    assert text.count("/system/usage.md") == 1
