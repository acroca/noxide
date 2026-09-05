"""Tests for the dynamic model catalog (parsing /models, defaults, merging)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.models import (
    DEFAULT_VENDORS,
    FetchedModel,
    ModelCapabilities,
    ModelOption,
    latest_of_family,
    merge_options,
    parse_capabilities,
    parse_models,
    resolve_startup,
    slug_for,
)


def _payload() -> dict:
    return {
        "data": [
            {
                "id": "claude-fable-5",
                "name": "Claude Fable 5",
                "vendor": "Anthropic",
                "model_picker_enabled": True,
                "policy": {"state": "enabled"},
            },
            {
                "id": "claude-opus-4.8",
                "name": "Claude Opus 4.8",
                "vendor": "Anthropic",
                "model_picker_enabled": True,
                "policy": {"state": "enabled"},
            },
            {
                "id": "gpt-5.5",
                "name": "GPT-5.5",
                "vendor": "OpenAI",
                "model_picker_enabled": True,
                "policy": {"state": "enabled"},
            },
            # filtered: vendor not whitelisted
            {
                "id": "grok-4.6",
                "name": "Grok 4.6",
                "vendor": "xAI",
                "model_picker_enabled": True,
            },
            # filtered: not picker-enabled (legacy/internal models)
            {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "vendor": "Azure OpenAI",
                "model_picker_enabled": False,
            },
            # filtered: policy present but not enabled for this account
            {
                "id": "gpt-5.3-codex",
                "name": "GPT-5.3-Codex",
                "vendor": "OpenAI",
                "model_picker_enabled": True,
                "policy": {"state": "unconfigured"},
            },
            # kept: no policy object at all means no gating
            {
                "id": "gpt-5-mini",
                "name": "GPT-5 mini",
                "vendor": "Azure OpenAI",
                "model_picker_enabled": True,
            },
            # filtered: no id
            {"name": "broken", "vendor": "OpenAI", "model_picker_enabled": True},
        ]
    }


# ------------------------------------------------------------------
# parse_models
# ------------------------------------------------------------------


def test_parse_models_filters_vendor_picker_and_policy() -> None:
    models = parse_models(_payload(), DEFAULT_VENDORS)

    assert [m.id for m in models] == [
        "claude-fable-5",
        "claude-opus-4.8",
        "gpt-5.5",
        "gpt-5-mini",
    ]


def test_parse_models_respects_custom_vendor_list() -> None:
    models = parse_models(_payload(), ["Anthropic"])

    assert {m.vendor for m in models} == {"Anthropic"}


def test_parse_models_falls_back_to_id_when_name_missing() -> None:
    payload = {
        "data": [{"id": "claude-x", "vendor": "Anthropic", "model_picker_enabled": True}]
    }

    models = parse_models(payload, DEFAULT_VENDORS)

    assert models[0].name == "claude-x"


def test_parse_models_empty_payload() -> None:
    assert parse_models({}, DEFAULT_VENDORS) == []


# ------------------------------------------------------------------
# slug_for
# ------------------------------------------------------------------


def test_slug_strips_claude_prefix() -> None:
    assert slug_for("claude-fable-5") == "fable-5"


def test_slug_keeps_other_ids() -> None:
    assert slug_for("gpt-5.5") == "gpt-5.5"


# ------------------------------------------------------------------
# latest_of_family
# ------------------------------------------------------------------


def _opus(*versions: str) -> list[FetchedModel]:
    return [
        FetchedModel(id=f"claude-opus-{v}", name=f"Claude Opus {v}", vendor="Anthropic")
        for v in versions
    ]


def test_latest_of_family_picks_highest_version() -> None:
    latest = latest_of_family(_opus("4.7", "5", "4.8"), "claude-opus")

    assert latest is not None
    assert latest.id == "claude-opus-5"


def test_latest_of_family_point_release_beats_major() -> None:
    latest = latest_of_family(_opus("5", "5.1"), "claude-opus")

    assert latest is not None
    assert latest.id == "claude-opus-5.1"


def test_latest_of_family_ignores_other_families() -> None:
    models = _opus("4.8") + [
        FetchedModel(id="claude-sonnet-5", name="Claude Sonnet 5", vendor="Anthropic")
    ]

    latest = latest_of_family(models, "claude-opus")

    assert latest is not None
    assert latest.id == "claude-opus-4.8"


def test_latest_of_family_none_when_no_match() -> None:
    assert latest_of_family(_opus("5"), "claude-haiku") is None


# ------------------------------------------------------------------
# merge_options
# ------------------------------------------------------------------


def test_merge_config_aliases_come_first_and_keep_their_names() -> None:
    fetched = [FetchedModel(id="claude-fable-5", name="Claude Fable 5", vendor="Anthropic")]

    options = merge_options({"sonnet": "claude-sonnet-5"}, fetched)

    assert list(options) == ["sonnet", "fable-5"]
    assert options["sonnet"] == ModelOption(id="claude-sonnet-5", label="sonnet")
    assert options["fable-5"] == ModelOption(id="claude-fable-5", label="Claude Fable 5")


def test_merge_replaces_config_alias_with_the_fetched_display_entry() -> None:
    """A model present in the live catalog shows under its API display name;
    the config alias for the same id only serves as the offline fallback."""
    fetched = [FetchedModel(id="claude-sonnet-5", name="Claude Sonnet 5", vendor="Anthropic")]

    options = merge_options({"sonnet": "claude-sonnet-5"}, fetched)

    assert options == {
        "sonnet-5": ModelOption(id="claude-sonnet-5", label="Claude Sonnet 5")
    }


def test_merge_never_overwrites_an_existing_alias() -> None:
    # A config alias that collides with a fetched model's slug wins.
    fetched = [FetchedModel(id="claude-fable-5", name="Claude Fable 5", vendor="Anthropic")]

    options = merge_options({"fable-5": "my-pinned-model"}, fetched)

    assert options["fable-5"].id == "my-pinned-model"


def test_merge_sorts_anthropic_before_other_vendors() -> None:
    fetched = [
        FetchedModel(id="gpt-5.5", name="GPT-5.5", vendor="OpenAI"),
        FetchedModel(id="claude-fable-5", name="Claude Fable 5", vendor="Anthropic"),
    ]

    options = merge_options({}, fetched)

    assert list(options) == ["fable-5", "gpt-5.5"]


# ------------------------------------------------------------------
# resolve_startup
# ------------------------------------------------------------------

_CONFIG_MODELS = {"sonnet": "claude-sonnet-5"}


def test_resolve_startup_picks_latest_of_family() -> None:
    fetched = _opus("4.8", "5")

    options, alias, model_id = resolve_startup(
        _CONFIG_MODELS, "sonnet", fetched, "claude-opus"
    )

    assert alias == "opus-5"
    assert model_id == "claude-opus-5"
    assert options[alias].id == "claude-opus-5"


def test_resolve_startup_uses_the_fetched_slug_even_when_config_aliases_the_id() -> None:
    options, alias, model_id = resolve_startup(
        {"opus": "claude-opus-5"}, "opus", _opus("5"), "claude-opus"
    )

    assert alias == "opus-5"
    assert model_id == "claude-opus-5"


def test_resolve_startup_without_family_keeps_config_default() -> None:
    options, alias, model_id = resolve_startup(_CONFIG_MODELS, "sonnet", _opus("5"), "")

    assert alias == "sonnet"
    assert model_id is None


def test_resolve_startup_family_not_found_keeps_config_default() -> None:
    options, alias, model_id = resolve_startup(
        _CONFIG_MODELS, "sonnet", _opus("5"), "claude-haiku"
    )

    assert alias == "sonnet"
    assert model_id is None


# ------------------------------------------------------------------
# supported_endpoints
# ------------------------------------------------------------------


def _endpoint_payload() -> dict:
    return {
        "data": [
            {
                "id": "claude-fable-5",
                "vendor": "Anthropic",
                "model_picker_enabled": True,
                "supported_endpoints": ["/v1/messages", "/chat/completions"],
            },
            {
                "id": "gpt-6-astra",
                "vendor": "OpenAI",
                "model_picker_enabled": True,
                "supported_endpoints": ["/responses", "ws:/responses"],
            },
            # filtered: neither endpoint the client speaks
            {
                "id": "gpt-embed",
                "vendor": "OpenAI",
                "model_picker_enabled": True,
                "supported_endpoints": ["/embeddings"],
            },
            # kept: catalog shape without the field means no gating
            {"id": "gpt-5-mini", "vendor": "Azure OpenAI", "model_picker_enabled": True},
        ]
    }


def test_parse_models_keeps_models_on_either_supported_endpoint() -> None:
    models = parse_models(_endpoint_payload(), DEFAULT_VENDORS)

    assert [m.id for m in models] == ["claude-fable-5", "gpt-6-astra", "gpt-5-mini"]


def test_parse_capabilities_reads_endpoints_and_structured_outputs() -> None:
    payload = {
        "data": [
            {
                "id": "gpt-6-astra",
                "capabilities": {"supports": {"structured_outputs": True}},
                "supported_endpoints": ["/responses", "ws:/responses"],
            },
            {"id": "claude-fable-5", "supported_endpoints": ["/chat/completions"]},
            {"name": "no id"},
        ]
    }

    caps = parse_capabilities(payload)

    assert caps == {
        "gpt-6-astra": ModelCapabilities(
            structured_outputs=True, endpoints=("/responses", "ws:/responses")
        ),
        "claude-fable-5": ModelCapabilities(
            structured_outputs=False, endpoints=("/chat/completions",)
        ),
    }


def test_capabilities_use_responses_only_without_chat_completions() -> None:
    assert ModelCapabilities(False, ("/responses",)).uses_responses is True
    assert ModelCapabilities(False, ("/responses", "/chat/completions")).uses_responses is False
    assert ModelCapabilities(False, ()).uses_responses is False
