"""Tests for the skill library: scanning, trigger parsing, menu rendering."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.skills import SkillLibrary


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (vault_root, repo_skills_dir) with both skill directories created."""
    vault_root = tmp_path / "vault"
    repo_dir = tmp_path / "repo-skills"
    (vault_root / "system" / "skills").mkdir(parents=True)
    repo_dir.mkdir(parents=True)
    return vault_root, repo_dir


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


def _vault_skills(vault_root: Path) -> Path:
    return vault_root / "system" / "skills"


def test_menu_lists_repo_and_vault_skills_alphabetically(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "alpha.md", "# alpha\n\n**Use when:** first thing.\n")
    _write(_vault_skills(vault_root), "zebra.md", "# zebra\n\n**Use when:** last thing.\n")

    menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu == (
        "## Available skills\n\n"
        "- `alpha` — first thing.\n"
        "- `zebra` — last thing."
    )


def test_menu_is_empty_when_no_skills_exist(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs

    assert SkillLibrary(vault_root, repo_dir).menu() == ""


def test_missing_skill_directories_are_treated_as_empty(tmp_path: Path) -> None:
    """The vault's system/skills/ will not exist until the first skill is written."""
    lib = SkillLibrary(tmp_path / "no-vault", tmp_path / "no-repo")

    assert lib.menu() == ""


@pytest.mark.parametrize(
    "line",
    [
        "**Use when:** asked for the weekly review.",
        "**Use when**: asked for the weekly review.",
        "Use when: asked for the weekly review.",
        "  **use when:**   asked for the weekly review.",
        "*Use when:* asked for the weekly review.",
        "*Use when*: asked for the weekly review.",
    ],
)
def test_trigger_line_spellings_all_parse(dirs: tuple[Path, Path], line: str) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "wr.md", f"# wr\n\n{line}\n\n## Steps\n1. do it\n")

    menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu.endswith("- `wr` — asked for the weekly review.")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Asterisk away from the marker boundary.
        ("**Use when:** matching files like *.txt or *.md.", "matching files like *.txt or *.md."),
        # Trigger text *starts* with asterisks under the plain marker — the case
        # an optional-run regex silently eats.
        ("Use when: **urgent** requests only.", "**urgent** requests only."),
        ("Use when: *.py files change.", "*.py files change."),
        # Same, under a bold marker.
        ("**Use when:** *.py files change.", "*.py files change."),
    ],
)
def test_asterisks_inside_the_trigger_survive(
    dirs: tuple[Path, Path], line: str, expected: str
) -> None:
    """Only a complete marker is consumed — trigger content is preserved verbatim."""
    vault_root, repo_dir = dirs
    _write(repo_dir, "globs.md", f"# globs\n\n{line}\n")

    menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu.endswith(f"- `globs` — {expected}")


def test_vault_skill_shadows_repo_skill_of_same_slug(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "review.md", "# review\n\n**Use when:** shipped version.\n")
    _write(_vault_skills(vault_root), "review.md", "# review\n\n**Use when:** my version.\n")

    menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu == "## Available skills\n\n- `review` — my version."


def test_skill_without_trigger_line_is_excluded_and_warned(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "broken.md", "# broken\n\nNo trigger anywhere in this file.\n")
    _write(repo_dir, "good.md", "# good\n\n**Use when:** it works.\n")

    with caplog.at_level(logging.WARNING):
        menu = SkillLibrary(vault_root, repo_dir).menu()

    assert "broken" not in menu
    assert "- `good` — it works." in menu
    assert "broken" in caplog.text


def test_long_trigger_is_truncated_to_200_chars(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "verbose.md", f"# verbose\n\n**Use when:** {'x' * 400}\n")

    menu = SkillLibrary(vault_root, repo_dir).menu()

    trigger = menu.splitlines()[-1].split(" — ", 1)[1]
    assert len(trigger) == 200
    assert trigger.endswith("…")


def test_undecodable_skill_is_skipped_and_menu_still_renders(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "ok.md", "# ok\n\n**Use when:** fine.\n")
    (repo_dir / "bad.md").write_bytes(b"# bad\n\n**Use when:** \xff\xfe not utf-8\n")

    with caplog.at_level(logging.WARNING):
        menu = SkillLibrary(vault_root, repo_dir).menu()

    assert "- `ok` — fine." in menu
    assert "bad" in caplog.text


def test_non_markdown_files_are_ignored(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "notes.txt", "**Use when:** should not appear.\n")

    assert SkillLibrary(vault_root, repo_dir).menu() == ""


# ------------------------------------------------------------------
# Loading bodies
# ------------------------------------------------------------------


def test_load_returns_the_full_body_verbatim(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    body = "# wr\n\n**Use when:** asked for the review.\n\n## Steps\n1. read now.md\n"
    _write(repo_dir, "wr.md", body)

    assert SkillLibrary(vault_root, repo_dir).load("wr") == body


def test_load_prefers_the_vault_copy(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "review.md", "# review\n\n**Use when:** shipped.\n\nshipped steps\n")
    _write(_vault_skills(vault_root), "review.md", "# review\n\n**Use when:** mine.\n\nmy steps\n")

    loaded = SkillLibrary(vault_root, repo_dir).load("review")

    assert "my steps" in loaded
    assert "shipped steps" not in loaded


def test_load_unknown_slug_returns_sentinel(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs

    assert SkillLibrary(vault_root, repo_dir).load("nope") == "[skill not found: nope]"


@pytest.mark.parametrize(
    "slug",
    ["../AGENTS.md", "../../etc/passwd", "/etc/passwd", "sub/dir", "..", "", "Weekly Review"],
)
def test_load_rejects_anything_that_is_not_a_slug(dirs: tuple[Path, Path], slug: str) -> None:
    """The slug comes from the model — it must never be joined to a path unvalidated."""
    vault_root, repo_dir = dirs
    (vault_root / "AGENTS.md").write_text("SECRET vault conventions", encoding="utf-8")
    _write(repo_dir, "ok.md", "# ok\n\n**Use when:** fine.\n")

    result = SkillLibrary(vault_root, repo_dir).load(slug)

    assert result == f"[skill not found: {slug}]"
    assert "SECRET" not in result


def test_load_of_undecodable_file_returns_sentinel(dirs: tuple[Path, Path]) -> None:
    """Such a file is dropped during the scan, so it is simply not loadable."""
    vault_root, repo_dir = dirs
    (repo_dir / "bad.md").write_bytes(b"# bad\n\n**Use when:** ok\n\n\xff\xfe\n")

    assert SkillLibrary(vault_root, repo_dir).load("bad") == "[skill not found: bad]"


# ------------------------------------------------------------------
# Tool schema and dispatch
# ------------------------------------------------------------------


def test_tool_schema_exposes_load_skill(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs

    schemas = SkillLibrary(vault_root, repo_dir).tool_schemas()

    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "load_skill"
    assert fn["parameters"]["required"] == ["name"]
    assert fn["parameters"]["properties"]["name"]["type"] == "string"


def test_dispatch_load_skill_returns_the_body(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs
    _write(repo_dir, "wr.md", "# wr\n\n**Use when:** asked.\n\n## Steps\n1. go\n")

    result = SkillLibrary(vault_root, repo_dir).dispatch("load_skill", {"name": "wr"})

    assert "## Steps" in result


def test_dispatch_logs_slug_and_resolved_source(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """Container logs must distinguish 'no skill matched' from 'wrong skill matched'."""
    vault_root, repo_dir = dirs
    _write(_vault_skills(vault_root), "mine.md", "# mine\n\n**Use when:** asked.\n")

    with caplog.at_level(logging.INFO):
        SkillLibrary(vault_root, repo_dir).dispatch("load_skill", {"name": "mine"})

    assert "mine" in caplog.text
    assert "vault" in caplog.text


def test_dispatch_unknown_tool_returns_sentinel(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs

    result = SkillLibrary(vault_root, repo_dir).dispatch("nope", {})

    assert result == "[unknown tool: nope]"


def test_dispatch_missing_name_argument_returns_sentinel(dirs: tuple[Path, Path]) -> None:
    vault_root, repo_dir = dirs

    result = SkillLibrary(vault_root, repo_dir).dispatch("load_skill", {})

    assert result == "[skill not found: ]"


# ------------------------------------------------------------------
# Shipped skills
# ------------------------------------------------------------------


def test_shipped_skill_authoring_is_discoverable(tmp_path: Path) -> None:
    """Guards the shipped file against a malformed trigger line."""
    lib = SkillLibrary(tmp_path)  # default repo dir = the real package skills/

    menu = lib.menu()

    assert "- `skill-authoring` — " in menu


def test_shipped_skill_authoring_body_loads(tmp_path: Path) -> None:
    body = SkillLibrary(tmp_path).load("skill-authoring")

    assert not body.startswith("[skill not found")
    assert "Use when:" in body


def test_skill_with_non_slug_filename_is_excluded_and_warned(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """Menu and load must agree: a stem load_skill would reject is never advertised."""
    vault_root, repo_dir = dirs
    _write(repo_dir, "revisión-semanal.md", "# x\n\n**Use when:** acentos.\n")
    _write(repo_dir, "weekly_review.md", "# x\n\n**Use when:** underscore.\n")
    _write(repo_dir, "CAPS.md", "# x\n\n**Use when:** uppercase.\n")
    _write(repo_dir, "ok.md", "# ok\n\n**Use when:** fine.\n")

    with caplog.at_level(logging.WARNING):
        menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu == "## Available skills\n\n- `ok` — fine."
    for bad in ("revisión-semanal", "weekly_review", "CAPS"):
        assert bad in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        "***Use when:*** triple asterisks.",
        "- **Use when:** bulleted marker.",
        "> **Use when:** blockquoted marker.",
        "**Use  when:** doubled space inside the marker.",
    ],
)
def test_out_of_set_markers_are_excluded_and_warned(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture, line: str
) -> None:
    """The accepted marker set is closed — guards against loosening a regex branch."""
    vault_root, repo_dir = dirs
    _write(repo_dir, "odd.md", f"# odd\n\n{line}\n")

    with caplog.at_level(logging.WARNING):
        menu = SkillLibrary(vault_root, repo_dir).menu()

    assert menu == ""
    assert "odd" in caplog.text
