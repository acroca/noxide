"""Skill library: markdown procedures shipped in the package and authored in the vault.

A skill is a markdown file whose filename stem is its slug and whose first
``**Use when:**`` line is the trigger shown in the prompt menu. Only the slug
and trigger reach the system prompt; bodies are loaded on demand, so refining
a skill's steps never changes the prompt (and never invalidates the provider's
prompt cache).

Two sources, vault shadowing repo on slug collision: shipped skills stay
pristine across image upgrades, and the agent "edits" one by writing a full
copy to the vault path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The accepted marker spellings, each spelling out its own asterisks so a
# branch matches a complete marker or nothing at all. Asterisks in the trigger
# text itself are never consumed — neither stripping them line-wide nor using
# independently-optional runs (\*{0,2}) at each position achieves that.
_TRIGGER_RE = re.compile(
    r"^\s*(?:"
    r"\*\*use when\s*:\s*\*\*"  # **Use when:**
    r"|\*\*use when\*\*\s*:"  # **Use when**:
    r"|\*use when\s*:\s*\*"  # *Use when:*
    r"|\*use when\*\s*:"  # *Use when*:
    r"|use when\s*:"  # Use when:
    r")\s*(.+)$",
    re.IGNORECASE,
)
_TRIGGER_MAX = 200
# The slug arrives from the model; never join unvalidated input to a path.
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


@dataclass(frozen=True)
class Skill:
    """One discovered skill: slug, menu trigger, source, and full body.

    The body is captured during the scan — finding the trigger already
    required reading the whole file, so no second read is needed within
    that scan.
    """

    slug: str
    trigger: str
    source: str  # "repo" | "vault"
    text: str


def _trigger_from(text: str) -> str | None:
    """Return the trigger from the first ``Use when:`` line, or None if absent."""
    for raw in text.splitlines():
        match = _TRIGGER_RE.match(raw)
        if not match:
            continue
        trigger = match.group(1).strip()
        if not trigger:
            return None
        if len(trigger) > _TRIGGER_MAX:
            trigger = trigger[: _TRIGGER_MAX - 1] + "…"
        return trigger
    return None


class SkillLibrary:
    """Discovers skills in the shipped package directory and the vault."""

    def __init__(self, vault_root: Path, repo_skills_dir: Path | None = None) -> None:
        self._vault_dir = vault_root / "system" / "skills"
        if repo_skills_dir is not None:
            self._repo_dir = repo_skills_dir
        else:
            self._repo_dir = Path(str(files("assistant"))) / "skills"

    def _scan(self) -> dict[str, Skill]:
        """Slug-keyed skills; vault entries overwrite repo ones (vault shadows repo)."""
        found: dict[str, Skill] = {}
        for directory, source in ((self._repo_dir, "repo"), (self._vault_dir, "vault")):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if not path.is_file():
                    continue
                if not _SLUG_RE.match(path.stem):
                    logger.warning(
                        "skill filename is not a valid slug (lowercase letters, digits, "
                        "hyphens), excluded from menu: %s",
                        path.name,
                    )
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("skill unreadable, skipped: %s (%s)", path.name, exc)
                    continue
                trigger = _trigger_from(text)
                if trigger is None:
                    logger.warning(
                        "skill has no '**Use when:**' line, excluded from menu: %s",
                        path.name,
                    )
                    continue
                found[path.stem] = Skill(
                    slug=path.stem, trigger=trigger, source=source, text=text
                )
        return found

    def menu(self) -> str:
        """Render the prompt menu, or '' when no skills exist."""
        skills = self._scan()
        if not skills:
            return ""
        lines = [
            f"- `{skill.slug}` — {skill.trigger}"
            for skill in sorted(skills.values(), key=lambda s: s.slug)
        ]
        return "## Available skills\n\n" + "\n".join(lines)

    def _resolve(self, slug: str) -> Skill | None:
        """Look up a skill by slug, rejecting anything that is not a bare slug."""
        if not _SLUG_RE.match(slug):
            logger.warning("load_skill rejected invalid slug: %r", slug)
            return None
        return self._scan().get(slug)

    def load(self, slug: str) -> str:
        """Return a skill's full body, or the not-found sentinel."""
        skill = self._resolve(slug)
        if skill is None:
            return f"[skill not found: {slug}]"
        return skill.text

    # ------------------------------------------------------------------
    # OpenAI tool schema
    # ------------------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "load_skill",
                    "description": (
                        "Load a skill: a stored procedure for recurring work. "
                        "Call it with the skill name from the 'Available skills' "
                        "list when its 'Use when' line matches the request, then "
                        "follow the steps it returns."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Skill name from the Available skills list, e.g. weekly-review",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
        ]

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call by name."""
        if name != "load_skill":
            return f"[unknown tool: {name}]"
        slug = args.get("name", "")
        skill = self._resolve(slug)
        if skill is None:
            return f"[skill not found: {slug}]"
        logger.info("skill loaded: %s (source=%s)", slug, skill.source)
        return skill.text
