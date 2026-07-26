"""Vault file tools: read, write, append, list, search.

All file operations are jailed to the vault root. Any path that resolves
outside the vault raises a PermissionError.
"""

from __future__ import annotations

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
            raise PermissionError(
                f"Path {rel!r} escapes vault root {self._root}"
            ) from None
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
        """Write (create or overwrite) *content* to *path*."""
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        """Replace the single occurrence of *old_string* in *path* with *new_string*.

        Surgical alternative to ``write_file`` for the wiki's line patches: the
        rest of the file stays byte-identical, so keeping ``now.md`` current
        costs one line instead of re-emitting the whole dashboard. Refuses
        ambiguous edits (0 or >1 matches) rather than guessing which line the
        caller meant.
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
        p.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return f"Edited {path}"

    def append_file(self, path: str, content: str) -> str:
        """Append *content* to *path*, creating it if absent."""
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} bytes to {path}"

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
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            rel = str(p.relative_to(self._root))
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
                    "description": "Read text content of a file in the vault.",
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
                    "name": "write_file",
                    "description": "Write (create or overwrite) a file in the vault.",
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
                    "name": "edit_file",
                    "description": (
                        "Replace one exact snippet in a vault file, leaving the rest "
                        "untouched. Preferred over write_file for changing a line or "
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
        """Dispatch a tool call by name."""
        if name == "read_file":
            return self.read_file(args["path"])
        elif name == "write_file":
            return self.write_file(args["path"], args["content"])
        elif name == "edit_file":
            return self.edit_file(args["path"], args["old_string"], args["new_string"])
        elif name == "append_file":
            return self.append_file(args["path"], args["content"])
        elif name == "list_files":
            return self.list_files(args["glob"])
        elif name == "search":
            return self.search(args["pattern"])
        else:
            return f"[unknown tool: {name}]"
