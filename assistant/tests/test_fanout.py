"""Tests for fan-out: concurrent read-only worker sub-agents over item batches."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.agent import Agent
from assistant.fanout import FanOut
from assistant.skills import SkillLibrary
from assistant.tools import VaultTools


def _make_text_response(content: str) -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _make_tool_call_response(tool_name: str, tool_args: dict, call_id: str = "tc1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }


@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    return VaultTools(tmp_path / "vault")


@pytest.fixture
def skills(tmp_path: Path) -> SkillLibrary:
    repo_dir = tmp_path / "repo-skills"
    repo_dir.mkdir()
    (repo_dir / "grade-item.md").write_text(
        "# Grade an item\n\n**Use when:** grading one catalogue item.\n\nSteps here.\n",
        encoding="utf-8",
    )
    return SkillLibrary(tmp_path / "vault", repo_skills_dir=repo_dir)


# ------------------------------------------------------------------
# Input validation (no model calls)
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("instruction", "items"),
    [
        ("", ["a"]),
        ("   ", ["a"]),
        ("x" * 2001, ["a"]),
        ("do it", []),
        ("do it", ["a"] * 51),
        ("do it", ["y" * 1001]),
    ],
)
async def test_run_rejects_invalid_input(
    vault: VaultTools, instruction: str, items: list[str]
) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock()
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run(instruction, items)

    assert out.startswith("[tool error:")
    mock_client.chat.assert_not_called()


# ------------------------------------------------------------------
# Aggregation
# ------------------------------------------------------------------

async def test_run_returns_one_block_per_item_in_order(vault: VaultTools) -> None:
    async def chat(messages, tools, initiator=None):
        item = messages[1]["content"].rsplit("\n", 1)[-1]
        return _make_text_response(f"graded {item}")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("grade each item", ["alpha", "beta", "gamma"])

    assert "fan_out processed 3 items (0 failed):" in out
    assert out.index("### Item 1: alpha") < out.index("### Item 2: beta") < out.index(
        "### Item 3: gamma"
    )
    assert "graded alpha" in out
    assert "graded beta" in out
    assert "graded gamma" in out


async def test_one_failing_worker_does_not_sink_the_batch(vault: VaultTools) -> None:
    async def chat(messages, tools, initiator=None):
        if "bad" in messages[1]["content"]:
            raise RuntimeError("copilot down")
        return _make_text_response("ok")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("grade each item", ["good", "bad", "fine"])

    assert "fan_out processed 3 items (1 failed):" in out
    assert "[item error: copilot down]" in out
    assert out.count("ok") == 2


async def test_worker_results_are_truncated(vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("y" * 50_000))
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("summarize", ["one"])

    assert len(out) < 3_000
    assert "[truncated]" in out


async def test_worker_iteration_cap(vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_tool_call_response("search", {"pattern": "x"})
    )
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("loop forever", ["one"])

    assert "[item error: worker hit the iteration limit" in out
    assert mock_client.chat.call_count == 10


# ------------------------------------------------------------------
# Concurrency
# ------------------------------------------------------------------

async def test_workers_run_concurrently_capped_by_semaphore(vault: VaultTools) -> None:
    inflight = {"now": 0, "max": 0}

    async def chat(messages, tools, initiator=None):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.02)
        inflight["now"] -= 1
        return _make_text_response("done")

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=chat)
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("do it", [f"item-{i}" for i in range(8)])

    assert inflight["max"] == 4


# ------------------------------------------------------------------
# Worker quarantine: context and tools
# ------------------------------------------------------------------

async def test_worker_context_is_instruction_and_item_only(vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("grade the item", ["alpha"])

    messages = mock_client.chat.call_args[0][0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "fan-out worker" in messages[0]["content"]
    assert "grade the item" in messages[1]["content"]
    assert "alpha" in messages[1]["content"]


async def test_worker_tools_are_readonly_subset(
    vault: VaultTools, skills: SkillLibrary
) -> None:
    async def fake_research(question: str) -> str:
        return "found"

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    fan_out = FanOut(vault, skills=skills, research_fn=fake_research)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("do it", ["one"])

    tools = mock_client.chat.call_args[0][1]
    names = {t["function"]["name"] for t in tools}
    assert names == {"read_file", "list_files", "search", "load_skill", "research"}


async def test_worker_tools_without_skills_and_research(vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("do it", ["one"])

    tools = mock_client.chat.call_args[0][1]
    names = {t["function"]["name"] for t in tools}
    assert names == {"read_file", "list_files", "search"}


async def test_worker_prompt_includes_skills_menu(
    vault: VaultTools, skills: SkillLibrary
) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    fan_out = FanOut(vault, skills=skills)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("do it", ["one"])

    system = mock_client.chat.call_args[0][0][0]["content"]
    assert "grade-item" in system


async def test_worker_write_tools_are_refused(vault: VaultTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("create_file", {"path": "x.md", "content": "boo"}),
        _make_text_response("gave up"),
    ])
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("do it", ["one"])

    assert "gave up" in out
    assert vault.read_file("x.md").startswith("[file not found")
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("[unknown tool: create_file]" in m["content"] for m in tool_msgs)


async def test_worker_searches_vault_then_answers(vault: VaultTools) -> None:
    vault.write_file("wiki/books.md", "- Dune: unread\n")
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("search", {"pattern": "Dune"}),
        _make_text_response("Dune is unread"),
    ])
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await fan_out.run("check reading status", ["Dune"])

    assert "Dune is unread" in out
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("wiki/books.md" in m["content"] for m in tool_msgs)


async def test_worker_requests_are_agent_initiated(vault: VaultTools) -> None:
    """Fan-out runs inside an existing user turn — its calls must not be
    billed as user-initiated premium requests."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await fan_out.run("do it", ["one"])

    assert mock_client.chat.call_args.kwargs["initiator"] == "agent"


# ------------------------------------------------------------------
# Usage recording
# ------------------------------------------------------------------

async def test_fan_out_records_usage_events(vault: VaultTools) -> None:
    mock_client = MagicMock()
    response = _make_text_response("done")
    response["model"] = "claude-sonnet-4.6"
    mock_client.chat = AsyncMock(return_value=response)
    fan_out = FanOut(vault)

    with patch("assistant.copilot.get_client", return_value=mock_client), \
         patch("assistant.fanout.usage") as mock_usage:
        await fan_out.run("do it", ["one", "two"])

    assert mock_usage.record.call_count == 2
    mock_usage.record.assert_called_with(
        "fanout", "claude-sonnet-4.6", {"prompt_tokens": 10, "completion_tokens": 5}
    )


# ------------------------------------------------------------------
# Agent integration: the `fan_out` tool
# ------------------------------------------------------------------

def test_agent_registers_fan_out_tool_when_fn_present(vault: VaultTools) -> None:
    async def fake_fan_out(instruction: str, items: list[str]) -> str:
        return "ok"

    agent = Agent(vault_tools=vault, fan_out_fn=fake_fan_out)

    names = {t["function"]["name"] for t in agent._all_tools()}
    assert "fan_out" in names
    assert "Fan-out" in agent._base_prompt()


def test_agent_omits_fan_out_tool_without_fn(vault: VaultTools) -> None:
    agent = Agent(vault_tools=vault)

    names = {t["function"]["name"] for t in agent._all_tools()}
    assert "fan_out" not in names
    assert "Fan-out" not in agent._base_prompt()


async def test_agent_dispatches_fan_out_tool(vault: VaultTools) -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_fan_out(instruction: str, items: list[str]) -> str:
        calls.append((instruction, items))
        return "### Item 1: a\nfine"

    agent = Agent(vault_tools=vault, fan_out_fn=fake_fan_out, history_size=10)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("fan_out", {"instruction": "grade", "items": ["a"]}),
        _make_text_response("All graded."),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="grade my list")

    assert calls == [("grade", ["a"])]
    assert reply == "All graded."
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("### Item 1: a" in m["content"] for m in tool_msgs)
