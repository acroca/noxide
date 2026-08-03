"""Vault file tools: read, write, append, list, search.

All file operations are jailed to the vault root. Any path that resolves
outside the vault raises a PermissionError.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Generous enough that no hand-written vault page realistically hits it, low
# enough that a runaway file cannot displace the conversation.
_MAX_READ_CHARS = 100_000
# Matches beyond this are dropped from a search result
_MAX_SEARCH_RESULTS = 200


def _version_of(text: str) -> str:
    """Short content hash used as an optimistic-concurrency token.

    ``read_file`` (via dispatch) hands it to the model; ``rewrite_file``
    requires it back and refuses when the file changed in between — the
    full-rewrite analogue of ``edit_file``'s exact-match requirement.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def slug_from_name(name: str) -> str:
    """Derive a topic slug from its display name.

    Rules: lowercase, spaces replaced with hyphens, non-alphanumeric
    characters (except hyphens) removed, runs of hyphens collapsed.

    Example: ``"Health & Fitness"`` → ``"health-fitness"``
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = slug.strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug


class VaultTools:
    """File-system tools operating within a vault directory."""

    def __init__(self, vault_root: Path) -> None:
        self._root = vault_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _safe_path(self, rel: str) -> Path:
        """Resolve *rel* against vault root; raise if it escapes."""
        # Resolve with the root as base; Path.resolve() requires the path to exist,
        # so we construct absolute path manually first.
        candidate = (self._root / rel).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            # Model-visible via "[permission denied: ...]" — never include
            # self._root: where the vault lives on the host is deployment
            # detail that must stay out of runtime error strings.
            raise PermissionError(f"Path {rel!r} escapes the vault") from None
        return candidate

    def abs_path(self, rel: str) -> Path:
        """Absolute path for a vault-relative path; raises PermissionError on escape."""
        return self._safe_path(rel)

    # ------------------------------------------------------------------
    # File tools
    # ------------------------------------------------------------------

    def read_file(self, path: str) -> str:
        """Return text content of *path* (relative to vault root)."""
        p = self._safe_path(path)
        if not p.exists():
            return f"[file not found: {path}]"
        text = p.read_text(encoding="utf-8")
        # Every other content tool caps its output; without this one, a single
        # long journal or project page can swallow the whole context window.
        if len(text) > _MAX_READ_CHARS:
            return (
                text[:_MAX_READ_CHARS]
                + f"\n\n[truncated at {_MAX_READ_CHARS} characters — "
                f"{len(text)} total; use `search` to find the part you need]"
            )
        return text

    def write_file(self, path: str, content: str) -> str:
        """Write (create or overwrite) *content* to *path*.

        Internal (Python-side) writes only — not exposed as a tool. The model
        gets ``create_file`` (refuses to overwrite) and ``rewrite_file``
        (version-checked) instead, so concurrent agent runs cannot blindly
        clobber each other's changes.
        """
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {path}"

    def version(self, path: str) -> str:
        """Current version token of *path* (must exist)."""
        return _version_of(self._safe_path(path).read_text(encoding="utf-8"))

    def create_file(self, path: str, content: str) -> str:
        """Create *path* with *content*; refuse if it already exists."""
        p = self._safe_path(path)
        if p.exists():
            return (
                f"[create error: {path} already exists — read it, then use "
                "edit_file or rewrite_file to change it]"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Created {path} ({len(content)} bytes, version {_version_of(content)})"

    def rewrite_file(self, path: str, content: str, expected_version: str) -> str:
        """Replace *path*'s entire content iff its version still matches.

        *expected_version* comes from the model's most recent ``read_file``
        (or the version reported by a previous write). A mismatch means
        another run changed the file since — the caller must re-read and
        redo its changes on the current content, exactly like a failed
        ``edit_file`` match.
        """
        p = self._safe_path(path)
        if not p.exists():
            return f"[rewrite error: file not found: {path} — use create_file for new files]"
        current = _version_of(p.read_text(encoding="utf-8"))
        # Accept the token as the model saw it: "abc123", "version: abc123",
        # or the full "[version: abc123]" marker. Only a real content
        # mismatch should fail the rewrite, never formatting.
        expected = expected_version.strip().strip("[]").removeprefix("version:").strip()
        if current != expected:
            return (
                f"[rewrite error: {path} has changed since you read it "
                f"(version is now {current}, you passed {expected_version}) — "
                "read it again and redo your changes on the current content]"
            )
        p.write_text(content, encoding="utf-8")
        return f"Rewrote {path} ({len(content)} bytes, version {_version_of(content)})"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        """Replace the single occurrence of *old_string* in *path* with *new_string*.

        Surgical alternative to ``rewrite_file`` for the wiki's line patches:
        the rest of the file stays byte-identical, so keeping ``now.md``
        current costs one line instead of re-emitting the whole dashboard.
        Refuses ambiguous edits (0 or >1 matches) rather than guessing which
        line the caller meant.
        """
        p = self._safe_path(path)
        if not p.exists():
            return f"[file not found: {path}]"
        content = p.read_text(encoding="utf-8")
        occurrences = content.count(old_string)
        if occurrences == 0:
            return f"[edit error: old_string not found in {path}]"
        if occurrences > 1:
            return (
                f"[edit error: old_string appears {occurrences} times in {path}; "
                "include surrounding lines to identify a single one]"
            )
        new_content = content.replace(old_string, new_string)
        p.write_text(new_content, encoding="utf-8")
        return f"Edited {path} (version {_version_of(new_content)})"

    def append_file(self, path: str, content: str) -> str:
        """Append *content* to *path*, creating it if absent."""
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return (
            f"Appended {len(content)} bytes to {path} "
            f"(version {_version_of(p.read_text(encoding='utf-8'))})"
        )

    def move_file(self, path: str, new_path: str) -> str:
        """Move (rename) *path* to *new_path*; refuse to overwrite.

        Exists for archiving and renames — a copy-then-delete is impossible
        for the model (there is no delete tool), so without this a page
        "moved" to the archive would leave its original behind.
        """
        src = self._safe_path(path)
        dst = self._safe_path(new_path)
        if not src.exists():
            return f"[file not found: {path}]"
        if not src.is_file():
            return f"[move error: {path} is a directory — move its files one at a time]"
        if dst.exists():
            return (
                f"[move error: destination {new_path} already exists — "
                "pick another path or rewrite that file instead]"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return (
            f"Moved {path} to {new_path} "
            f"(version {_version_of(dst.read_text(encoding='utf-8'))})"
        )

    def list_files(self, glob: str) -> str:
        """Return newline-separated list of vault-relative paths matching *glob*."""
        # glob patterns are applied from vault root. glob() does not route through
        # _safe_path, and relative_to() is lexical (it does not collapse ".."), so a
        # pattern like "../*" would otherwise escape the vault. Resolve each match and
        # drop anything outside the root before listing.
        lines = []
        for p in sorted(self._root.glob(glob)):
            try:
                rel = p.resolve().relative_to(self._root)
            except ValueError:
                continue
            if p.is_file():
                lines.append(str(rel))
        return "\n".join(lines) if lines else "[no files matched]"

    def search(self, pattern: str) -> str:
        """Search all vault files for *pattern* (regex or plain text).

        Returns matches in ripgrep-style format: path:line_no:line_content
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Treat as plain text substring search
            rx = re.compile(re.escape(pattern), re.IGNORECASE)

        results: list[str] = []
        for p in sorted(self._root.rglob("*.md")):
            try:
                resolved = p.resolve()
                rel = str(resolved.relative_to(self._root))
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                lines = resolved.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    results.append(f"{rel}:{i}:{line}")
                    if len(results) >= _MAX_SEARCH_RESULTS:
                        results.append("... (truncated)")
                        return "\n".join(results)

        return "\n".join(results) if results else "[no matches]"

    def save_attachment(self, data: bytes, ext: str = "jpg") -> str:
        """Save binary *data* under attachments/ and return the vault-relative path.

        Used by the bot for incoming media (photos, …), not exposed as an
        agent tool.
        """
        safe_ext = re.sub(r"[^a-z0-9]", "", ext.lower()) or "bin"
        date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        rel = f"attachments/{date}-{secrets.token_hex(3)}.{safe_ext}"
        p = self._safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel

    # ------------------------------------------------------------------
    # OpenAI tool schemas
    # ------------------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read text content of a file in the vault. The output ends "
                        "with a [version: ...] token identifying the content you saw; "
                        "rewrite_file requires it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Vault-relative path, e.g. raw/journal/2026-01-01.md"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_file",
                    "description": (
                        "Create a new file in the vault. Fails if the file already "
                        "exists — read it and use edit_file or rewrite_file instead."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "rewrite_file",
                    "description": (
                        "Replace the entire content of an existing vault file. "
                        "Requires the [version: ...] token from your most recent "
                        "read of the file; fails if the file changed since, in "
                        "which case read it again and redo your changes. Prefer "
                        "edit_file for changing a line or two."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "expected_version": {
                                "type": "string",
                                "description": "Version token from your latest read_file of this file",
                            },
                        },
                        "required": ["path", "content", "expected_version"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "Replace one exact snippet in a vault file, leaving the rest "
                        "untouched. Preferred over rewrite_file for changing a line or "
                        "two (e.g. patching a wiki/now.md line). old_string must match "
                        "exactly and appear exactly once — include surrounding lines if "
                        "the text repeats. Pass an empty new_string to delete."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {
                                "type": "string",
                                "description": "Exact existing text, unique within the file",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text; empty string deletes",
                            },
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "append_file",
                    "description": "Append text to a file in the vault (creates it if absent).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "move_file",
                    "description": (
                        "Move or rename a vault file. Fails if the destination "
                        "already exists. After moving a wiki page, search for the "
                        "old path and patch any references to it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Current vault-relative path"},
                            "new_path": {"type": "string", "description": "New vault-relative path"},
                        },
                        "required": ["path", "new_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List vault files matching a glob pattern, e.g. wiki/people/*.md",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "glob": {"type": "string", "description": "Glob pattern relative to vault root"},
                        },
                        "required": ["glob"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search vault markdown files for a regex or plain-text pattern.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
        ]

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call by name.

        ``read_file`` results gain the ``[version: ...]`` token here rather
        than in the method, so internal callers (system prompt assembly,
        topic index parsing) keep seeing the file's bare content.
        """
        if name == "read_file":
            content = self.read_file(args["path"])
            if content.startswith("[file not found"):
                return content
            # Hash the file, not the (possibly truncated) returned text: the
            # token must identify the on-disk state a rewrite would replace.
            return f"{content}\n[version: {self.version(args['path'])}]"
        elif name == "create_file":
            return self.create_file(args["path"], args["content"])
        elif name == "rewrite_file":
            return self.rewrite_file(
                args["path"], args["content"], args["expected_version"]
            )
        elif name == "edit_file":
            return self.edit_file(args["path"], args["old_string"], args["new_string"])
        elif name == "append_file":
            return self.append_file(args["path"], args["content"])
        elif name == "move_file":
            return self.move_file(args["path"], args["new_path"])
        elif name == "list_files":
            return self.list_files(args["glob"])
        elif name == "search":
            return self.search(args["pattern"])
        else:
            return f"[unknown tool: {name}]"
