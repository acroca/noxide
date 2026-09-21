"""Tests for the agent loop using a mocked Copilot client."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.agent import (
    _AMBIENT_HEADER,
    _JOB_CLOSE_RESPONSE_FORMAT,
    Agent,
    _extract_vault_paths,
    _parse_job_close,
)
from assistant.conversations import ConversationArchive
from assistant.history import (
    _HISTORY_TOOL_RESULT_CAP,
    _HISTORY_TRIM_MARKER,
    ConversationHistory,
)
from assistant.skills import SkillLibrary
from assistant.tools import VaultTools


def _make_text_response(content: str) -> dict:
    """Create a fake Copilot text response."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _make_tool_call_response(
    tool_name: str,
    tool_args: dict,
    call_id: str = "tc1",
    finish_reason: str = "tool_calls",
    content: str | None = None,
) -> dict:
    """Create a fake Copilot response with a tool call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }
                    ],
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    return VaultTools(tmp_path)


@pytest.fixture
def agent(vault: VaultTools) -> Agent:
    return Agent(vault_tools=vault)


def test_agent_instance_name_is_in_stable_identity_prompt(vault):
    agent = Agent(vault, agent_name='Juniper')
    prompt = agent._load_system_prompt()
    assert 'Your assistant instance name is "Juniper"' in prompt
    assert 'Noxide is the software project' in prompt
    assert agent._load_system_prompt() == prompt


# ------------------------------------------------------------------
# Basic text reply
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_simple_reply(agent: Agent, vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Hello there!"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("Hi")

    assert reply == "Hello there!"


# ------------------------------------------------------------------
# Tool calling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_tool_read_file(agent: Agent, vault: VaultTools, tmp_path: Path) -> None:
    """Agent calls read_file tool then returns text."""
    (tmp_path / "memo.md").write_text("Buy milk")

    responses = [
        _make_tool_call_response("read_file", {"path": "memo.md"}),
        _make_text_response("Your memo says: Buy milk"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("What's in memo.md?")

    assert "Buy milk" in reply
    # Two calls: one tool call, one final text
    assert mock_client.chat.call_count == 2


@pytest.mark.asyncio
async def test_agent_tool_create_file(agent: Agent, vault: VaultTools) -> None:
    """Agent calls create_file then returns confirmation."""
    responses = [
        _make_tool_call_response("create_file", {"path": "note.md", "content": "Important note"}),
        _make_text_response("Saved to note.md"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Save a note")

    assert vault.read_file("note.md") == "Important note"


@pytest.mark.asyncio
async def test_agent_tool_edit_file(agent: Agent, vault: VaultTools) -> None:
    """edit_file is routed to the vault so ingest can patch one now.md line."""
    vault.write_file("wiki/now.md", "## Today\n- feed the ants (due)\n- gym\n")
    responses = [
        _make_tool_call_response(
            "edit_file",
            {
                "path": "wiki/now.md",
                "old_string": "- feed the ants (due)",
                "new_string": "- feed the ants (done)",
            },
        ),
        _make_text_response("Updated wiki/now.md"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("I fed the ants")

    assert vault.read_file("wiki/now.md") == "## Today\n- feed the ants (done)\n- gym\n"


@pytest.mark.asyncio
async def test_agent_tool_move_file(agent: Agent, vault: VaultTools, tmp_path: Path) -> None:
    """The agent-level routing must know move_file — VaultTools having the
    method is not enough (the allowlist in _dispatch_tool_unlocked answered
    [unknown tool: move_file] to a perfectly-formed call in production)."""
    vault.write_file("wiki/projects/p.md", "page")

    responses = [
        _make_tool_call_response(
            "move_file",
            {"path": "wiki/projects/p.md", "new_path": "wiki/archive/projects/p.md"},
        ),
        _make_text_response("moved"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("archive it")

    assert reply == "moved"
    assert not (tmp_path / "wiki/projects/p.md").exists()
    assert (tmp_path / "wiki/archive/projects/p.md").read_text() == "page"


@pytest.mark.asyncio
async def test_agent_tool_check_vault(agent: Agent, vault: VaultTools) -> None:
    """Agent-level routing must know check_vault too (same trap as move_file:
    a tool in VaultTools.dispatch but missing from the agent's file-tools
    allowlist is advertised yet answers [unknown tool: ...])."""
    responses = [
        _make_tool_call_response("check_vault", {}),
        _make_text_response("all clean"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("run the checks")

    assert reply == "all clean"
    tool_result = mock_client.chat.call_args_list[1].args[0][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["content"] == "[no findings]"


@pytest.mark.asyncio
async def test_agent_path_jail_in_tool_call(agent: Agent, vault: VaultTools) -> None:
    """Tool call with path escape should return error string, not crash."""
    responses = [
        _make_tool_call_response("read_file", {"path": "../../../etc/passwd"}),
        _make_text_response("Cannot access that file"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Read /etc/passwd")

    # Agent should have continued (not crashed) with permission error
    assert mock_client.chat.call_count == 2
    # The second call should have received a tool result with permission denied
    second_call_args = mock_client.chat.call_args_list[1]
    messages = second_call_args[0][0]  # first positional arg
    tool_results = [m for m in messages if m.get("role") == "tool"]
    assert any("permission" in r["content"].lower() for r in tool_results)


@pytest.mark.asyncio
async def test_agent_executes_tool_calls_despite_stop_finish_reason(
    agent: Agent, vault: VaultTools
) -> None:
    """Tool calls must run even when the API reports finish_reason='stop'.

    The Copilot API often returns 'stop' alongside tool_calls; the presence
    of tool calls is what matters, not the finish reason.
    """
    responses = [
        _make_tool_call_response(
            "create_file",
            {"path": "note.md", "content": "reminder set"},
            finish_reason="stop",
            content="¡Claro!",
        ),
        _make_text_response("Done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("Remind me")

    assert vault.read_file("note.md") == "reminder set"
    assert reply == "Done"


# ------------------------------------------------------------------
# System prompt assembly (embedded capabilities + vault AGENTS.md)
# ------------------------------------------------------------------

def test_base_prompt_present_even_without_vault_agents_md(vault: VaultTools) -> None:
    prompt = Agent(vault_tools=vault)._load_system_prompt()

    assert "personal AI assistant" in prompt
    assert "Incoming media" in prompt


def test_system_prompt_is_stable_across_runs(vault: VaultTools) -> None:
    """No timestamps in the system prompt — a changing prefix would defeat
    the provider's prompt caching on every turn."""
    agent = Agent(vault_tools=vault)

    prompt = agent._load_system_prompt()

    assert "Current datetime" not in prompt
    assert prompt == agent._load_system_prompt()


@pytest.mark.asyncio
async def test_user_message_is_stamped_with_send_time(agent: Agent) -> None:
    """The current time rides on the newest user message, not the system prompt."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("ok"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Hi")

    messages = mock_client.chat.call_args.args[0]
    assert messages[0]["role"] == "system"
    assert "Current datetime" not in messages[0]["content"]
    user_msg = [m for m in messages if m["role"] == "user"][-1]
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} local\] Hi", user_msg["content"])


@pytest.mark.asyncio
async def test_stamps_are_frozen_in_history_across_runs(agent: Agent) -> None:
    """Old messages keep their original stamp so the request prefix stays cacheable."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("ok"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        with patch("assistant.agent.datetime") as dt:
            dt.now.return_value = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
            await agent.run("first")
        with patch("assistant.agent.datetime") as dt:
            dt.now.return_value = datetime(2026, 7, 24, 10, 7, tzinfo=UTC)
            await agent.run("second")

    messages = mock_client.chat.call_args.args[0]
    users = [m["content"] for m in messages if m["role"] == "user"]
    assert users == ["[2026-07-24 10:00 local] first", "[2026-07-24 10:07 local] second"]


@pytest.mark.asyncio
async def test_stamp_is_in_the_configured_timezone(vault: VaultTools) -> None:
    """The stamp is the only clock the model gets. Handing it UTC and asking it
    to convert in its head is how UTC times got written into vault fields that
    are supposed to be local."""
    agent = Agent(vault_tools=vault, tz_name="Europe/Madrid")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("ok"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        with patch("assistant.agent.datetime") as dt:
            dt.now.return_value = datetime(2026, 7, 27, 6, 57, tzinfo=UTC)
            await agent.run("hola")

    user_msg = [m for m in mock_client.chat.call_args.args[0] if m["role"] == "user"][-1]
    assert user_msg["content"] == "[2026-07-27 08:57 local] hola"  # CEST = UTC+2


def test_wiki_schema_always_in_base_prompt(vault: VaultTools) -> None:
    """The raw-journal + compiled-wiki workflow is baseline behavior, not vault config."""
    prompt = Agent(vault_tools=vault)._load_system_prompt()

    assert "raw/journal/" in prompt
    assert "wiki/now.md" in prompt
    assert "### Ingest" in prompt
    assert "### Compile" in prompt


def test_routine_completion_rule_points_at_now_md(vault: VaultTools) -> None:
    """Regression: a confirmed routine updated routines.md but left now.md pending.

    The routines section is the rule the model reads when a routine is
    confirmed; if it enumerates the routine-completion steps without naming
    now.md, that local recipe wins over the general ingest list.
    """
    prompt = Agent(vault_tools=vault)._load_system_prompt()

    routines_section = prompt.split("### `wiki/routines.md`")[1].split("###")[0]
    assert "now.md" in routines_section


def test_ingest_reconciles_now_md_unconditionally(vault: VaultTools) -> None:
    """now.md must be read on every ingest that touches a routine/task/event.

    Framing the read as conditional on now.md being stale asks the model to
    evaluate a predicate about a file it has not opened — which it resolves by
    guessing "probably fine" and skipping the read.
    """
    prompt = Agent(vault_tools=vault)._load_system_prompt()
    ingest_section = prompt.split("### Ingest")[1].split("### Query")[0]

    assert "read `now.md`" in ingest_section
    # No blanket permission to defer to the nightly rebuild.
    assert "compile still reconciles anything ingest misses" not in ingest_section


def test_vault_agents_md_appended_after_embedded_base(vault: VaultTools) -> None:
    vault.write_file("AGENTS.md", "## My conventions\nAlways answer in Catalan.")

    prompt = Agent(vault_tools=vault)._load_system_prompt()

    assert "Always answer in Catalan." in prompt
    # Vault instructions come later so they take precedence
    assert prompt.index("Incoming media") < prompt.index("Always answer in Catalan.")


def test_capability_sections_track_wired_features(vault: VaultTools) -> None:
    bare = Agent(vault_tools=vault)._load_system_prompt()
    assert "Time-based requests" not in bare
    assert "Proactive follow-up" not in bare
    assert "Web research" not in bare

    assert "Extracting attachment contents" not in bare

    full = Agent(
        vault_tools=vault,
        schedule_dispatcher=lambda name, args: "",
        research_fn=AsyncMock(),
        extract_fn=AsyncMock(),
    )._load_system_prompt()
    assert "Time-based requests" in full
    assert "Proactive follow-up" in full
    assert "Web research" in full
    assert "Extracting attachment contents" in full


# ------------------------------------------------------------------
# Skills: rules section early, volatile menu last
# ------------------------------------------------------------------

def _library(tmp_path: Path) -> SkillLibrary:
    """A library whose vault root is the same tmp_path the `vault` fixture uses."""
    repo_dir = tmp_path / "repo-skills"
    repo_dir.mkdir(exist_ok=True)
    return SkillLibrary(tmp_path, repo_dir)


def _write_skill(tmp_path: Path, slug: str, trigger: str, steps: str = "1. do it") -> None:
    d = tmp_path / "system" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        f"# {slug}\n\n**Use when:** {trigger}\n\n## Steps\n{steps}\n", encoding="utf-8"
    )


def test_skills_rules_section_only_present_when_library_wired(
    vault: VaultTools, tmp_path: Path
) -> None:
    bare = Agent(vault_tools=vault)._load_system_prompt()
    assert "Available skills" not in bare

    wired = Agent(vault_tools=vault, skills=_library(tmp_path))._load_system_prompt()
    assert "load_skill" in wired


def test_skills_menu_comes_after_vault_prompt(vault: VaultTools, tmp_path: Path) -> None:
    vault.write_file("AGENTS.md", "vault-level conventions marker")
    _write_skill(tmp_path, "weekly-review", "asked for the weekly review.")

    prompt = Agent(vault_tools=vault, skills=_library(tmp_path))._load_system_prompt()

    assert prompt.index("vault-level conventions marker") < prompt.index("## Available skills")
    assert prompt.rstrip().endswith("- `weekly-review` — asked for the weekly review.")


def test_editing_a_skill_body_leaves_the_system_prompt_identical(
    vault: VaultTools, tmp_path: Path
) -> None:
    """The cache property: refining steps must not change the prompt prefix."""
    _write_skill(tmp_path, "weekly-review", "asked for the weekly review.", steps="1. old")
    agent = Agent(vault_tools=vault, skills=_library(tmp_path))
    before = agent._load_system_prompt()

    _write_skill(
        tmp_path, "weekly-review", "asked for the weekly review.", steps="1. new\n2. more"
    )

    assert agent._load_system_prompt() == before


def test_adding_a_skill_changes_the_system_prompt(vault: VaultTools, tmp_path: Path) -> None:
    _write_skill(tmp_path, "weekly-review", "asked for the weekly review.")
    agent = Agent(vault_tools=vault, skills=_library(tmp_path))
    before = agent._load_system_prompt()

    _write_skill(tmp_path, "receipt-filing", "a receipt photo arrives.")

    after = agent._load_system_prompt()
    assert after != before
    assert "- `receipt-filing` — a receipt photo arrives." in after


def test_no_menu_block_when_no_skills_exist(vault: VaultTools, tmp_path: Path) -> None:
    prompt = Agent(vault_tools=vault, skills=_library(tmp_path))._load_system_prompt()

    assert "## Available skills" not in prompt


# ------------------------------------------------------------------
# History management
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_history_preserved(agent: Agent, vault: VaultTools) -> None:
    """Second message includes history from first."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("First message")
        await agent.run("Second message")

    # Second call's messages should include both user messages
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    user_messages = [m for m in second_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert any("First message" in c for c in contents)
    assert any("Second message" in c for c in contents)




@pytest.mark.asyncio
async def test_agent_clear_history_forgets_previous_messages(agent: Agent) -> None:
    """After clear_history, the next run starts with a fresh context."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("First message")
        agent.clear_history()
        await agent.run("Second message")

    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    user_messages = [m for m in second_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert not any("First message" in c for c in contents)
    assert any("Second message" in c for c in contents)




@pytest.mark.asyncio
async def test_agent_handles_legacy_function_call_shape(
    agent: Agent, vault: VaultTools
) -> None:
    """Some backends return a single legacy 'function_call' instead of 'tool_calls'."""
    legacy_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "function_call": {
                        "name": "create_file",
                        "arguments": json.dumps({"path": "note.md", "content": "legacy"}),
                    },
                },
                "finish_reason": "function_call",
            }
        ],
        "usage": {},
    }
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[legacy_response, _make_text_response("Done")])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("Save it")

    assert vault.read_file("note.md") == "legacy"
    assert reply == "Done"


@pytest.mark.asyncio
async def test_agent_logs_raw_message_when_tool_calls_missing(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    """finish_reason='tool_calls' with no parseable calls must log the raw message."""
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "¡Claro!", "tool_calls": None},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=response)

    with (
        patch("assistant.copilot.get_client", return_value=mock_client),
        caplog.at_level("WARNING", logger="assistant.agent"),
    ):
        reply = await agent.run("Inicializa el vault")

    assert reply == "¡Claro!"
    assert any("no tool calls parsed" in r.message for r in caplog.records)
    assert any("¡Claro!" in r.message for r in caplog.records)  # raw msg included


def test_completed_history_omits_tool_protocol() -> None:
    history = ConversationHistory(exchanges=3)
    history.begin_run()
    history.append({"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}]})
    history.append({"role": "tool", "tool_call_id": "tc1", "content": "result"})
    history.append({"role": "user", "content": "hi"})
    history.append({"role": "assistant", "content": "hello"})
    history.finish_run()

    messages = history.messages()

    assert messages[0]["role"] != "tool"
    assert [m["role"] for m in messages] == ["user", "assistant"]


# ------------------------------------------------------------------
# History compaction: tool results from finished runs are trimmed
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prior_run_tool_results_are_omitted_on_next_run(
    agent: Agent, vault: VaultTools, tmp_path: Path
) -> None:
    """Completed tool traces do not ride later requests; the exchange survives."""
    big = "x" * (_HISTORY_TOOL_RESULT_CAP * 3)
    (tmp_path / "big.md").write_text(big)
    responses = [
        _make_tool_call_response("read_file", {"path": "big.md"}),
        _make_text_response("Read it"),
        _make_text_response("OK"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Read the big file")
        await agent.run("Thanks")

    next_run_messages = mock_client.chat.call_args_list[2][0][0]
    tool_msgs = [m for m in next_run_messages if m.get("role") == "tool"]
    assert tool_msgs == []
    assert any("Read the big file" in m["content"] for m in next_run_messages)
    assert any(m["content"] == "Read it" for m in next_run_messages)


@pytest.mark.asyncio
async def test_tool_results_stay_full_within_a_run(
    agent: Agent, vault: VaultTools, tmp_path: Path
) -> None:
    """The running turn keeps its own tool output intact — only *finished*
    runs get compacted, or the model would lose content it just asked for."""
    big = "x" * (_HISTORY_TOOL_RESULT_CAP * 3)
    (tmp_path / "big.md").write_text(big)
    responses = [
        _make_tool_call_response("read_file", {"path": "big.md"}),
        _make_text_response("Read it"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Read the big file")

    same_run_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in same_run_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert big in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_completed_exchange_is_byte_stable_across_runs(
    agent: Agent, vault: VaultTools, tmp_path: Path
) -> None:
    """Projecting completed text must not keep changing its cached prefix."""
    big = "x" * (_HISTORY_TOOL_RESULT_CAP * 3)
    (tmp_path / "big.md").write_text(big)
    responses = [
        _make_tool_call_response("read_file", {"path": "big.md"}),
        _make_text_response("Read it"),
        _make_text_response("OK"),
        _make_text_response("OK again"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("Read the big file")
        await agent.run("Thanks")
        await agent.run("Thanks again")

    second_run = mock_client.chat.call_args_list[2][0][0]
    third_run = mock_client.chat.call_args_list[3][0][0]
    assert second_run[:3] == third_run[:3]
    assert not any(m.get("role") == "tool" for m in third_run)


def test_compact_tool_results_only_trims_long_tool_results() -> None:
    """Short tool results and non-tool messages are left untouched."""
    history = ConversationHistory(exchanges=5)
    history.append(
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]}
    )
    history.append({"role": "tool", "tool_call_id": "tc1", "content": "short"})
    history.append({"role": "tool", "tool_call_id": "tc2", "content": "y" * 9000})
    long_user = "x" * 9000
    history.append({"role": "user", "content": long_user})

    history.compact_tool_results()

    msgs = history.messages()
    assert msgs[1]["content"] == "short"
    assert msgs[2]["content"] == "y" * _HISTORY_TOOL_RESULT_CAP + _HISTORY_TRIM_MARKER
    assert msgs[3]["content"] == long_user


# ------------------------------------------------------------------
# Max iterations guard
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_max_iterations(agent: Agent, vault: VaultTools) -> None:
    """Agent should stop after max iterations and return error message."""
    # Always return a tool call — agent should stop after 20 iterations
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_tool_call_response("list_files", {"glob": "*.md"})
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("List everything")

    assert "maximum" in reply.lower() or "iteration" in reply.lower()
    assert mock_client.chat.call_count == 20


# ------------------------------------------------------------------
# send_message tool
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_send_message_awaits_sender_with_text_only(vault: VaultTools) -> None:
    """The send_message tool awaits the send_message_fn with the text alone;
    the model gets no destination to pick."""
    mock_send = AsyncMock(return_value=None)
    agent = Agent(vault_tools=vault, send_message_fn=mock_send)
    schema = next(t for t in agent._all_tools() if t["function"]["name"] == "send_message")
    assert list(schema["function"]["parameters"]["properties"]) == ["text"]

    responses = [
        _make_tool_call_response("send_message", {"text": "Hello there!"}),
        _make_text_response("Sent."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("Send hello")

    assert reply == "Sent."
    mock_send.assert_awaited_once_with("Hello there!")


# ------------------------------------------------------------------
# Web research: on_research callback
# ------------------------------------------------------------------

def _research_agent(vault: VaultTools) -> tuple[Agent, AsyncMock]:
    research_fn = AsyncMock(return_value="research summary")
    agent = Agent(vault_tools=vault, research_fn=research_fn)
    return agent, research_fn


@pytest.mark.asyncio
async def test_agent_research_fires_on_research_callback(vault: VaultTools) -> None:
    agent, research_fn = _research_agent(vault)
    on_research = AsyncMock()

    responses = [
        _make_tool_call_response("research", {"question": "weather in Girona?"}),
        _make_text_response("Sunny."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("weather?", on_research=on_research)

    on_research.assert_awaited_once()
    research_fn.assert_awaited_once_with("weather in Girona?")
    assert reply == "Sunny."


@pytest.mark.asyncio
async def test_agent_on_research_fires_once_across_multiple_calls(vault: VaultTools) -> None:
    agent, _ = _research_agent(vault)
    on_research = AsyncMock()

    responses = [
        _make_tool_call_response("research", {"question": "first?"}),
        _make_tool_call_response("research", {"question": "second?"}, call_id="tc2"),
        _make_text_response("Done."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("dig deep", on_research=on_research)

    on_research.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_on_research_not_fired_for_other_tools(vault: VaultTools) -> None:
    agent, _ = _research_agent(vault)
    on_research = AsyncMock()

    responses = [
        _make_tool_call_response("list_files", {"glob": "*.md"}),
        _make_text_response("Done."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("list notes", on_research=on_research)

    on_research.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_on_research_failure_does_not_break_run(vault: VaultTools) -> None:
    agent, research_fn = _research_agent(vault)
    on_research = AsyncMock(side_effect=Exception("reaction failed"))

    responses = [
        _make_tool_call_response("research", {"question": "weather?"}),
        _make_text_response("Sunny."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("weather?", on_research=on_research)

    research_fn.assert_awaited_once()
    assert reply == "Sunny."


# ------------------------------------------------------------------
# Attachment extraction tool
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_extract_attachment_tool(vault: VaultTools) -> None:
    extract_fn = AsyncMock(return_value="[PDF, 1 page(s), text layer]\n\nTotal: 99 EUR")
    agent = Agent(vault_tools=vault, extract_fn=extract_fn)

    responses = [
        _make_tool_call_response("extract_attachment", {"path": "attachments/a.pdf"}),
        _make_text_response("The invoice total is 99 EUR."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("what's the total on that invoice?")

    extract_fn.assert_awaited_once_with("attachments/a.pdf")
    assert "99 EUR" in reply


def test_extract_tool_schema_gated_on_callback(vault: VaultTools) -> None:
    names_without = {
        t["function"]["name"] for t in Agent(vault_tools=vault)._all_tools()
    }
    names_with = {
        t["function"]["name"]
        for t in Agent(vault_tools=vault, extract_fn=AsyncMock())._all_tools()
    }
    assert "extract_attachment" not in names_without
    assert "extract_attachment" in names_with


# ------------------------------------------------------------------
# Vision (image attachments)
# ------------------------------------------------------------------

_DATA_URL = "data:image/jpeg;base64,QUJD"


@pytest.mark.asyncio
async def test_image_is_sent_as_multimodal_content(agent: Agent) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("A nice plant."))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("what plant is this?", image_data_urls=[_DATA_URL])

    assert reply == "A nice plant."
    messages = mock_client.chat.call_args.args[0]
    user_msg = [m for m in messages if m["role"] == "user"][-1]
    assert isinstance(user_msg["content"], list)
    texts = [p["text"] for p in user_msg["content"] if p["type"] == "text"]
    images = [p for p in user_msg["content"] if p["type"] == "image_url"]
    assert len(texts) == 1
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} local\] what plant is this\?", texts[0]
    )
    assert images == [{"type": "image_url", "image_url": {"url": _DATA_URL}}]


@pytest.mark.asyncio
async def test_several_images_ride_the_same_user_message(agent: Agent) -> None:
    """A burst of photos reaches the model as one turn carrying every image."""
    second_url = "data:image/jpeg;base64,REVG"
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("two receipts"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("file these", image_data_urls=[_DATA_URL, second_url])

    messages = mock_client.chat.call_args.args[0]
    user_msg = [m for m in messages if m["role"] == "user"][-1]
    images = [p["image_url"]["url"] for p in user_msg["content"] if p["type"] == "image_url"]
    assert images == [_DATA_URL, second_url]


@pytest.mark.asyncio
async def test_image_is_resent_on_every_iteration_of_the_same_run(agent: Agent) -> None:
    """During tool-call iterations the model must still see the image."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("list_files", {"glob": "*.md"}),
        _make_text_response("Filed it."),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("file this", image_data_urls=[_DATA_URL])

    for call in mock_client.chat.call_args_list:
        messages = call.args[0]
        user_msg = [m for m in messages if m["role"] == "user"][-1]
        assert isinstance(user_msg["content"], list), "image dropped mid-run"


@pytest.mark.asyncio
async def test_image_is_not_stored_in_history_for_later_runs(agent: Agent) -> None:
    """Follow-up turns must not re-send image tokens: history keeps text only."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("ok"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("look at this", image_data_urls=[_DATA_URL])
        await agent.run("thanks")

    # Second request: the earlier user message must be plain text again
    messages = mock_client.chat.call_args.args[0]
    earlier_user_msgs = [m for m in messages if m["role"] == "user"][:-1]
    assert earlier_user_msgs, "expected first user message in history"
    for m in earlier_user_msgs:
        assert isinstance(m["content"], str)


# ------------------------------------------------------------------
# Scheduled job runs (run_job)
# ------------------------------------------------------------------

def _job_agent(vault: VaultTools, captured: list[str]) -> Agent:
    async def mock_send(text: str) -> None:
        captured.append(text)

    return Agent(vault_tools=vault, send_message_fn=mock_send)


@pytest.mark.asyncio
async def test_run_job_drops_closing_text_after_send_message(vault: VaultTools) -> None:
    """The model delivers the reminder via send_message; its closing text
    ("mensaje enviado") must not become a second message."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    responses = [
        _make_tool_call_response("send_message", {"text": "Reminder: call the doctor"}),
        _make_text_response("Mensaje enviado."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == ["Reminder: call the doctor"]


@pytest.mark.asyncio
async def test_run_job_forwards_reply_when_model_sends_nothing(vault: VaultTools) -> None:
    """Safety net: a job that never calls send_message still delivers its reply."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Reminder: call the doctor"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == ["Reminder: call the doctor"]


@pytest.mark.asyncio
async def test_run_job_forwards_reply_when_send_message_fails(vault: VaultTools) -> None:
    """A failed send_message doesn't count as delivered — the safety net fires."""
    captured: list[str] = []
    attempts = 0

    async def flaky_send(text: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("push down")
        captured.append(text)

    agent = Agent(vault_tools=vault, send_message_fn=flaky_send)

    responses = [
        _make_tool_call_response("send_message", {"text": "Reminder: call the doctor"}),
        _make_text_response("Could not deliver."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == ["Could not deliver."]


@pytest.mark.asyncio
async def test_run_job_stays_silent_on_silent_sentinel(vault: VaultTools) -> None:
    """A reminder that finds its purpose already met (routine logged, task
    closed) replies '[silent]' and nothing reaches the user."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response("[silent] pill already logged today")
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == []


@pytest.mark.asyncio
async def test_run_job_sends_nothing_for_empty_reply(vault: VaultTools) -> None:
    """No send_message call and an empty final reply → no message at all."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response(""))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Log the weather silently")

    assert captured == []


# ------------------------------------------------------------------
# Threads: a root message and its replies, with the newest threads of the
# chat riding the live turn as ambient background (never stored)
# ------------------------------------------------------------------

_AMBIENT_STAMP_RX = re.compile(r"--- \d{4}-\d{2}-\d{2} \d{2}:\d{2} ---")


@pytest.fixture
def archive(tmp_path: Path) -> ConversationArchive:
    state = tmp_path / "state"
    state.mkdir()
    archive = ConversationArchive(state)
    yield archive
    archive.close()


@pytest.fixture
def threaded(vault: VaultTools, archive: ConversationArchive) -> Agent:
    """Archive-backed agent whose chat 7 is the pinned home chat (space ``general``)."""
    return Agent(vault_tools=vault, archive=archive)


def _live_turn(sent: list[dict]) -> str:
    """The content of the live user message of a request (always the last message)."""
    assert sent[-1]["role"] == "user"
    return str(sent[-1]["content"])


def _delivery_shapes() -> list[tuple[str, list[dict]]]:
    """The three ways a scheduled run delivers: the tool, the close JSON, the raw fallback."""
    return [
        ("send_message tool", [
            _make_tool_call_response("send_message", {"text": "Reminder: take the pill"}),
            _make_text_response('{"silent": false, "message": null}'),
        ]),
        ("close JSON", [_make_text_response('{"silent": false, "message": "Reminder: take the pill"}')]),
        ("raw fallback", [_make_text_response("Reminder: take the pill")]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape,responses", _delivery_shapes(), ids=lambda v: v if isinstance(v, str) else "")
async def test_scheduled_delivery_is_ambient_background_for_the_next_root(
    vault: VaultTools, archive: ConversationArchive, shape: str, responses: list[dict],
) -> None:
    """A delivery archived as a thread root (what __main__'s sender does) reaches
    the next new thread through the ambient block, marked as sent by the bot, and
    the stored context of that thread keeps the bare message."""

    async def send(text: str) -> None:
        archive.insert("general", "assistant", text, "done")

    agent = Agent(vault_tools=vault, archive=archive, send_message_fn=send)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[*responses, _make_text_response("Logged!")])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")
        assert await agent.run("Pill taken") == "Logged!"

    sent = mock_client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user"], "a new root carries no other thread's messages"
    live = _live_turn(sent)
    assert live.index("Pill taken") < live.index(_AMBIENT_HEADER)
    assert "Assistant (sent from a scheduled run): Reminder: take the pill" in live
    assert len(_AMBIENT_STAMP_RX.findall(live)) == 1
    assert "sent from a scheduled run" not in _load_system(sent)
    stored = archive.load_context("general")
    assert [r["role"] for r in stored] == ["user", "assistant"]
    assert stored[0]["content"].endswith("] Pill taken")
    assert _AMBIENT_HEADER not in str(stored) and "Reminder" not in str(stored)


def _load_system(sent: list[dict]) -> str:
    return str(sent[0]["content"])


@pytest.mark.asyncio
async def test_ambient_block_lists_user_threads_with_their_reply(
    threaded: Agent, archive: ConversationArchive,
) -> None:
    """A new root sees the recent threads' root and latest reply as background,
    oldest first, and no other thread's messages in the message list."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_text_response("A1"), _make_text_response("A2"), _make_text_response("A3"),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await threaded.run("first question")
        await threaded.run("second question")
        await threaded.run("unrelated")

    sent = mock_client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    live = _live_turn(sent)
    assert live.startswith("[") and "] unrelated" in live.split("\n")[0]
    assert live.count(_AMBIENT_HEADER) == 1
    assert len(_AMBIENT_STAMP_RX.findall(live)) == 2
    assert live.index("User: first question") < live.index("Assistant: A1") < live.index("User: second question") < live.index("Assistant: A2")
    assert "sent from a scheduled run):" not in live
    stored = archive.load_context("general")
    assert [r["content"] for r in stored if r["role"] == "assistant"] == ["A1", "A2", "A3"]
    assert _AMBIENT_HEADER not in str(stored)


@pytest.mark.asyncio
async def test_reply_runs_with_its_thread_exchanges(threaded: Agent, archive: ConversationArchive) -> None:
    """A reply (``reply_to``, or a pre-inserted row that replies) sees the parent
    thread's completed exchanges as messages, joins the thread in the archive, and
    gets no ambient block for the thread it is already in."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_text_response("A1"), _make_text_response("A2"), _make_text_response("A3"),
    ])
    root = archive.insert("general", "user", "first question", "queued")

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await threaded.run("first question", message_id=root)
        assert await threaded.run("and then?", reply_to=root) == "A2"
        sent = mock_client.chat.call_args.args[0]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert "] first question" in sent[1]["content"] and sent[2]["content"] == "A1"
        assert "] and then?" in _live_turn(sent)
        assert _AMBIENT_HEADER not in str(sent)
        # The web flow inserts the row first, replying to any message of the thread.
        later = archive.insert("general", "user", "more", "queued",
                               reply_to=archive.db.execute(
                                   "SELECT id FROM messages WHERE text='and then?'").fetchone()[0])
        assert await threaded.run("more", message_id=later) == "A3"

    sent = mock_client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in sent if m["role"] == "assistant"] == ["A1", "A2"]
    rows = archive.db.execute("SELECT id, thread, reply_to FROM messages ORDER BY created").fetchall()
    assert {r["thread"] for r in rows} == {root}
    assert len(rows) == 6
    assert archive.get(later)["reply_to"] != root and archive.thread_of(later) == root
    assert archive.get(f"reply:{later}")["thread"] == root


@pytest.mark.asyncio
async def test_reply_to_a_delivery_sees_the_delivery(threaded: Agent, archive: ConversationArchive) -> None:
    """A scheduled-run delivery is a thread root archived as a message row, never a
    context record, so a reply to it restored an empty thread and ran blind to the
    very reminder it answered while the ambient block excluded that thread; "Hecho"
    on a pill reminder got "the pills, the stock check, or both?" (2026-09-20). The
    delivery is the thread's first assistant message on every restore, and stays
    out of the stored context."""
    root = archive.insert("general", "assistant", "Reminder: take the pill", "done")
    archive.insert("general", "assistant", "Reminder: check the pill stock", "done")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[_make_text_response("Logged!"), _make_text_response("Noted.")])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        assert await threaded.run("Done", reply_to=root) == "Logged!"
        sent = mock_client.chat.call_args.args[0]
        assert [m["role"] for m in sent] == ["system", "assistant", "user"]
        assert sent[1]["content"] == "Reminder: take the pill"
        assert "] Done" in _live_turn(sent)
        assert "Reminder: check the pill stock" in _live_turn(sent), "other threads stay ambient background"
        assert "Reminder: take the pill" not in _live_turn(sent), "the thread's own root is not repeated as background"
        # The settled history is rebuilt from the archive for the next reply, delivery first.
        assert await threaded.run("and the stock is fine", reply_to=root) == "Noted."

    sent = mock_client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in sent[1:4]] == ["Reminder: take the pill", sent[2]["content"], "Logged!"]
    assert [r["role"] for r in archive.load_context("general", root)] == ["user", "assistant", "user", "assistant"]


async def _run_records_concurrency(agent: Agent, calls: list) -> dict:
    active = {"now": 0, "max": 0}

    async def chat(messages, tools, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return _make_text_response("ok")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)
    with patch("assistant.copilot.get_client", return_value=mock_client):
        await asyncio.gather(*calls)
    return active


async def test_different_threads_of_one_chat_run_in_parallel(threaded: Agent) -> None:
    """Two new roots in the one chat are two threads: neither waits for the other."""
    active = await _run_records_concurrency(threaded, [
        threaded.run("root one"),
        threaded.run("root two"),
    ])
    assert active["max"] == 2


async def test_replies_in_one_thread_are_serialized(threaded: Agent, archive: ConversationArchive) -> None:
    """Two replies to one thread must never interleave: concurrent appends into
    one history produce tool orderings the API rejects. FIFO: arrival order."""
    root = archive.insert("general", "user", "root", "queued")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("ok"))
    with patch("assistant.copilot.get_client", return_value=mock_client):
        await threaded.run("root", message_id=root)
    active = await _run_records_concurrency(threaded, [
        threaded.run("first reply", reply_to=root),
        threaded.run("second reply", reply_to=root),
    ])
    assert active["max"] == 1
    msgs = threaded._get_history(root).messages()
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert "root" in users[0] and "first reply" in users[1] and "second reply" in users[2]


async def test_settled_thread_history_is_released_and_rebuilt_from_the_archive(
    threaded: Agent, archive: ConversationArchive,
) -> None:
    """A finished thread's history leaves memory; a reply restores it from the
    archive. A failed run keeps its history for the hot retry that resumes it."""
    from assistant.copilot import CopilotUnavailableError

    root = archive.insert("general", "user", "first", "queued")
    broken = archive.insert("general", "user", "broken", "queued")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_text_response("A1"), _make_text_response("A2"),
        CopilotUnavailableError("HTTP 502"), _make_text_response("Resumed"),
    ])
    with patch("assistant.copilot.get_client", return_value=mock_client):
        await threaded.run("first", message_id=root)
        assert root not in threaded._histories
        await threaded.run("again", reply_to=root)
        sent = mock_client.chat.call_args.args[0]
        assert "] first" in sent[1]["content"] and sent[2]["content"] == "A1"
        assert root not in threaded._histories

        with pytest.raises(CopilotUnavailableError):
            await threaded.run("broken", message_id=broken)
        assert broken in threaded._histories
        assert not threaded._histories[broken].is_settled()
        assert await threaded.resume(broken) == "Resumed"
        assert broken not in threaded._histories
    assert archive.get(broken)["status"] == "done" and archive.reply(broken) == "Resumed"


@pytest.mark.asyncio
async def test_clear_history_hides_earlier_threads_from_the_ambient_block(
    threaded: Agent, archive: ConversationArchive,
) -> None:
    """/clear opens a new generation: deliveries and threads before it are no
    longer background for new messages (they stay searchable)."""
    archive.insert("general", "assistant", "Reminder: take the pill", "done")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Logged!"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await threaded.run("Pill taken")
        assert "Reminder: take the pill" in _live_turn(mock_client.chat.call_args.args[0])
        threaded.clear_history()
        await threaded.run("Pill taken again")

    sent = mock_client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    live = _live_turn(sent)
    assert _AMBIENT_HEADER not in live and "Reminder" not in live
    assert archive.generation("general") == 1


# ------------------------------------------------------------------
# Fire-time state snapshot for scheduled runs
# ------------------------------------------------------------------


def _delivering_agent(vault: VaultTools) -> Agent:
    """Agent whose send fn accepts every delivery."""

    async def send(text: str) -> None:
        return None

    return Agent(vault_tools=vault, send_message_fn=send)


def test_extract_vault_paths_finds_slashed_md_paths() -> None:
    prompt = (
        "Pregunta cómo fue e ingiere en `wiki/projects/busqueda-empleo/empresas/framer.md` "
        "y en wiki/projects/busqueda-empleo/index.md. Ver wiki/projects/busqueda-empleo/index.md."
    )
    assert _extract_vault_paths(prompt) == [
        "wiki/projects/busqueda-empleo/empresas/framer.md",
        "wiki/projects/busqueda-empleo/index.md",
    ]


def test_extract_vault_paths_ignores_bare_basenames_and_caps() -> None:
    # A bare basename is not a vault-relative path; more than 5 paths cap at 5.
    many = " ".join(f"wiki/p{i}.md" for i in range(8))
    assert _extract_vault_paths("mira framer.md") == []
    assert len(_extract_vault_paths(many)) == 5


def test_schedule_prompt_documents_state_snapshot(vault: VaultTools) -> None:
    """The standing contract must tell scheduled runs the snapshot exists and
    to check it, and job authors to name the pages a job depends on."""
    agent = Agent(vault_tools=vault, schedule_dispatcher=lambda name, args: "")
    prompt = agent._load_system_prompt()
    assert "state snapshot" in prompt


def test_schedule_prompt_uses_canonical_reminder_marker(vault: VaultTools) -> None:
    """The trace rule must name the checkable [reminder:<id>] token —
    free-form markers are invisible to check_vault."""
    agent = Agent(vault_tools=vault, schedule_dispatcher=lambda name, args: "")
    assert "[reminder:" in agent._load_system_prompt()


@pytest.mark.asyncio
async def test_run_job_injects_referenced_page_state(vault: VaultTools) -> None:
    """The live turn of a scheduled run carries the current content of every
    vault page the job prompt names — the premise check cannot be skipped."""
    vault.write_file("wiki/x.md", "**Estado:** ya realizado y registrado")
    agent = _delivering_agent(vault)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response('{"silent": true, "message": null}'))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Pregunta a Albert cómo fue X e ingiere en wiki/x.md.")

    messages = mock_client.chat.call_args.args[0]
    user_msg = [m for m in messages if m["role"] == "user"][-1]
    assert "state snapshot" in user_msg["content"]
    assert "--- wiki/x.md ---" in user_msg["content"]
    assert "**Estado:** ya realizado y registrado" in user_msg["content"]


@pytest.mark.asyncio
async def test_run_job_snapshot_not_stored_in_history(vault: VaultTools) -> None:
    """Snapshots ride the live turn only: chat-0 history keeps the bare
    prompt, or every later job run would re-pay old snapshots."""
    vault.write_file("wiki/x.md", "**Estado:** ya realizado")
    agent = _delivering_agent(vault)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response('{"silent": true, "message": null}'))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Revisa wiki/x.md y avisa si procede.")
        await agent.run_job("Otra tarea sin páginas.")

    messages = mock_client.chat.call_args.args[0]
    earlier_user_msgs = [m for m in messages if m["role"] == "user"][:-1]
    assert earlier_user_msgs, "expected first job's user message in history"
    for m in earlier_user_msgs:
        assert "state snapshot" not in m["content"]


@pytest.mark.asyncio
async def test_run_job_snapshot_inlines_not_found_sentinel(vault: VaultTools) -> None:
    """A named page that no longer exists is itself information: the job's
    premise is broken and the model should see that, not guess."""
    agent = _delivering_agent(vault)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response('{"silent": true, "message": null}'))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Ingiere la respuesta en wiki/gone.md.")

    user_msg = [m for m in mock_client.chat.call_args.args[0] if m["role"] == "user"][-1]
    assert "--- wiki/gone.md ---" in user_msg["content"]
    assert "[file not found" in user_msg["content"]


@pytest.mark.asyncio
async def test_run_job_without_page_references_gets_no_snapshot(vault: VaultTools) -> None:
    agent = _delivering_agent(vault)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response('{"silent": true, "message": null}'))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Recuérdale a Albert que estire la espalda.")

    user_msg = [m for m in mock_client.chat.call_args.args[0] if m["role"] == "user"][-1]
    assert "state snapshot" not in user_msg["content"]


@pytest.mark.asyncio
async def test_run_job_snapshot_truncates_long_pages(vault: VaultTools) -> None:
    vault.write_file("wiki/big.md", "x" * 6000)
    agent = _delivering_agent(vault)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response('{"silent": true, "message": null}'))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Revisa wiki/big.md.")

    user_msg = [m for m in mock_client.chat.call_args.args[0] if m["role"] == "user"][-1]
    assert "x" * 5000 in user_msg["content"]
    assert "x" * 5001 not in user_msg["content"]
    assert "[truncated]" in user_msg["content"]


# ------------------------------------------------------------------
# Job-close JSON contract
# ------------------------------------------------------------------

def test_parse_job_close_plain_object() -> None:
    assert _parse_job_close('{"silent": true, "message": null}') == {
        "silent": True, "message": None,
    }


def test_parse_job_close_fenced_object() -> None:
    reply = '```json\n{"silent": false, "message": "Toma la pastilla"}\n```'
    assert _parse_job_close(reply) == {"silent": False, "message": "Toma la pastilla"}


def test_parse_job_close_object_wrapped_in_prose() -> None:
    reply = 'Run complete.\n{"silent": true, "message": null}\nBye.'
    assert _parse_job_close(reply) == {"silent": True, "message": None}


def test_parse_job_close_rejects_garbage() -> None:
    assert _parse_job_close("Ya está registrada (08:19). Reminder resuelto.") is None


def test_parse_job_close_rejects_wrong_types() -> None:
    assert _parse_job_close('{"silent": "yes", "message": null}') is None
    assert _parse_job_close('{"silent": true, "message": 42}') is None
    assert _parse_job_close('{"message": "no silent field"}') is None


@pytest.mark.asyncio
async def test_run_job_json_close_silent_suppresses(vault: VaultTools) -> None:
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response('{"silent": true, "message": null}')
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == []


@pytest.mark.asyncio
async def test_run_job_json_close_delivers_message(vault: VaultTools) -> None:
    """The delivered text is the schema's message field, not the raw reply."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response('{"silent": false, "message": "Toma la pastilla"}')
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == ["Toma la pastilla"]


@pytest.mark.asyncio
async def test_run_job_json_close_message_dropped_after_send_message(
    vault: VaultTools,
) -> None:
    """A run that already delivered via send_message must not repeat itself."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    responses = [
        _make_tool_call_response("send_message", {"text": "Toma la pastilla"}),
        _make_text_response('{"silent": false, "message": "Toma la pastilla"}'),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == ["Toma la pastilla"]


@pytest.mark.asyncio
async def test_run_job_sentinel_anywhere_suppresses(vault: VaultTools) -> None:
    """Legacy fallback: a misplaced [silent] still means stand down (the
    2026-07-29 bug: 'Ya está registrada (08:19). Reminder resuelto.\\n\\n[silent]'
    was delivered verbatim because only the prefix was checked)."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response(
            "Ya está registrada (08:19). Reminder resuelto.\n\n[silent]"
        )
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == []


@pytest.mark.asyncio
async def test_run_job_prefixes_scheduled_run_marker(vault: VaultTools) -> None:
    """The job prompt reaches the model tagged so the closing contract applies."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response('{"silent": true, "message": null}')
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Log the weather")

    messages = mock_client.chat.call_args.args[0]
    assert "[scheduled run] Log the weather" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_run_job_requests_job_close_response_format(vault: VaultTools) -> None:
    """Job runs ask the API for the schema; enforcement is a no-op today but
    activates by itself the day Copilot honors response_format."""
    captured: list[str] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response('{"silent": true, "message": null}')
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Log the weather")

    assert mock_client.chat.call_args.kwargs["response_format"] == _JOB_CLOSE_RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_run_without_response_format_passes_none(agent: Agent) -> None:
    """Chat runs are plain text: no response_format on their requests."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Hola"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("hi")

    assert mock_client.chat.call_args.kwargs.get("response_format") is None


def test_schedule_prompt_states_job_close_contract() -> None:
    """Drift guard: the prompt must describe the same fields the parser expects
    and the [scheduled run] marker run_job prepends."""
    prompt = (
        Path(__file__).parent.parent / "src" / "assistant" / "prompts" / "schedule.md"
    ).read_text()
    assert '"silent"' in prompt
    assert '"message"' in prompt
    assert "[scheduled run]" in prompt


# ------------------------------------------------------------------
# Usage recording
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_records_usage_event(agent: Agent) -> None:
    mock_client = MagicMock()
    response = _make_text_response("Hello there!")
    response["model"] = "claude-sonnet-4.6"
    mock_client.chat = AsyncMock(return_value=response)

    with patch("assistant.copilot.get_client", return_value=mock_client), \
         patch("assistant.agent.usage") as mock_usage:
        await agent.run("hi")

    mock_usage.record.assert_called_once_with(
        "agent",
        "claude-sonnet-4.6",
        {"prompt_tokens": 10, "completion_tokens": 5},
    )


# ------------------------------------------------------------------
# Skills tool dispatch
# ------------------------------------------------------------------


def test_load_skill_tool_offered_only_when_library_wired(
    vault: VaultTools, tmp_path: Path
) -> None:
    bare_names = [t["function"]["name"] for t in Agent(vault_tools=vault)._all_tools()]
    assert "load_skill" not in bare_names

    wired = Agent(vault_tools=vault, skills=_library(tmp_path))
    assert "load_skill" in [t["function"]["name"] for t in wired._all_tools()]


@pytest.mark.asyncio
async def test_agent_dispatches_load_skill(vault: VaultTools, tmp_path: Path) -> None:
    _write_skill(tmp_path, "weekly-review", "asked for the weekly review.", steps="1. read now.md")
    agent = Agent(vault_tools=vault, skills=_library(tmp_path))

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        side_effect=[
            _make_tool_call_response("load_skill", {"name": "weekly-review"}),
            _make_text_response("Done"),
        ]
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run("weekly review please")

    tool_result = [
        m for m in mock_client.chat.call_args.args[0] if m.get("role") == "tool"
    ][-1]
    assert "read now.md" in tool_result["content"]
    assert reply == "Done"


# ------------------------------------------------------------------
# Concurrency: per-conversation serialization
# ------------------------------------------------------------------

async def test_same_conversation_runs_are_serialized(agent: Agent) -> None:
    """Two runs on one history must never interleave: concurrent appends
    into one history produce tool orderings the API rejects."""
    active = {"now": 0, "max": 0}

    async def chat(messages, tools, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return _make_text_response("ok")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await asyncio.gather(
            agent.run("first"),
            agent.run("second"),
        )

    assert active["max"] == 1
    msgs = agent._get_history().messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert "first" in users[0]
    assert "second" in users[1]




async def test_scheduled_job_runs_in_parallel_with_user_chat(vault: VaultTools, archive: ConversationArchive) -> None:
    """Scheduled jobs run on their own unthreaded history and must not queue
    behind a user thread."""
    agent = Agent(vault_tools=vault, archive=archive)
    active = {"now": 0, "max": 0}

    async def chat(messages, tools, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1
        return _make_text_response("ok")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await asyncio.gather(
            agent.run("user turn"),
            agent.run_job("job prompt"),
        )

    assert active["max"] == 2


# ------------------------------------------------------------------
# Vault backup integration
# ------------------------------------------------------------------

class _StubBackup:
    """Captures schedule_commit calls; lock mirrors VaultBackup's."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.commits: list[tuple[set[str], str, str]] = []

    def schedule_commit(self, paths, trigger: str, response: str) -> None:
        self.commits.append((set(paths), trigger, response))


@pytest.mark.asyncio
async def test_run_schedules_backup_commit_with_touched_paths(vault: VaultTools) -> None:
    backup = _StubBackup()
    agent = Agent(vault_tools=vault, backup=backup)
    responses = [
        _make_tool_call_response("create_file", {"path": "notes/x.md", "content": "hi"}),
        _make_text_response("Done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("make a note")

    assert backup.commits == [({"notes/x.md"}, "make a note", "Done")]


@pytest.mark.asyncio
async def test_run_without_vault_writes_schedules_no_commit(vault: VaultTools) -> None:
    backup = _StubBackup()
    agent = Agent(vault_tools=vault, backup=backup)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Just chatting"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("hi")

    assert backup.commits == []


@pytest.mark.asyncio
async def test_schedule_tool_attributes_the_schedule_file(vault: VaultTools) -> None:
    backup = _StubBackup()
    agent = Agent(
        vault_tools=vault,
        schedule_dispatcher=lambda name, args: "Scheduled.",
        schedule_schemas=[],
        backup=backup,
    )
    responses = [
        _make_tool_call_response("schedule", {"prompt": "water plants", "when": "tomorrow"}),
        _make_text_response("Will do"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run("remind me")

    assert backup.commits == [({"system/schedule.md"}, "remind me", "Will do")]


@pytest.mark.asyncio
async def test_mutating_tool_waits_for_backup_lock(vault: VaultTools, tmp_path: Path) -> None:
    backup = _StubBackup()
    agent = Agent(vault_tools=vault, backup=backup)
    responses = [
        _make_tool_call_response("create_file", {"path": "x.md", "content": "hi"}),
        _make_text_response("Done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    async with backup.lock:
        with patch("assistant.copilot.get_client", return_value=mock_client):
            run = asyncio.create_task(agent.run("write"))
            await asyncio.sleep(0.05)
            assert not (tmp_path / "x.md").exists()
        # Lock released here; the pending write may now proceed.
    await run

    assert (tmp_path / "x.md").exists()


# ------------------------------------------------------------------
# Tool failure logging
# ------------------------------------------------------------------
# A production move_file failure left zero log trace: error results went back
# to the model as strings and the operator saw nothing. Every failed tool call
# must leave a WARNING with the tool name, the error, and the arguments.

@pytest.mark.asyncio
async def test_error_tool_result_is_logged_as_warning(
    agent: Agent, vault: VaultTools, caplog: pytest.LogCaptureFixture
) -> None:
    responses = [
        _make_tool_call_response("edit_file", {"path": "nope.md", "old_string": "a", "new_string": "b"}),
        _make_text_response("done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with caplog.at_level(logging.WARNING, logger="assistant.agent"):
        with patch("assistant.copilot.get_client", return_value=mock_client):
            await agent.run("edit")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "edit_file" in r.getMessage() and "[file not found" in r.getMessage()
        for r in warnings
    )


@pytest.mark.asyncio
async def test_tool_exception_is_logged_with_traceback(
    agent: Agent, vault: VaultTools, caplog: pytest.LogCaptureFixture
) -> None:
    """A dispatch exception (the model passed wrong argument names) must log
    the traceback, not just silently become a [tool error: ...] string."""
    vault.write_file("wiki/projects/p.md", "page")
    responses = [
        # Wrong argument name: dispatch does args["new_path"] -> KeyError
        _make_tool_call_response("move_file", {"path": "wiki/projects/p.md", "destination": "wiki/archive/p.md"}),
        _make_text_response("done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with caplog.at_level(logging.WARNING, logger="assistant.agent"):
        with patch("assistant.copilot.get_client", return_value=mock_client):
            reply = await agent.run("archive it")

    assert reply == "done"  # the loop survived the exception as before
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    joined = "\n".join(warnings)
    assert "move_file" in joined
    assert "KeyError" in joined
    assert "new_path" in joined


@pytest.mark.asyncio
async def test_successful_tool_call_logs_no_warning(
    agent: Agent, vault: VaultTools, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "memo.md").write_text("Buy milk")
    responses = [
        _make_tool_call_response("read_file", {"path": "memo.md"}),
        _make_text_response("done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with caplog.at_level(logging.WARNING, logger="assistant.agent"):
        with patch("assistant.copilot.get_client", return_value=mock_client):
            await agent.run("read")

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_unparseable_tool_arguments_are_logged(
    agent: Agent, vault: VaultTools, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed arguments JSON used to silently become {} — the model's call
    then fails on missing keys with no trace of the real cause."""
    bad = _make_tool_call_response("read_file", {})
    bad["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    responses = [bad, _make_text_response("done")]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with caplog.at_level(logging.WARNING, logger="assistant.agent"):
        with patch("assistant.copilot.get_client", return_value=mock_client):
            await agent.run("go")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("read_file" in m and "arguments" in m for m in warnings)


@pytest.mark.asyncio
async def test_turn_log_names_the_tools_called(
    agent: Agent, vault: VaultTools, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`tools=1` alone made it impossible to reconstruct a run from the logs."""
    (tmp_path / "memo.md").write_text("x")
    responses = [
        _make_tool_call_response("read_file", {"path": "memo.md"}),
        _make_text_response("done"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with caplog.at_level(logging.INFO, logger="assistant.agent"):
        with patch("assistant.copilot.get_client", return_value=mock_client):
            await agent.run("read")

    infos = [r.getMessage() for r in caplog.records if "agent turn=" in r.getMessage()]
    assert any("read_file" in m for m in infos)


def test_paths_touched_maps_move_file_to_both_paths() -> None:
    """A move is a deletion at the source and an addition at the destination;
    the backup commit must stage both."""
    from assistant.agent import _paths_touched

    assert _paths_touched(
        "move_file",
        {"path": "wiki/projects/boat.md", "new_path": "wiki/archive/projects/boat.md"},
    ) == {"wiki/projects/boat.md", "wiki/archive/projects/boat.md"}


def test_move_file_holds_the_backup_lock() -> None:
    """move_file mutates the vault, so its dispatch must serialize with
    backup commits like every other mutating tool."""
    from assistant.agent import _VAULT_MUTATING_TOOLS

    assert "move_file" in _VAULT_MUTATING_TOOLS


# ------------------------------------------------------------------
# Outage resume: the failed turn is picked up in place
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_resumes_pending_turn(agent: Agent) -> None:
    """The failed turn is still in history; replay appends only a resume note,
    never a second copy of the message (which could redo tool side effects)."""
    history = agent._get_history()
    history.append({"role": "user", "content": "[2026-08-17 15:08 local] pastilla tomada"})
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Anotado."))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.resume()

    assert reply == "Anotado."
    sent = mock_client.chat.call_args.args[0]
    user_contents = [str(m.get("content")) for m in sent if m.get("role") == "user"]
    assert sum("pastilla tomada" in c for c in user_contents) == 1
    assert "Copilot" in user_contents[-1]


@pytest.mark.asyncio
async def test_resume_resumes_after_tool_tail(agent: Agent) -> None:
    """A mid-run failure leaves history ending in tool results; the resume note
    lets the model continue instead of re-running the whole turn."""
    history = agent._get_history()
    history.append({"role": "user", "content": "[2026-08-17 15:08 local] log my run"})
    history.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "tc1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    })
    history.append({"role": "tool", "tool_call_id": "tc1", "content": "file contents"})
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Done."))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.resume()

    assert reply == "Done."
    sent = mock_client.chat.call_args.args[0]
    assert sent[-1]["role"] == "user"
    assert "Copilot" in str(sent[-1]["content"])


@pytest.mark.asyncio
async def test_resume_superseded_returns_none(agent: Agent) -> None:
    """A later successful run already answered this turn — replaying would
    double-process it."""
    history = agent._get_history()
    history.append({"role": "user", "content": "[2026-08-17 15:08 local] pastilla tomada"})
    history.append({"role": "assistant", "content": "Anotado."})
    mock_client = MagicMock()
    mock_client.chat = AsyncMock()

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.resume()

    assert reply is None
    mock_client.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_after_clear_returns_none(agent: Agent) -> None:
    """/clear during the outage means the conversation was deliberately forgotten."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock()

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.resume()

    assert reply is None
    mock_client.chat.assert_not_awaited()




@pytest.mark.asyncio
async def test_resume_failed_attempts_leave_history_unchanged(agent: Agent) -> None:
    """The web app re-invokes resume on every manual retry during a
    sustained outage; each failed attempt must be a no-op on history or the
    accumulated notes evict the original failed turn from the deque."""
    from assistant.copilot import CopilotUnavailableError

    history = agent._get_history()
    history.append({"role": "user", "content": "[2026-08-17 15:08 local] pastilla tomada"})
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        CopilotUnavailableError("HTTP 502"),
        CopilotUnavailableError("HTTP 502"),
        _make_text_response("Anotado."),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        for _ in range(2):
            with pytest.raises(CopilotUnavailableError):
                await agent.resume()
            assert len(history.messages()) == 1, "failed replay attempt left a note behind"
        reply = await agent.resume()

    assert reply == "Anotado."
    sent = mock_client.chat.call_args.args[0]
    notes = [m for m in sent if m.get("role") == "user" and "Copilot" in str(m.get("content"))]
    assert len(notes) == 1, "the model saw stale notes from failed attempts"




@pytest.mark.parametrize("args", [None, [], 3, {"path": ["x.md"]}, {"path": {"bad": True}}])
async def test_invalid_tool_argument_types_return_results(agent: Agent, args) -> None:
    from assistant.responses import build_payload, parse_response

    response = parse_response({"status": "completed", "output": [{
        "type": "function_call", "call_id": "bad", "name": "create_file",
        "arguments": json.dumps(args),
    }]})
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[response, _make_text_response("recovered"),
                                        _make_text_response("next reply")])
    with patch("assistant.copilot.get_client", return_value=client):
        assert await agent.run("write") == "recovered"
        assert await agent.run("next") == "next reply"
    messages = client.chat.call_args_list[1].args[0]
    assert messages[-1]["tool_call_id"] == "bad"
    assert messages[-1]["content"].startswith("[tool error:")
    items = build_payload("m", messages, None, None)["input"]
    assert items[-1]["type"] == "function_call_output"
    assert items[-1]["call_id"] == "bad"


@pytest.mark.parametrize("calls_per_turn,turns", [(2, 16), (40, 1)])
async def test_active_run_keeps_task_images_and_tool_results(
    vault: VaultTools, calls_per_turn: int, turns: int,
) -> None:
    agent = Agent(vault)
    client = MagicMock()
    replies = []
    for turn in range(turns):
        response = _make_tool_call_response("list_files", {})
        response["choices"][0]["message"]["tool_calls"] = [
            {"id": f"c{turn}-{n}", "type": "function",
             "function": {"name": "list_files", "arguments": "{}"}}
            for n in range(calls_per_turn)
        ]
        replies.append(response)
    client.chat = AsyncMock(side_effect=[*replies, _make_text_response("done")])
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run("original task", image_data_urls=[_DATA_URL], transient_context="snapshot")
    messages = client.chat.call_args.args[0]
    user = next(m for m in messages if m["role"] == "user")
    assert "original task" in user["content"][0]["text"]
    assert "snapshot" in user["content"][0]["text"]
    assert user["content"][1]["image_url"]["url"] == _DATA_URL
    assert sum(m["role"] == "tool" for m in messages) == calls_per_turn * turns
    completed = agent._get_history().messages()
    assert len(completed) == 2
    assert "original task" in completed[0]["content"]
    assert completed[1]["content"] == "done"


async def test_hot_retries_keep_all_unanswered_messages_beyond_history_limit(vault: VaultTools) -> None:
    from assistant.copilot import CopilotUnavailableError

    agent = Agent(vault, history_exchanges=3)
    client = MagicMock()
    client.chat = AsyncMock(side_effect=CopilotUnavailableError("502"))
    with patch("assistant.copilot.get_client", return_value=client):
        for n in range(6):
            with pytest.raises(CopilotUnavailableError):
                await agent.run(f"pending-{n}")
        before = agent._get_history().messages()
        for _ in range(3):
            with pytest.raises(CopilotUnavailableError):
                await agent.resume()
            assert agent._get_history().messages() == before
        client.chat = AsyncMock(return_value=_make_text_response("handled all"))
        assert await agent.resume() == "handled all"
        sent = client.chat.call_args.args[0]
        for n in range(6):
            assert any(f"pending-{n}" in str(m.get("content")) for m in sent)
        assert await agent.resume() is None


async def test_failed_retry_preserves_full_deque_and_partial_tool_output(vault: VaultTools) -> None:
    from assistant.copilot import CopilotUnavailableError

    agent = Agent(vault, history_exchanges=3)
    history = agent._get_history()
    for n in range(3):
        history.append({"role": "user", "content": f"old-{n}"})
    client = MagicMock()
    client.chat = AsyncMock(side_effect=CopilotUnavailableError("502"))
    before = history.messages()
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.resume()
        assert history.messages() == before
        history.append({"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]})
        history.append({"role": "tool", "tool_call_id": "c", "content": "x" * 6000})
        with pytest.raises(CopilotUnavailableError):
            await agent.resume()
        assert history.messages()[-1]["content"] == "x" * 6000
        agent.clear_history()
        assert await agent.resume() is None


async def test_hot_retry_rechecks_superseding_after_waiting_for_run_lock(agent: Agent) -> None:
    from assistant.copilot import CopilotUnavailableError

    started = asyncio.Event()
    finish = asyncio.Event()

    async def reply(messages, tools, **kwargs):
        started.set()
        await finish.wait()
        return _make_text_response("handled pending work")

    client = MagicMock()
    client.chat = AsyncMock(side_effect=CopilotUnavailableError("502"))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run("pending")
        client.chat = AsyncMock(side_effect=reply)
        run = asyncio.create_task(agent.run("followup"))
        await started.wait()
        retry = asyncio.create_task(agent.resume())
        await asyncio.sleep(0)
        finish.set()
        await run
        assert await retry is None
    client.chat.assert_awaited_once()


async def test_partial_work_is_retained_across_outage_without_reexecution(vault: VaultTools) -> None:
    from assistant.copilot import CopilotUnavailableError

    vault.write_file("large.md", "x" * 6000)
    backup = _StubBackup()
    agent = Agent(vault, history_exchanges=2, backup=backup)
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("read_file", {"path": "large.md"}, call_id="read"),
        _make_tool_call_response("append_file", {"path": "note.md", "content": "once"}, call_id="write"),
        CopilotUnavailableError("502"),
        _make_tool_call_response("append_file", {"path": "note.md", "content": " twice"}, call_id="resume"),
        _make_text_response("done"),
    ])
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run("original task")
        assert await agent.resume() == "done"
    recovery_messages = client.chat.call_args_list[3].args[0]
    assert any("original task" in str(m.get("content")) for m in recovery_messages)
    assert any("x" * 6000 in str(m.get("content")) for m in recovery_messages)
    assert vault.read_file("note.md") == "once twice"
    assert backup.commits[0][0] == {"note.md"}


@pytest.mark.parametrize("error_kind", ["request", "http400"])
@pytest.mark.parametrize("hot_retry", [False, True])
async def test_terminal_chat_error_preserves_failure_tail_and_allows_next_run_compaction(
    vault: VaultTools, error_kind: str, hot_retry: bool,
) -> None:
    import httpx

    from assistant.responses import RequestError

    error = RequestError("context length exceeded") if error_kind == "request" else (
        httpx.HTTPStatusError(
            "context length exceeded", request=httpx.Request("POST", "https://example.com"),
            response=httpx.Response(400),
        )
    )
    vault.write_file("large.md", "x" * 6000)
    agent = Agent(vault, history_exchanges=4)
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("read_file", {"path": "large.md"}, call_id="first"),
        _make_tool_call_response("read_file", {"path": "large.md"}, call_id="second"),
        error,
        _make_text_response("recovered"),
    ])
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(type(error)):
            await agent.run("read both")
        history = agent._get_history()
        assert len(history.messages()) == 5
        assert not history._unfinished
        assert history.messages()[-1]["role"] == "tool"
        assert "x" * 6000 in history.messages()[-1]["content"]
        if hot_retry:
            reply = await agent.resume()
        else:
            reply = await agent.run("try again")
        assert reply == "recovered"  # not falsely superseded by the rejection
    sent = client.chat.call_args.args[0]
    outputs = [m["content"] for m in sent if m["role"] == "tool"]
    assert len(outputs) == 2
    assert all(output == "x" * _HISTORY_TOOL_RESULT_CAP + _HISTORY_TRIM_MARKER for output in outputs)
    assert len(history.messages()) <= 4


@pytest.mark.parametrize("ending", ["outage", "cancel", "cap"])
async def test_nonterminal_chat_end_preserves_unbounded_uncompacted_work(
    vault: VaultTools, ending: str,
) -> None:
    from assistant.agent import MAX_ITERATIONS_REPLY
    from assistant.copilot import CopilotUnavailableError

    vault.write_file("large.md", "x" * 6000)
    agent = Agent(vault, history_exchanges=4)
    replies = [
        _make_tool_call_response("read_file", {"path": "large.md"}, call_id="first"),
        _make_tool_call_response("read_file", {"path": "large.md"}, call_id="second"),
    ]
    error = CopilotUnavailableError("502") if ending == "outage" else asyncio.CancelledError()
    if ending != "cap":
        replies.append(error)
    replies.append(_make_text_response("resumed"))
    client = MagicMock()
    client.chat = AsyncMock(side_effect=replies)
    with (
        patch("assistant.copilot.get_client", return_value=client),
        patch("assistant.agent._MAX_ITERATIONS", 2 if ending == "cap" else 20),
    ):
        if ending == "cap":
            assert await agent.run("original task") == MAX_ITERATIONS_REPLY
        else:
            with pytest.raises(type(error)):
                await agent.run("original task")
        history = agent._get_history()
        assert len(history.messages()) == 5
        assert history._unfinished
        assert await agent.resume() == "resumed"
    sent = client.chat.call_args.args[0]
    assert any("original task" in str(m.get("content")) for m in sent)
    outputs = [m["content"] for m in sent if m["role"] == "tool"]
    assert len(outputs) == 2
    assert all("x" * 6000 in output for output in outputs)
