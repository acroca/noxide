"""Dynamic model catalog: parse Copilot's /models into picker options.

The /model picker is built from two sources — the static ``[copilot.models]``
alias map in config (kept as the offline fallback and for pinned custom ids)
and the live catalog fetched from the API. Everything here is a pure function
over already-fetched data; the HTTP call lives in ``copilot.CopilotClient``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Vendors offered by default. "Azure OpenAI" is mostly legacy models, which
# the picker flag filters out, but it also hosts current ones (gpt-5-mini).
DEFAULT_VENDORS = ("Anthropic", "OpenAI", "Azure OpenAI")


@dataclass(frozen=True)
class FetchedModel:
    id: str
    name: str
    vendor: str


@dataclass(frozen=True)
class ModelOption:
    """One /model picker entry: the id sent to the API and the button label."""

    id: str
    label: str


def parse_models(payload: dict, vendors: Sequence[str]) -> list[FetchedModel]:
    """Extract selectable chat models from a /models response.

    Keeps models that are picker-enabled (Copilot's own curation — this drops
    embeddings and legacy ids), from a whitelisted vendor, and not gated by a
    disabled account policy (a missing policy object means no gating).
    """
    models: list[FetchedModel] = []
    for m in payload.get("data", []):
        if not m.get("id") or not m.get("model_picker_enabled"):
            continue
        if m.get("vendor") not in vendors:
            continue
        policy = m.get("policy")
        if policy is not None and policy.get("state") != "enabled":
            continue
        models.append(
            FetchedModel(id=m["id"], name=m.get("name") or m["id"], vendor=m["vendor"])
        )
    return models


def slug_for(model_id: str) -> str:
    """Short alias for a model id, used in the picker and the group-title suffix."""
    return model_id.removeprefix("claude-")


def _version_key(model_id: str, family: str) -> tuple[int, ...]:
    """Numeric version tuple from the id's suffix: "-4.8" → (4, 8), "-5" → (5,)."""
    suffix = model_id[len(family) :]
    return tuple(int(n) for n in re.findall(r"\d+", suffix))


def latest_of_family(models: Iterable[FetchedModel], family: str) -> FetchedModel | None:
    """The highest-versioned model whose id is ``family`` or ``family-<version>``."""
    matches = [m for m in models if m.id == family or m.id.startswith(family + "-")]
    if not matches:
        return None
    return max(matches, key=lambda m: _version_key(m.id, family))


def merge_options(
    config_models: dict[str, str], fetched: Iterable[FetchedModel]
) -> dict[str, ModelOption]:
    """Combine config aliases with the fetched catalog into picker options.

    Config aliases come first and keep their short names (the alias doubles as
    the label). Fetched models append with slug aliases and API display names,
    skipping ids the config already aliases; on a slug collision the config
    alias wins. Fetched entries sort Anthropic first for a stable picker.
    """
    options = {
        alias: ModelOption(id=model_id, label=alias)
        for alias, model_id in config_models.items()
    }
    known_ids = set(config_models.values())
    ordered = sorted(fetched, key=lambda m: (m.vendor != "Anthropic", m.vendor, m.id))
    for m in ordered:
        slug = slug_for(m.id)
        if m.id in known_ids or slug in options:
            continue
        options[slug] = ModelOption(id=m.id, label=m.name)
    return options


def resolve_startup(
    config_models: dict[str, str],
    default_alias: str,
    fetched: Iterable[FetchedModel],
    default_family: str,
) -> tuple[dict[str, ModelOption], str, str | None]:
    """Boot-time model resolution: (picker options, default alias, model id to set).

    When ``default_family`` is set and the fetched catalog has a match, the
    newest model of that family becomes the default (model id returned so the
    caller switches the client to it); otherwise the configured default stands
    and the id is None.
    """
    fetched = list(fetched)
    options = merge_options(config_models, fetched)
    if default_family:
        latest = latest_of_family(fetched, default_family)
        if latest is not None:
            alias = next(a for a, o in options.items() if o.id == latest.id)
            return options, alias, latest.id
    return options, default_alias, None
