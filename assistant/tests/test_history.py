"""Recent text context, exact older retrieval, and scoped agent tool plumbing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.agent import Agent
from assistant.copilot import CopilotUnavailableError
from assistant.history import ConversationHistory
from assistant.responses import OUTPUT_KEY
from assistant.tools import VaultTools

from .test_agent import _make_text_response, _make_tool_call_response


def complete(history, text, reply="noted"):
    history.begin_run()
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    history.finish_run(timestamp="2026-09-08 12:00 local")


def test_five_complete_exchanges_and_retrievable_older_text():
    history = ConversationHistory()
    for n in range(7):
        complete(history, f"request {n}")
    assert [m["content"] for m in history.messages() if m["role"] == "user"] == [
        f"request {n}" for n in range(2, 7)
    ]
    assert "2 older exchanges omitted" in history.coverage()
    assert "before_id=5" in history.coverage()
    first_page = json.loads(history.retrieve("get_history", {"before_id": 5, "limit": 2}))
    assert [m["id"] for m in first_page["messages"]] == [3, 4]
    assert first_page["next_before_id"] == 3
    second_page = json.loads(history.retrieve("get_history", {"before_id": 3}))
    assert [m["id"] for m in second_page["messages"]] == [1, 2]
    assert second_page["next_before_id"] is None
    assert all(m["completed_at"] == "2026-09-08 12:00 local" for m in second_page["messages"])


def test_search_is_literal_case_insensitive_and_paginated():
    history = ConversationHistory()
    for n in range(4):
        complete(history, f"Budget [EUR] {n}")
    page = json.loads(history.retrieve("search_history", {"query": "[eur]", "limit": 2}))
    assert [m["id"] for m in page["messages"]] == [5, 7]
    assert page["next_before_id"] == 5
    page = json.loads(history.retrieve("search_history", {"query": "[eur]", "before_id": 5}))
    assert [m["id"] for m in page["messages"]] == [1, 3]
    assert page["next_before_id"] is None
    assert json.loads(history.retrieve("search_history", {"query": ".*"}))["messages"] == []


def test_long_text_is_fully_recoverable_and_search_finds_omitted_tail():
    history = ConversationHistory()
    text = "a" * 35000 + " old constraint " + "b" * 15000
    complete(history, text)
    assert len(str(history.messages())) < 7000
    assert "get_history message_id=1 offset=3000" in history.messages()[0]["content"]
    search = json.loads(history.retrieve("search_history", {"query": "old constraint"}))
    assert "old constraint" in search["messages"][0]["content"]
    assert search["messages"][0]["truncated"]
    offset = 0
    chunks = []
    while offset is not None:
        page = json.loads(history.retrieve("get_history", {"message_id": 1, "offset": offset}))
        assert len(page["content"]) <= 12000
        chunks.append(page["content"])
        offset = page["next_offset"]
    assert "".join(chunks) == text


@pytest.mark.parametrize("name,args", [
    ("get_history", {"before_id": True}),
    ("get_history", {"message_id": 0}),
    ("get_history", {"limit": 21}),
    ("get_history", {"limit": "5"}),
    ("get_history", {"before_id": None}),
    ("get_history", {"offset": 10}),
    ("get_history", {"message_id": 1, "offset": -1}),
    ("get_history", {"message_id": 1, "before_id": 2}),
    ("get_history", {"chat_id": 2}),
    ("search_history", {}),
    ("search_history", {"query": " "}),
    ("search_history", {"query": 12}),
    ("search_history", {"query": "a" * 401}),
])
def test_invalid_arguments(name, args):
    with pytest.raises(ValueError):
        ConversationHistory().retrieve(name, args)


def test_empty_history_and_missing_message():
    history = ConversationHistory()
    assert json.loads(history.retrieve("get_history", {}))["messages"] == []
    assert "not found" in history.retrieve("get_history", {"message_id": 1})
    assert history.coverage() is None


def test_list_pages_respect_total_content_budget_without_skipping_messages():
    history = ConversationHistory()
    for n in range(10):
        complete(history, str(n) * 9000)
    before = None
    ids = []
    while True:
        args = {"limit": 20}
        if before is not None:
            args["before_id"] = before
        page = json.loads(history.retrieve("get_history", args))
        assert sum(len(m["content"]) for m in page["messages"]) <= 12000
        ids.extend(m["id"] for m in page["messages"])
        before = page["next_before_id"]
        if before is None:
            break
    assert sorted(ids) == list(range(1, 21))


def test_search_offsets_reference_original_unicode_text():
    history = ConversationHistory()
    text = "\u0130" * 3000 + "needle"
    complete(history, text)
    page = json.loads(history.retrieve("search_history", {"query": "needle"}))
    record = page["messages"][0]
    assert "needle" in record["content"]
    assert text[record["offset"]:].startswith(record["content"])


def test_only_text_is_archived_and_tool_heavy_work_is_one_exchange():
    history = ConversationHistory()
    history.begin_run()
    history.append({"role": "assistant", "content": "[sent from a scheduled run] medication"})
    history.append({"role": "user", "content": "taken"})
    for n in range(40):
        history.append({"role": "assistant", "content": "internal preamble",
                        "tool_calls": [{"id": str(n), "function": {"arguments": "secret"}}],
                        "reasoning_opaque": "secret", OUTPUT_KEY: [{"secret": "raw output"}]})
        history.append({"role": "tool", "tool_call_id": str(n), "content": "tool secret"})
    history.append({"role": "user", "content": "resume after outage"})
    history.append({"role": "assistant", "content": "recorded", "reasoning_text": "secret"})
    history.finish_run()
    for n in range(4):
        complete(history, f"next {n}")
    assert "medication" in str(history.messages())
    assert "resume after outage" in str(history.messages())
    assert "secret" not in str(history.messages())
    assert "secret" not in history.retrieve("get_history", {"limit": 20})
    assert "internal preamble" not in history.retrieve("get_history", {"limit": 20})
    complete(history, "sixth")
    assert "medication" not in str(history.messages())
    assert "medication" in history.retrieve("search_history", {"query": "medication"})


@pytest.mark.parametrize("chat_id,thread_id", [(1, None), (1, 100), (1, 200), (2, 100), (0, None)])
async def test_real_loop_scopes_history_tools_and_clear(tmp_path, chat_id, thread_id):
    agent = Agent(VaultTools(tmp_path))
    keys = [(1, None), (1, 100), (1, 200), (2, 100), (0, None)]
    for key in keys:
        complete(agent._get_history(*key), f"unique {key}")
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("search_history", {"query": "unique"}),
        _make_tool_call_response("get_history", {"message_id": 1}),
        _make_text_response("found"),
    ])
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run(chat_id, "look back", thread_id=thread_id)
    tools = client.chat.call_args.args[1]
    assert {"get_history", "search_history"} <= {t["function"]["name"] for t in tools}
    outputs = [m["content"] for m in client.chat.call_args.args[0] if m["role"] == "tool"]
    assert len(outputs) == 2
    for output in outputs:
        assert f"unique {(chat_id, thread_id)}" in output
        for key in keys:
            if key != (chat_id, thread_id):
                assert f"unique {key}" not in output
    agent.clear_history(chat_id, thread_id)
    assert json.loads(agent._get_history(chat_id, thread_id).retrieve("get_history", {}))["messages"] == []
    assert "no conversation history" in await agent._dispatch_tool("get_history", {})


async def test_older_context_window_is_frozen_while_outage_work_survives(tmp_path):
    agent = Agent(VaultTools(tmp_path))
    history = agent._get_history(1)
    for n in range(7):
        complete(history, f"completed {n}")
    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("get_history", {"before_id": 5}),
        CopilotUnavailableError("offline"),
        CopilotUnavailableError("still offline"),
        _make_text_response("recovered"),
    ])
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run(1, "original task")
        first = client.chat.call_args_list[0].args[0]
        second = client.chat.call_args_list[1].args[0]
        assert first == second[:len(first)]
        assert "older exchanges omitted" in first[-1]["content"]
        assert "completed 0" not in str(first)
        before = history.messages()
        with pytest.raises(CopilotUnavailableError):
            await agent.retry_message(1, None, "original task", "earlier", hot=True)
        assert history.messages() == before
        await agent.retry_message(1, None, "original task", "earlier", hot=True)
    sent = client.chat.call_args.args[0]
    assert "original task" in str(sent)
    assert any(m["role"] == "tool" and "completed 0" in m["content"] for m in sent)
    assert not any(m["role"] == "tool" for m in history.messages())


async def test_empty_success_reply_still_supersedes_hot_retry(tmp_path):
    agent = Agent(VaultTools(tmp_path))
    client = MagicMock()
    client.chat = AsyncMock(return_value=_make_text_response(""))
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run(1, "question")
        assert await agent.retry_message(1, None, "question", "earlier", hot=True) is None


async def test_pending_reminder_notes_are_not_limited_by_automatic_window(tmp_path):
    agent = Agent(VaultTools(tmp_path))
    for n in range(12):
        agent._queue_sent_note(1, None, f"reminder {n}")
    client = MagicMock()
    client.chat = AsyncMock(return_value=_make_text_response("noted"))
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run(1, "tell me more")
    sent = client.chat.call_args.args[0]
    for n in range(12):
        assert any(f"reminder {n}" in m["content"] for m in sent)
    archived = json.loads(agent._get_history(1).retrieve("get_history", {"limit": 20}))
    assert len(archived["messages"]) == 14
