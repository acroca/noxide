"""Page-level hygiene checks, run by ``vault_check.run_checks``.

What a full-wiki lint found by hand on 2026-09-02, turned into enumerated
checks: dead relative links (a page kept pointing at a legacy file deleted
five weeks earlier), empty sections, a bullet pasted twice, placeholder
Status paragraphs that survived from page creation, and a Tasks list past
the archive threshold. Each check returns ``(heading, "path:line: …")``
findings and never writes to the vault.

Lines inside fenced code blocks are skipped everywhere — a template page can
legitimately show example links and bullets.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

_NOW_PATH = "wiki/now.md"

_FENCE_RX = re.compile(r"^\s*(```|~~~)")
_HEADING_RX = re.compile(r"^(?P<hashes>#{1,6})\s+\S")


def _scan(lines: list[str]) -> list[tuple[int, str, bool]]:
    """(line no, line, fenced) for every line; fence markers count as fenced."""
    out = []
    in_fence = False
    for i, line in enumerate(lines, 1):
        if _FENCE_RX.match(line):
            in_fence = not in_fence
            out.append((i, line, True))
            continue
        out.append((i, line, in_fence))
    return out


def _content_lines(lines: list[str]) -> list[tuple[int, str]]:
    """(line no, line) for every line outside fenced code blocks."""
    return [(i, line) for i, line, fenced in _scan(lines) if not fenced]


def _heading_level(line: str) -> int:
    m = _HEADING_RX.match(line)
    return len(m.group("hashes")) if m else 0


# Markdown link target (no spaces; the same regex serves the index check).
_LINK_RX = re.compile(r"\]\(([^)\s]+)\)")
_SCHEME_RX = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def resolve_link(rel: str, target: str) -> str | None:
    """Vault-relative path a link on page *rel* points at, or None when it is
    not a vault link (external URL, in-page anchor, escapes the vault).

    A leading slash is vault-absolute (`/wiki/now.md`, the Obsidian/GitHub
    style), never a host path; a `#fragment` is dropped; `%20` is decoded.
    """
    if target.startswith("#") or _SCHEME_RX.match(target):
        return None
    path = unquote(target.split("#", 1)[0])
    if path.startswith("/"):
        resolved = posixpath.normpath(path.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
    if resolved == ".." or resolved.startswith("../"):
        return None
    return resolved


def page_findings(root: Path, wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    return (
        _link_findings(root, wiki_files)
        + _empty_section_findings(wiki_files)
        + _duplicate_findings(wiki_files)
        + _archive_findings(wiki_files)
        + _status_findings(wiki_files)
    )


# ---------------------------------------------------------------------------
# Links: every relative link target on a wiki page exists in the vault.
# ---------------------------------------------------------------------------

_LINK_HEADING = "Broken links"


def _link_findings(root: Path, wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        is_index = posixpath.basename(rel) == "index.md"
        for i, line in _content_lines(lines):
            for target in _LINK_RX.findall(line):
                resolved = resolve_link(rel, target)
                if resolved is None:
                    continue
                # A wiki page missing from an index is the index check's
                # finding, worded for the index; don't report it twice.
                if is_index and resolved.startswith("wiki/") and resolved.endswith(".md"):
                    continue
                if not (root / resolved).exists():
                    findings.append((
                        _LINK_HEADING,
                        f"{rel}:{i}: link to {target} resolves to nothing — "
                        f"fix the path or drop the link",
                    ))
    return findings


# ---------------------------------------------------------------------------
# Empty sections: a "##"-or-deeper heading with nothing under it before the
# next heading of the same or a higher level (or the end of the page). A
# section holding only subsections is not empty, and neither is one holding
# only a code block. The dashboard is exempt — an empty Waiting section
# there means nothing is waiting.
# ---------------------------------------------------------------------------

_EMPTY_HEADING = "Empty sections"


def _empty_section_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        if rel == _NOW_PATH:
            continue
        scanned = _scan(lines)
        for idx, (i, line, fenced) in enumerate(scanned):
            level = 0 if fenced else _heading_level(line)
            if level < 2:
                continue
            following = next(
                ((text, f) for _, text, f in scanned[idx + 1 :] if text.strip()), None
            )
            if following is None or (
                not following[1] and 0 < _heading_level(following[0]) <= level
            ):
                findings.append((
                    _EMPTY_HEADING,
                    f"{rel}:{i}: empty section {line.strip()!r} — fill it or remove the heading",
                ))
    return findings


# ---------------------------------------------------------------------------
# Duplicate bullets: the same bullet line twice on one page is a paste
# accident, not content. Short lines ("- sí") repeat legitimately. The
# dashboard repeats items across its sections by design.
# ---------------------------------------------------------------------------

_DUPLICATE_HEADING = "Duplicate lines"

_BULLET_RX = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")
_MIN_DUPLICATE_LEN = 20


def _duplicate_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        if rel == _NOW_PATH:
            continue
        first_seen: dict[str, int] = {}
        for i, line in _content_lines(lines):
            m = _BULLET_RX.match(line)
            if m is None:
                continue
            text = " ".join(m.group("text").split()).casefold()
            if len(text) < _MIN_DUPLICATE_LEN:
                continue
            if text in first_seen:
                findings.append((
                    _DUPLICATE_HEADING,
                    f"{rel}:{i}: duplicate of line {first_seen[text]} — delete one",
                ))
            else:
                first_seen[text] = i
    return findings


# ---------------------------------------------------------------------------
# Archive threshold: ~15 done tasks on a page is where the schema says to
# move the oldest into an "## Archive" section. Done tasks already under an
# archive heading (Archive / Archivo / Arxiu) don't count.
# ---------------------------------------------------------------------------

_ARCHIVE_HEADING = "Done-task archive threshold"

_DONE_TASK_RX = re.compile(r"^\s*- \[x\]", re.IGNORECASE)
_ARCHIVE_SECTION_RX = re.compile(r"archiv|arxiu", re.IGNORECASE)
_MAX_DONE_TASKS = 15


def _archive_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        if rel == _NOW_PATH or rel.startswith("wiki/archive/"):
            continue
        active: dict[int, str] = {}  # heading level → heading text, current chain
        count = 0
        for _, line in _content_lines(lines):
            level = _heading_level(line)
            if level:
                active = {lv: text for lv, text in active.items() if lv < level}
                active[level] = line
                continue
            if _DONE_TASK_RX.match(line) and not any(
                _ARCHIVE_SECTION_RX.search(text) for text in active.values()
            ):
                count += 1
        if count > _MAX_DONE_TASKS:
            findings.append((
                _ARCHIVE_HEADING,
                f"{rel}: {count} done tasks outside an archive section — move the "
                f"oldest into an '## Archive' section at the bottom of the page",
            ))
    return findings


# ---------------------------------------------------------------------------
# Status paragraph: a project/area page's first bold label is its Status
# (localized: Estado, Estat, …). Empty, or opening with a known stub phrase,
# means the page was created with a placeholder that ingest never replaced.
# Pages with no label at all (a template stashed under projects/) are left
# alone — a finding there would nag forever.
# ---------------------------------------------------------------------------

_STATUS_HEADING = "Status paragraphs"

_STATUS_RX = re.compile(r"^\*\*(?P<label>[^*:]{1,30}):\*\*\s*(?P<text>.*)$")
_STUB_RX = re.compile(
    r"^(?:sin contenido|sin actividad(?: de \w+)? registrada|no content|nothing (?:yet|here)"
    r"|todo\b|tbd\b|placeholder|por (?:definir|rellenar|completar)|sense contingut"
    r"|per (?:definir|omplir))",
    re.IGNORECASE,
)
_STATUS_SCAN_LINES = 10


def _status_findings(wiki_files: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    findings = []
    for rel, lines in wiki_files:
        if not (rel.startswith("wiki/projects/") or rel.startswith("wiki/areas/")):
            continue
        for i, line in _content_lines(lines)[:_STATUS_SCAN_LINES]:
            m = _STATUS_RX.match(line)
            if m is None:
                continue
            text = m.group("text").strip()
            label = m.group("label")
            if not text:
                findings.append((
                    _STATUS_HEADING,
                    f"{rel}:{i}: {label} paragraph is empty — write the page's current state",
                ))
            elif _STUB_RX.match(text):
                findings.append((
                    _STATUS_HEADING,
                    f'{rel}:{i}: {label} paragraph is a placeholder ("{text}") — '
                    f"write the page's current state",
                ))
            break  # only the first label on a page is its Status
    return findings
