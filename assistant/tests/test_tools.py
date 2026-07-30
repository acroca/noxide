"""Tests for path jail in VaultTools."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.tools import VaultTools, slug_from_name


@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    return VaultTools(tmp_path)


# ------------------------------------------------------------------
# Path jail escapes
# ------------------------------------------------------------------

def test_read_file_normal(vault: VaultTools, tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hello")
    assert vault.read_file("notes.md") == "hello"


def test_read_file_subdir(vault: VaultTools, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.md").write_text("world")
    assert vault.read_file("sub/file.md") == "world"


def test_read_file_missing(vault: VaultTools) -> None:
    result = vault.read_file("nonexistent.md")
    assert result.startswith("[file not found")


def test_read_file_truncates_oversized_files(vault: VaultTools, tmp_path: Path) -> None:
    """An uncapped read can displace the whole conversation context."""
    (tmp_path / "huge.md").write_text("x" * 250_000)

    result = vault.read_file("huge.md")

    assert len(result) < 250_000
    assert result.startswith("x" * 1000)
    assert "truncated at 100000 characters" in result
    assert "250000 total" in result


def test_read_file_leaves_normal_files_byte_identical(
    vault: VaultTools, tmp_path: Path
) -> None:
    content = "# Journal\n\n- 09:00 nothing to report\n"
    (tmp_path / "day.md").write_text(content)

    assert vault.read_file("day.md") == content


def test_path_jail_dotdot(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.read_file("../etc/passwd")


def test_path_jail_absolute(vault: VaultTools, tmp_path: Path) -> None:
    # An absolute path outside the vault should be rejected
    outside = "/etc/passwd"
    with pytest.raises(PermissionError):
        vault.read_file(outside)


def test_path_jail_dotdot_deep(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.read_file("a/b/../../../../../../etc/passwd")


def test_path_jail_dotdot_write(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.write_file("../evil.txt", "bad")


def test_path_jail_dotdot_append(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.append_file("../evil.txt", "bad")


# ------------------------------------------------------------------
# Write / append
# ------------------------------------------------------------------

def test_write_and_read(vault: VaultTools) -> None:
    vault.write_file("hello.md", "content here")
    assert vault.read_file("hello.md") == "content here"


def test_write_creates_subdirs(vault: VaultTools) -> None:
    vault.write_file("a/b/c.md", "nested")
    assert vault.read_file("a/b/c.md") == "nested"


def test_append_file(vault: VaultTools) -> None:
    vault.write_file("log.md", "line1\n")
    vault.append_file("log.md", "line2\n")
    assert vault.read_file("log.md") == "line1\nline2\n"


def test_append_creates_file(vault: VaultTools) -> None:
    vault.append_file("new.md", "first")
    assert vault.read_file("new.md") == "first"


# ------------------------------------------------------------------
# edit_file
# ------------------------------------------------------------------

def test_edit_file_replaces_the_matched_text(vault: VaultTools) -> None:
    vault.write_file("now.md", "## Today\n- feed the ants (due)\n- water plants\n")

    vault.edit_file("now.md", "- feed the ants (due)", "- feed the ants (done today)")

    assert vault.read_file("now.md") == (
        "## Today\n- feed the ants (done today)\n- water plants\n"
    )


def test_edit_file_reports_the_replacement(vault: VaultTools) -> None:
    vault.write_file("now.md", "a\nb\n")

    result = vault.edit_file("now.md", "a", "z")

    assert "now.md" in result
    assert not result.startswith("[")


def test_edit_file_can_delete_a_line(vault: VaultTools) -> None:
    vault.write_file("now.md", "keep\nstale line\nkeep too\n")

    vault.edit_file("now.md", "stale line\n", "")

    assert vault.read_file("now.md") == "keep\nkeep too\n"


def test_edit_file_missing_file_returns_not_found_sentinel(vault: VaultTools) -> None:
    result = vault.edit_file("wiki/now.md", "a", "b")

    assert result.startswith("[file not found")


def test_edit_file_no_match_returns_error(vault: VaultTools) -> None:
    vault.write_file("now.md", "some content\n")

    result = vault.edit_file("now.md", "absent text", "new")

    assert result.startswith("[edit error")
    assert vault.read_file("now.md") == "some content\n"


def test_edit_file_ambiguous_match_refuses_and_leaves_file_unchanged(
    vault: VaultTools,
) -> None:
    original = "- [ ] feed ants\n## Upcoming\n- [ ] feed ants\n"
    vault.write_file("now.md", original)

    result = vault.edit_file("now.md", "- [ ] feed ants", "- [x] feed ants")

    assert result.startswith("[edit error")
    assert "2" in result
    assert vault.read_file("now.md") == original


def test_edit_file_path_jail(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.edit_file("../evil.txt", "a", "b")


def test_edit_file_is_exposed_as_a_tool(vault: VaultTools) -> None:
    names = [s["function"]["name"] for s in vault.tool_schemas()]

    assert "edit_file" in names


def test_edit_file_dispatch(vault: VaultTools) -> None:
    vault.write_file("now.md", "before\n")

    vault.dispatch(
        "edit_file", {"path": "now.md", "old_string": "before", "new_string": "after"}
    )

    assert vault.read_file("now.md") == "after\n"


# ------------------------------------------------------------------
# list_files
# ------------------------------------------------------------------

def test_list_files(vault: VaultTools, tmp_path: Path) -> None:
    vault.write_file("a.md", "")
    vault.write_file("b.md", "")
    vault.write_file("sub/c.md", "")
    result = vault.list_files("*.md")
    assert "a.md" in result
    assert "b.md" in result


def test_list_files_no_match(vault: VaultTools) -> None:
    result = vault.list_files("*.xyz")
    assert result == "[no files matched]"


def test_list_files_glob_subdir(vault: VaultTools) -> None:
    vault.write_file("notes/people/alice.md", "")
    vault.write_file("notes/people/bob.md", "")
    result = vault.list_files("notes/people/*.md")
    assert "notes/people/alice.md" in result
    assert "notes/people/bob.md" in result


def test_list_files_dotdot_does_not_escape(vault: VaultTools, tmp_path: Path) -> None:
    # A sibling file outside the vault must never be enumerated via "../" globs.
    (tmp_path.parent / "secret_outside.txt").write_text("top secret")
    vault.write_file("inside.md", "")
    for pattern in ("../*", "../*.txt", "**/../../*"):
        result = vault.list_files(pattern)
        assert "secret_outside" not in result
        assert ".." not in result


def test_list_files_dotdot_deep_does_not_escape(vault: VaultTools, tmp_path: Path) -> None:
    result = vault.list_files("../../../../../../etc/*")
    assert "passwd" not in result
    assert result == "[no files matched]"


# ------------------------------------------------------------------
# search
# ------------------------------------------------------------------

def test_search_finds_match(vault: VaultTools) -> None:
    vault.write_file("test.md", "Hello world\nfoo bar\n")
    result = vault.search("Hello")
    assert "test.md" in result
    assert "Hello world" in result


def test_search_no_match(vault: VaultTools) -> None:
    vault.write_file("test.md", "Hello world")
    result = vault.search("xyz_not_found")
    assert result == "[no matches]"


def test_search_regex(vault: VaultTools) -> None:
    vault.write_file("test.md", "TODO: buy milk\nDone: bought bread")
    result = vault.search(r"TODO:.*")
    assert "TODO: buy milk" in result
    assert "Done: bought bread" not in result


def test_search_case_insensitive(vault: VaultTools) -> None:
    vault.write_file("test.md", "Hello World")
    result = vault.search("hello world")
    assert "Hello World" in result


def test_search_does_not_follow_symlink_outside_vault(
    vault: VaultTools, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "secret.md"
    outside.write_text("outside secret")
    (tmp_path / "linked.md").symlink_to(outside)

    assert vault.search("outside secret") == "[no matches]"


# ------------------------------------------------------------------
# slug_from_name
# ------------------------------------------------------------------

def test_slug_simple() -> None:
    assert slug_from_name("Finance") == "finance"


def test_slug_spaces_to_hyphens() -> None:
    assert slug_from_name("Health Fitness") == "health-fitness"


def test_slug_special_chars_removed() -> None:
    assert slug_from_name("Health & Fitness") == "health-fitness"


def test_slug_multiple_spaces() -> None:
    assert slug_from_name("  Health   Fitness  ") == "health-fitness"


def test_slug_numbers_preserved() -> None:
    assert slug_from_name("Room 42") == "room-42"


def test_slug_already_hyphenated() -> None:
    assert slug_from_name("my-topic") == "my-topic"


def test_slug_mixed() -> None:
    result = slug_from_name("Q&A / Support")
    assert result == "qa-support"
    assert "&" not in result
    assert "/" not in result


# ------------------------------------------------------------------
# save_attachment
# ------------------------------------------------------------------

def test_save_attachment_writes_bytes_and_returns_relative_path(
    vault: VaultTools, tmp_path: Path
) -> None:
    rel = vault.save_attachment(b"\xff\xd8JPEGDATA", ext="jpg")

    assert rel.startswith("attachments/")
    assert rel.endswith(".jpg")
    assert (tmp_path / rel).read_bytes() == b"\xff\xd8JPEGDATA"


def test_save_attachment_names_are_unique(vault: VaultTools) -> None:
    rel1 = vault.save_attachment(b"one", ext="jpg")
    rel2 = vault.save_attachment(b"two", ext="jpg")

    assert rel1 != rel2


def test_save_attachment_sanitizes_extension(vault: VaultTools, tmp_path: Path) -> None:
    rel = vault.save_attachment(b"data", ext="../../etc/passwd")

    assert (tmp_path / rel).exists()
    resolved = (tmp_path / rel).resolve()
    assert str(resolved).startswith(str(tmp_path.resolve()))
    assert "/attachments/" in str(resolved)


# ------------------------------------------------------------------
# create_file / rewrite_file (optimistic concurrency for full writes)
# ------------------------------------------------------------------

def test_create_file_creates_new_file(vault: VaultTools) -> None:
    result = vault.create_file("wiki/people/anna.md", "# Anna\n")

    assert result.startswith("Created wiki/people/anna.md")
    assert "version" in result
    assert vault.read_file("wiki/people/anna.md") == "# Anna\n"


def test_create_file_refuses_existing_file(vault: VaultTools) -> None:
    vault.write_file("note.md", "original")

    result = vault.create_file("note.md", "clobber attempt")

    assert result.startswith("[create error:")
    assert vault.read_file("note.md") == "original"


def test_create_file_path_jail(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.create_file("../outside.md", "x")


def test_rewrite_file_with_current_version(vault: VaultTools) -> None:
    vault.write_file("note.md", "old content")
    version = vault.version("note.md")

    result = vault.rewrite_file("note.md", "new content", version)

    assert result.startswith("Rewrote note.md")
    assert vault.read_file("note.md") == "new content"


def test_rewrite_file_refuses_stale_version(vault: VaultTools) -> None:
    """The lost-update guard: a rewrite based on a read that another run has
    since overwritten must fail instead of clobbering the newer content."""
    vault.write_file("note.md", "state A")
    stale = vault.version("note.md")
    vault.write_file("note.md", "state B")  # concurrent run wrote in between

    result = vault.rewrite_file("note.md", "based on state A", stale)

    assert result.startswith("[rewrite error:")
    assert "changed since you read it" in result
    assert vault.read_file("note.md") == "state B"


def test_rewrite_file_missing_file_points_at_create(vault: VaultTools) -> None:
    result = vault.rewrite_file("nope.md", "content", "deadbeef")

    assert result.startswith("[rewrite error:")
    assert "create_file" in result


def test_rewrite_file_path_jail(vault: VaultTools) -> None:
    with pytest.raises(PermissionError):
        vault.rewrite_file("../outside.md", "x", "deadbeef")


def test_version_changes_with_content(vault: VaultTools) -> None:
    vault.write_file("note.md", "one")
    v1 = vault.version("note.md")
    vault.write_file("note.md", "two")
    v2 = vault.version("note.md")
    vault.write_file("note.md", "one")

    assert v1 != v2
    assert vault.version("note.md") == v1  # deterministic across writes


def test_mutating_tools_report_the_new_version(vault: VaultTools) -> None:
    """Every write reports the resulting version so the model can chain a
    later rewrite_file without re-reading."""
    created = vault.create_file("note.md", "line1\n")
    appended = vault.append_file("note.md", "line2\n")
    edited = vault.edit_file("note.md", "line2", "line-two")

    current = vault.version("note.md")
    assert f"version {current}" in edited
    for report in (created, appended):
        assert "version " in report


# ------------------------------------------------------------------
# Tool surface: dispatch and schemas
# ------------------------------------------------------------------

def test_read_file_dispatch_appends_version_token(vault: VaultTools) -> None:
    vault.write_file("note.md", "content here")

    out = vault.dispatch("read_file", {"path": "note.md"})

    assert out.startswith("content here")
    assert f"[version: {vault.version('note.md')}]" in out


def test_read_file_dispatch_missing_file_has_no_version(vault: VaultTools) -> None:
    out = vault.dispatch("read_file", {"path": "nope.md"})

    assert out == "[file not found: nope.md]"


def test_read_file_method_stays_bare(vault: VaultTools) -> None:
    """Internal callers (system prompt assembly, index parsing) must see the
    file's exact content, without the tool-facing version token."""
    vault.write_file("note.md", "content here")

    assert vault.read_file("note.md") == "content here"


def test_create_and_rewrite_dispatch(vault: VaultTools) -> None:
    vault.dispatch("create_file", {"path": "n.md", "content": "v1"})
    version = vault.version("n.md")
    vault.dispatch(
        "rewrite_file", {"path": "n.md", "content": "v2", "expected_version": version}
    )

    assert vault.read_file("n.md") == "v2"


def test_write_file_is_not_a_tool(vault: VaultTools) -> None:
    """write_file stays a Python method for internal writes; the model only
    gets the guarded create_file/rewrite_file pair."""
    names = {s["function"]["name"] for s in vault.tool_schemas()}
    assert "write_file" not in names
    assert {"create_file", "rewrite_file"} <= names

    out = vault.dispatch("write_file", {"path": "x.md", "content": "boo"})
    assert out == "[unknown tool: write_file]"
    assert vault.read_file("x.md").startswith("[file not found")


def test_rewrite_file_accepts_full_version_marker(vault: VaultTools) -> None:
    """Models echo tokens with their surrounding syntax; only a content
    mismatch should fail a rewrite, never formatting."""
    vault.write_file("note.md", "old")
    version = vault.version("note.md")

    result = vault.rewrite_file("note.md", "new", f"[version: {version}]")

    assert result.startswith("Rewrote note.md")
    assert vault.read_file("note.md") == "new"
