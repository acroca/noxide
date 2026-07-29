"""Tests for the agent loop using a mocked Copilot client."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.agent import (
    _JOB_CLOSE_RESPONSE_FORMAT,
    Agent,
    ConversationHistory,
    _parse_job_close,
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
    return Agent(vault_tools=vault, history_size=10)


# ------------------------------------------------------------------
# Basic text reply
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_simple_reply(agent: Agent, vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Hello there!"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="Hi")

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
        reply = await agent.run(chat_id=1, user_message="What's in memo.md?")

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
        await agent.run(chat_id=1, user_message="Save a note")

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
        await agent.run(chat_id=1, user_message="I fed the ants")

    assert vault.read_file("wiki/now.md") == "## Today\n- feed the ants (done)\n- gym\n"


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
        await agent.run(chat_id=1, user_message="Read /etc/passwd")

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
        reply = await agent.run(chat_id=1, user_message="Remind me")

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
        await agent.run(chat_id=1, user_message="Hi")

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
            await agent.run(chat_id=1, user_message="first")
        with patch("assistant.agent.datetime") as dt:
            dt.now.return_value = datetime(2026, 7, 24, 10, 7, tzinfo=UTC)
            await agent.run(chat_id=1, user_message="second")

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
            await agent.run(chat_id=1, user_message="hola")

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
    assert "Multi-room topics" not in bare

    assert "Extracting attachment contents" not in bare

    full = Agent(
        vault_tools=vault,
        schedule_dispatcher=lambda name, args: "",
        research_fn=AsyncMock(),
        create_forum_topic_fn=AsyncMock(),
        extract_fn=AsyncMock(),
    )._load_system_prompt()
    assert "Time-based requests" in full
    assert "Proactive follow-up" in full
    assert "Web research" in full
    assert "Multi-room topics" in full
    assert "Extracting attachment contents" in full


def test_topic_prompt_comes_after_vault_prompt(vault: VaultTools) -> None:
    vault.write_file("AGENTS.md", "vault-level conventions marker")
    vault.write_file("system/topics/finance/AGENTS.md", "finance-topic marker")

    prompt = Agent(vault_tools=vault)._load_system_prompt(topic_slug="finance")

    assert prompt.index("vault-level conventions marker") < prompt.index("finance-topic marker")


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


def test_skills_menu_comes_after_vault_and_topic_prompts(
    vault: VaultTools, tmp_path: Path
) -> None:
    vault.write_file("AGENTS.md", "vault-level conventions marker")
    vault.write_file("system/topics/finance/AGENTS.md", "finance-topic marker")
    _write_skill(tmp_path, "weekly-review", "asked for the weekly review.")

    prompt = Agent(vault_tools=vault, skills=_library(tmp_path))._load_system_prompt(
        topic_slug="finance"
    )

    assert prompt.index("finance-topic marker") < prompt.index("## Available skills")
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
        await agent.run(chat_id=42, user_message="First message")
        await agent.run(chat_id=42, user_message="Second message")

    # Second call's messages should include both user messages
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    user_messages = [m for m in second_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert any("First message" in c for c in contents)
    assert any("Second message" in c for c in contents)


@pytest.mark.asyncio
async def test_agent_separate_histories_per_chat(agent: Agent) -> None:
    """Different chat_ids have separate histories."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Chat 1 message")
        await agent.run(chat_id=2, user_message="Chat 2 message")
        # Send another to chat 1
        await agent.run(chat_id=1, user_message="Another chat 1")

    # Last call was to chat_id=1; its history should only have chat 1 messages
    last_call_messages = mock_client.chat.call_args_list[2][0][0]
    user_messages = [m for m in last_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert any("Chat 1 message" in c for c in contents)
    assert not any("Chat 2 message" in c for c in contents)


@pytest.mark.asyncio
async def test_agent_clear_history_forgets_previous_messages(agent: Agent) -> None:
    """After clear_history, the next run starts with a fresh context."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=42, user_message="First message")
        agent.clear_history(chat_id=42)
        await agent.run(chat_id=42, user_message="Second message")

    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    user_messages = [m for m in second_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert not any("First message" in c for c in contents)
    assert any("Second message" in c for c in contents)


@pytest.mark.asyncio
async def test_agent_clear_history_only_clears_given_thread(agent: Agent) -> None:
    """clear_history for one forum topic leaves other topics' histories intact."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Topic A message", thread_id=100)
        await agent.run(chat_id=1, user_message="Topic B message", thread_id=200)
        agent.clear_history(chat_id=1, thread_id=100)
        await agent.run(chat_id=1, user_message="Another topic A", thread_id=100)
        await agent.run(chat_id=1, user_message="Another topic B", thread_id=200)

    # Topic A was cleared: its latest run must not contain the first A message
    topic_a_messages = mock_client.chat.call_args_list[2][0][0]
    a_contents = [m["content"] for m in topic_a_messages if m.get("role") == "user"]
    assert not any("Topic A message" in c for c in a_contents)

    # Topic B was untouched: its history survives
    topic_b_messages = mock_client.chat.call_args_list[3][0][0]
    b_contents = [m["content"] for m in topic_b_messages if m.get("role") == "user"]
    assert any("Topic B message" in c for c in b_contents)


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
        reply = await agent.run(chat_id=1, user_message="Save it")

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
        reply = await agent.run(chat_id=1, user_message="Inicializa el vault")

    assert reply == "¡Claro!"
    assert any("no tool calls parsed" in r.message for r in caplog.records)
    assert any("¡Claro!" in r.message for r in caplog.records)  # raw msg included


def test_history_drops_orphaned_leading_tool_messages() -> None:
    """When eviction cuts between an assistant tool_calls message and its
    tool results, the orphaned tool messages must not be sent to the API."""
    history = ConversationHistory(max_size=3)
    history.append({"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}]})
    history.append({"role": "tool", "tool_call_id": "tc1", "content": "result"})
    history.append({"role": "user", "content": "hi"})
    history.append({"role": "assistant", "content": "hello"})  # evicts the assistant msg

    messages = history.messages()

    assert messages[0]["role"] != "tool"
    assert [m["role"] for m in messages] == ["user", "assistant"]


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
        reply = await agent.run(chat_id=1, user_message="List everything")

    assert "maximum" in reply.lower() or "iteration" in reply.lower()
    assert mock_client.chat.call_count == 20


# ------------------------------------------------------------------
# Forum topic support
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_separate_histories_per_thread(vault: VaultTools) -> None:
    """Messages in different forum topics keep separate histories."""
    agent = Agent(vault_tools=vault, history_size=10)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Topic A message", thread_id=100)
        await agent.run(chat_id=1, user_message="Topic B message", thread_id=200)
        await agent.run(chat_id=1, user_message="Another topic A", thread_id=100)

    # Last call was thread_id=100; its history should NOT contain Topic B message
    last_call_messages = mock_client.chat.call_args_list[2][0][0]
    user_messages = [m for m in last_call_messages if m.get("role") == "user"]
    contents = [m["content"] for m in user_messages]
    assert any("Topic A message" in c for c in contents)
    assert not any("Topic B message" in c for c in contents)


@pytest.mark.asyncio
async def test_agent_loads_topic_specific_prompt(vault: VaultTools) -> None:
    """When a topic slug resolves, topic AGENTS.md is appended to the system prompt."""
    # Write a topic-specific AGENTS.md
    vault.write_file("system/topics/finance/AGENTS.md", "## Finance topic instructions\n")
    # Write an index entry mapping thread_id 42 → slug 'finance'
    vault.write_file(
        "system/topics/index.md",
        "# Topic Index\n\n| topic_id | slug | name |\n|----------|------|------|\n| 42 | finance | Finance |\n",
    )

    agent = Agent(vault_tools=vault, history_size=10)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Hello", thread_id=42)

    # The system prompt passed to client.chat should include the topic-specific instructions
    first_call_messages = mock_client.chat.call_args_list[0][0][0]
    system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
    assert any("Finance topic instructions" in m["content"] for m in system_msgs)


@pytest.mark.asyncio
async def test_agent_no_topic_prompt_when_no_thread_id(vault: VaultTools) -> None:
    """Without a thread_id, only the global AGENTS.md is used."""
    vault.write_file("system/topics/finance/AGENTS.md", "## Finance topic instructions\n")
    vault.write_file(
        "system/topics/index.md",
        "# Topic Index\n\n| topic_id | slug | name |\n|----------|------|------|\n| 42 | finance | Finance |\n",
    )

    agent = Agent(vault_tools=vault, history_size=10)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("OK"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Hello")  # no thread_id

    first_call_messages = mock_client.chat.call_args_list[0][0][0]
    system_msgs = [m for m in first_call_messages if m.get("role") == "system"]
    assert not any("Finance topic instructions" in m["content"] for m in system_msgs)


@pytest.mark.asyncio
async def test_agent_register_topic_creates_index(vault: VaultTools) -> None:
    """_register_topic creates a new index.md if it doesn't exist."""
    agent = Agent(vault_tools=vault, history_size=10)
    agent._register_topic(123, "health-fitness", "Health & Fitness")
    index = vault.read_file("system/topics/index.md")
    assert "123" in index
    assert "health-fitness" in index
    assert "Health & Fitness" in index


@pytest.mark.asyncio
async def test_agent_register_topic_appends_to_existing_index(vault: VaultTools) -> None:
    """_register_topic appends a new row to an existing index.md."""
    vault.write_file(
        "system/topics/index.md",
        "# Topic Index\n\n| topic_id | slug | name |\n|----------|------|------|\n| 1 | first | First |\n",
    )
    agent = Agent(vault_tools=vault, history_size=10)
    agent._register_topic(2, "second", "Second")
    index = vault.read_file("system/topics/index.md")
    assert "| 1 | first | First |" in index
    assert "second" in index


@pytest.mark.asyncio
async def test_agent_create_forum_topic_tool(vault: VaultTools) -> None:
    """create_forum_topic tool creates vault dirs and registers topic."""
    create_fn_mock = AsyncMock(return_value={"message_thread_id": 999, "name": "Recipes"})
    agent = Agent(
        vault_tools=vault,
        create_forum_topic_fn=create_fn_mock,
        history_size=10,
    )

    responses = [
        _make_tool_call_response("create_forum_topic", {"name": "Recipes"}),
        _make_text_response("Topic 'Recipes' created!"),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="Create a recipes topic")

    create_fn_mock.assert_called_once_with("Recipes")
    assert "created" in reply.lower()

    # Vault should have system/topics/recipes/AGENTS.md
    topic_agents = vault.read_file("system/topics/recipes/AGENTS.md")
    assert "Recipes" in topic_agents

    # Index should be updated
    index = vault.read_file("system/topics/index.md")
    assert "999" in index
    assert "recipes" in index


@pytest.mark.asyncio
async def test_agent_send_message_with_thread_id(vault: VaultTools) -> None:
    """send_message tool passes message_thread_id to the send_message_fn."""
    captured: list[tuple[str, int | None]] = []

    async def mock_send(text: str, tid: int | None = None) -> None:
        captured.append((text, tid))

    agent = Agent(vault_tools=vault, send_message_fn=mock_send, history_size=10)

    responses = [
        _make_tool_call_response("send_message", {"text": "Hello topic!", "message_thread_id": 42}),
        _make_text_response("Sent."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="Send hello to topic 42")

    assert captured == [("Hello topic!", 42)]


def test_resolve_topic_slug_found(vault: VaultTools) -> None:
    vault.write_file(
        "system/topics/index.md",
        "# Topic Index\n\n| topic_id | slug | name |\n|----------|------|------|\n| 123 | health-fitness | Health & Fitness |\n",
    )
    agent = Agent(vault_tools=vault)
    slug, name = agent._resolve_topic_slug(123)
    assert slug == "health-fitness"
    assert name == "Health & Fitness"


def test_resolve_topic_slug_not_found(vault: VaultTools) -> None:
    vault.write_file(
        "system/topics/index.md",
        "# Topic Index\n\n| topic_id | slug | name |\n|----------|------|------|\n| 123 | health-fitness | Health & Fitness |\n",
    )
    agent = Agent(vault_tools=vault)
    slug, name = agent._resolve_topic_slug(999)
    assert slug is None
    assert name is None


def test_resolve_topic_slug_no_index(vault: VaultTools) -> None:
    agent = Agent(vault_tools=vault)
    slug, name = agent._resolve_topic_slug(1)
    assert slug is None
    assert name is None


# ------------------------------------------------------------------
# Web research: on_research callback
# ------------------------------------------------------------------

def _research_agent(vault: VaultTools) -> tuple[Agent, AsyncMock]:
    research_fn = AsyncMock(return_value="research summary")
    agent = Agent(vault_tools=vault, research_fn=research_fn, history_size=10)
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
        reply = await agent.run(chat_id=1, user_message="weather?", on_research=on_research)

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
        await agent.run(chat_id=1, user_message="dig deep", on_research=on_research)

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
        await agent.run(chat_id=1, user_message="list notes", on_research=on_research)

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
        reply = await agent.run(chat_id=1, user_message="weather?", on_research=on_research)

    research_fn.assert_awaited_once()
    assert reply == "Sunny."


# ------------------------------------------------------------------
# Attachment extraction tool
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_extract_attachment_tool(vault: VaultTools) -> None:
    extract_fn = AsyncMock(return_value="[PDF, 1 page(s), text layer]\n\nTotal: 99 EUR")
    agent = Agent(vault_tools=vault, extract_fn=extract_fn, history_size=10)

    responses = [
        _make_tool_call_response("extract_attachment", {"path": "attachments/a.pdf"}),
        _make_text_response("The invoice total is 99 EUR."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="what's the total on that invoice?")

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
        reply = await agent.run(chat_id=1, user_message="what plant is this?", image_data_url=_DATA_URL)

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
async def test_image_is_resent_on_every_iteration_of_the_same_run(agent: Agent) -> None:
    """During tool-call iterations the model must still see the image."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("list_files", {"glob": "*.md"}),
        _make_text_response("Filed it."),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run(chat_id=1, user_message="file this", image_data_url=_DATA_URL)

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
        await agent.run(chat_id=1, user_message="look at this", image_data_url=_DATA_URL)
        await agent.run(chat_id=1, user_message="thanks")

    # Second request: the earlier user message must be plain text again
    messages = mock_client.chat.call_args.args[0]
    earlier_user_msgs = [m for m in messages if m["role"] == "user"][:-1]
    assert earlier_user_msgs, "expected first user message in history"
    for m in earlier_user_msgs:
        assert isinstance(m["content"], str)


# ------------------------------------------------------------------
# Scheduled job runs (run_job)
# ------------------------------------------------------------------

def _job_agent(vault: VaultTools, captured: list[tuple[str, int | None]]) -> Agent:
    async def mock_send(text: str, tid: int | None = None) -> None:
        captured.append((text, tid))

    return Agent(vault_tools=vault, send_message_fn=mock_send, history_size=10)


@pytest.mark.asyncio
async def test_run_job_drops_closing_text_after_send_message(vault: VaultTools) -> None:
    """The model delivers the reminder via send_message; its closing text
    ("mensaje enviado") must not become a second message."""
    captured: list[tuple[str, int | None]] = []
    agent = _job_agent(vault, captured)

    responses = [
        _make_tool_call_response("send_message", {"text": "Reminder: call the doctor"}),
        _make_text_response("Mensaje enviado."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == [("Reminder: call the doctor", None)]


@pytest.mark.asyncio
async def test_run_job_forwards_reply_when_model_sends_nothing(vault: VaultTools) -> None:
    """Safety net: a job that never calls send_message still delivers its reply."""
    captured: list[tuple[str, int | None]] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("Reminder: call the doctor"))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == [("Reminder: call the doctor", None)]


@pytest.mark.asyncio
async def test_run_job_forwards_reply_when_send_message_fails(vault: VaultTools) -> None:
    """A failed send_message doesn't count as delivered — the safety net fires."""
    captured: list[tuple[str, int | None]] = []
    attempts = 0

    async def flaky_send(text: str, tid: int | None = None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("telegram down")
        captured.append((text, tid))

    agent = Agent(vault_tools=vault, send_message_fn=flaky_send, history_size=10)

    responses = [
        _make_tool_call_response("send_message", {"text": "Reminder: call the doctor"}),
        _make_text_response("Could not deliver."),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to call the doctor")

    assert captured == [("Could not deliver.", None)]


@pytest.mark.asyncio
async def test_run_job_stays_silent_on_silent_sentinel(vault: VaultTools) -> None:
    """A reminder that finds its purpose already met (routine logged, task
    closed) replies '[silent]' and nothing reaches the user."""
    captured: list[tuple[str, int | None]] = []
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
    captured: list[tuple[str, int | None]] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response(""))

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Log the weather silently")

    assert captured == []


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
    captured: list[tuple[str, int | None]] = []
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
    captured: list[tuple[str, int | None]] = []
    agent = _job_agent(vault, captured)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_text_response('{"silent": false, "message": "Toma la pastilla"}')
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == [("Toma la pastilla", None)]


@pytest.mark.asyncio
async def test_run_job_json_close_message_dropped_after_send_message(
    vault: VaultTools,
) -> None:
    """A run that already delivered via send_message must not repeat itself."""
    captured: list[tuple[str, int | None]] = []
    agent = _job_agent(vault, captured)

    responses = [
        _make_tool_call_response("send_message", {"text": "Toma la pastilla"}),
        _make_text_response('{"silent": false, "message": "Toma la pastilla"}'),
    ]
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=responses)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await agent.run_job("Remind the user to take the pill")

    assert captured == [("Toma la pastilla", None)]


@pytest.mark.asyncio
async def test_run_job_sentinel_anywhere_suppresses(vault: VaultTools) -> None:
    """Legacy fallback: a misplaced [silent] still means stand down (the
    2026-07-29 bug: 'Ya está registrada (08:19). Reminder resuelto.\\n\\n[silent]'
    was delivered verbatim because only the prefix was checked)."""
    captured: list[tuple[str, int | None]] = []
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
    captured: list[tuple[str, int | None]] = []
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
    captured: list[tuple[str, int | None]] = []
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
        await agent.run(chat_id=42, user_message="hi")

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
        await agent.run(chat_id=42, user_message="hi")

    mock_usage.record.assert_called_once_with(
        "agent",
        "claude-sonnet-4.6",
        {"prompt_tokens": 10, "completion_tokens": 5},
        chat_id=42,
        thread_id=None,
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
    agent = Agent(vault_tools=vault, skills=_library(tmp_path), history_size=10)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        side_effect=[
            _make_tool_call_response("load_skill", {"name": "weekly-review"}),
            _make_text_response("Done"),
        ]
    )

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="weekly review please")

    tool_result = [
        m for m in mock_client.chat.call_args.args[0] if m.get("role") == "tool"
    ][-1]
    assert "read now.md" in tool_result["content"]
    assert reply == "Done"


# ------------------------------------------------------------------
# Concurrency: per-conversation serialization
# ------------------------------------------------------------------

async def test_same_conversation_runs_are_serialized(agent: Agent) -> None:
    """Two runs for one (chat_id, thread_id) must never interleave: concurrent
    appends into one history produce tool orderings the API rejects."""
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
            agent.run(chat_id=1, user_message="first"),
            agent.run(chat_id=1, user_message="second"),
        )

    assert active["max"] == 1
    msgs = agent._get_history(1).messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert "first" in users[0]
    assert "second" in users[1]


async def test_different_conversations_run_in_parallel(agent: Agent) -> None:
    """A long run in one topic must not block a message in another topic."""
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
            agent.run(chat_id=1, user_message="topic one", thread_id=11),
            agent.run(chat_id=1, user_message="topic two", thread_id=22),
        )

    assert active["max"] == 2


async def test_scheduled_job_runs_in_parallel_with_user_chat(agent: Agent) -> None:
    """Scheduled jobs (chat_id 0) must not queue behind a user conversation."""
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
            agent.run(chat_id=1, user_message="user turn"),
            agent.run(chat_id=0, user_message="job prompt"),
        )

    assert active["max"] == 2
