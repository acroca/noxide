"""Durable shared history, reset generations, migration, and transport identity."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.agent import Agent
from assistant.conversations import WEB_CHAT_ID, ConversationArchive
from assistant.copilot import CopilotUnavailableError
from assistant.history import ConversationHistory
from assistant.retry_queue import RetryQueue
from assistant.tools import VaultTools

from .test_agent import _make_text_response, _make_tool_call_response


@pytest.fixture
def setup(tmp_path):
    archive = ConversationArchive(tmp_path)
    vault = VaultTools(tmp_path / "vault")
    agent = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
    yield archive, vault, agent
    archive.close()


async def test_both_transports_restore_five_exchanges_and_search_old_text(setup):
    archive, vault, agent = setup
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Recorded")))
    with patch("assistant.copilot.get_client", return_value=client):
        for n in range(7):
            await agent.run(123 if n % 2 else WEB_CHAT_ID, f"message-{n}", thread_id=10,
                            source="telegram" if n % 2 else "web")
        restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
        await restored.run(WEB_CHAT_ID, "followup", thread_id=10, source="web")
    sent = str(client.chat.call_args.args[0])
    assert "message-0" not in sent and "message-1" not in sent
    for n in range(2, 7):
        assert f"message-{n}" in sent
    history = restored._get_history(123, 10)
    assert "message-0" in history.retrieve("search_history", {"query": "message-0"})
    rows = archive.db.execute("SELECT * FROM messages WHERE role='user'").fetchall()
    assert len(rows) == 8
    assert {r["space"] for r in rows} == {"topic:10"}
    assert {r["source"] for r in rows} == {"telegram", "web"}
    assert restored._get_history(123, 10) is restored._get_history(WEB_CHAT_ID, 10)
    assert restored._get_history(999, 10) is not history


async def test_shared_lock_serializes_web_and_telegram_and_reset(setup):
    archive, _, agent = setup
    started, release = asyncio.Event(), asyncio.Event()

    async def reply(messages, *args, **kwargs):
        if "first" in messages[-1]["content"]:
            started.set()
            await release.wait()
        return _make_text_response("finished")

    client = MagicMock(chat=AsyncMock(side_effect=reply))
    with patch("assistant.copilot.get_client", return_value=client):
        first = asyncio.create_task(agent.run(123, "first"))
        await started.wait()
        second = asyncio.create_task(agent.run(WEB_CHAT_ID, "second", source="web"))
        await asyncio.sleep(0)
        assert client.chat.await_count == 1
        reset = asyncio.create_task(agent.reset_conversation(WEB_CHAT_ID))
        await asyncio.sleep(0)
        assert not reset.done()
        release.set()
        await asyncio.gather(first, second, reset)
    assert "first" in str(client.chat.call_args.args[0])
    assert agent._get_history(123).messages() == []
    assert "first" in agent._get_history(123).retrieve("search_history", {"query": "first"})
    assert archive.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4


async def test_reset_dismisses_pending_work_but_keeps_archived_text(setup):
    archive, vault, agent = setup
    mid = archive.insert("general", "user", "secret-pending", "queued", source="telegram")
    client = MagicMock(chat=AsyncMock(side_effect=CopilotUnavailableError("offline")))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run(123, "secret-pending", message_id=mid)
        await agent.reset_conversation(WEB_CHAT_ID)
        restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
        assert await restored.retry_message(123, None, "secret-pending", "earlier", hot=False, message_id=mid) is None
    row = archive.get(mid)
    assert row["status"] == "dismissed" and row["text"] == "secret-pending"


async def test_outage_retry_commits_one_reply_and_completed_context(setup):
    archive, vault, agent = setup
    vault.write_file("large.md", "x" * 6000)
    mid = archive.insert("general", "user", "read it", "queued", source="web")
    client = MagicMock(chat=AsyncMock(side_effect=[
        _make_tool_call_response("read_file", {"path": "large.md"}),
        CopilotUnavailableError("offline"), _make_text_response("Done"),
    ]))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run(WEB_CHAT_ID, "read it", message_id=mid, source="web")
        assert await agent.retry_message(123, None, "read it", "earlier", hot=True, message_id=mid) == "Done"
        assert await agent.retry_message(123, None, "read it", "earlier", hot=True, message_id=mid) is None
    assert "x" * 6000 in str(client.chat.call_args.args[0])
    assert archive.get(mid)["status"] == "done"
    assert archive.reply(mid) == "Done"
    assert archive.db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0] == 1
    restored = ConversationHistory(archive=archive, space="general")
    assert "x" * 6000 not in str(restored.messages())
    assert "read it" in str(restored.messages())


def test_atomic_completion_rolls_back_context_and_note_consumption(setup):
    archive, _, _ = setup
    mid = archive.insert("general", "user", "question", "queued")
    archive.queue_note("general", "note")
    note = archive.notes("general")[0]
    archive.db.execute("""CREATE TRIGGER reject_reply BEFORE INSERT ON messages
                        WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'test'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        archive.save_context("general", [{"role": "user", "content": "question"},
                                         {"role": "assistant", "content": "answer"}], "now",
                             message_id=mid, request_ids={mid}, note_ids={note["id"]})
    assert archive.load_context("general") == []
    assert archive.get(mid)["status"] == "queued"
    assert len(archive.notes("general")) == 1


async def test_notes_survive_restart_without_double_injection(setup):
    archive, vault, agent = setup
    agent._queue_sent_note(123, None, "Medicine reminder")
    restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Done")))
    with patch("assistant.copilot.get_client", return_value=client):
        await restored.run(WEB_CHAT_ID, "taken", source="web")
    assert str(client.chat.call_args.args[0]).count("Medicine reminder") == 1
    assert archive.notes("general") == []
    assert "Medicine reminder" in str(Agent(vault, archive=archive)._get_history(WEB_CHAT_ID).messages())


def test_legacy_web_context_migration_is_once_and_uses_same_space(tmp_path):
    db = sqlite3.connect(tmp_path / "companion.sqlite3")
    db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY, space TEXT, role TEXT, text TEXT,
        status TEXT, created REAL, reply_to TEXT, error TEXT DEFAULT '');
        INSERT INTO messages VALUES ('old','wiki/projects/garden.md','user','Watered','done',1,NULL,'');
        INSERT INTO messages VALUES ('answer','wiki/projects/garden.md','assistant','Noted','done',2,'old','');""")
    db.close()
    archive = ConversationArchive(tmp_path)
    assert len(archive.load_context("wiki/projects/garden.md")) == 2
    agent = Agent(VaultTools(tmp_path / "vault"), archive=archive)
    agent.register_legacy_space(321, "wiki/projects/garden.md")
    assert "Watered" in str(agent._get_history(WEB_CHAT_ID, 321).messages())
    archive.close()
    archive = ConversationArchive(tmp_path)
    assert len(archive.load_context("wiki/projects/garden.md")) == 2
    archive.close()


def test_pre_upgrade_retry_entries_get_durable_identity(setup, tmp_path):
    archive, _, agent = setup
    (tmp_path / "pending_runs.jsonl").write_text(json.dumps({"kind": "message", "text": "old pending",
        "queued_at": "earlier", "chat_id": 123, "thread_id": None}) + "\n")
    queue = RetryQueue(tmp_path, AsyncMock(), AsyncMock())
    queue.attach_archive(archive, agent.conversation_space)
    item = queue._items[0]
    assert item.message_id
    assert archive.get(item.message_id)["space"] == "general"
    archive.reset("general")
    assert archive.get(item.message_id)["status"] == "dismissed"
    assert json.loads((tmp_path / "pending_runs.jsonl").read_text())["message_id"] == item.message_id


async def test_telegram_batch_is_archived_and_redelivery_cannot_regroup_completed_updates(setup):
    from assistant.telegram_bot import TelegramBot, _BatchItem, _PendingBatch

    archive, _, agent = setup
    bot = TelegramBot("token", [1], agent, archive=archive)
    def message(mid):
        return SimpleNamespace(message_id=mid, date="2026-09-15", set_reaction=AsyncMock(),
                               reply_text=AsyncMock(return_value=SimpleNamespace(message_id=mid + 100)))
    one, two, three = message(1), message(2), message(3)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Received")))
    with patch("assistant.copilot.get_client", return_value=client):
        await bot._answer_batch((123, 10), _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
            items=[_BatchItem(one, "A voice transcript"), _BatchItem(two, "attachments/photo.jpg")]))
        await bot._answer_batch((123, 10), _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
            items=[_BatchItem(two, "attachments/photo.jpg"), _BatchItem(three, "New input")]))
    rows = archive.db.execute("SELECT * FROM messages WHERE role='user' ORDER BY created").fetchall()
    assert len(rows) == 2
    assert rows[0]["source"] == "telegram" and rows[0]["space"] == "topic:10"
    assert json.loads(rows[0]["metadata"])["message_ids"] == [1, 2]
    assert "attachments/photo.jpg" in rows[0]["text"]
    assert rows[1]["text"] == "New input"
    assert archive.get("reply:" + rows[0]["id"])["delivery"] == "delivered"
    assert client.chat.await_count == 2


async def test_telegram_reply_delivery_failure_keeps_generated_reply_visible(setup):
    from assistant.telegram_bot import TelegramBot, _BatchItem, _PendingBatch

    archive, _, agent = setup
    bot = TelegramBot("token", [1], agent, archive=archive)
    msg = SimpleNamespace(message_id=1, date="today", set_reaction=AsyncMock(),
                          reply_text=AsyncMock(side_effect=RuntimeError("Telegram offline")))
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Written successfully")))
    with patch("assistant.copilot.get_client", return_value=client), pytest.raises(RuntimeError):
        await bot._answer_batch((123, None), _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
                                                        items=[_BatchItem(msg, "write a note")]))
    assert archive.get("telegram:123:1")["status"] == "done"
    row = archive.get("reply:telegram:123:1")
    assert row["text"] == "Written successfully" and row["delivery"] == "failed"


async def test_database_reopen_restores_only_completed_text(tmp_path):
    vault = VaultTools(tmp_path / "vault")
    archive = ConversationArchive(tmp_path)
    agent = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Remembered answer")))
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run(123, "Remembered question")
    archive.close()
    archive = ConversationArchive(tmp_path)
    try:
        restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
        with patch("assistant.copilot.get_client", return_value=client):
            await restored.run(WEB_CHAT_ID, "Follow up", source="web")
        sent = str(client.chat.call_args.args[0])
        assert "Remembered question" in sent and "Remembered answer" in sent
        assert len(archive.load_context("general")) == 4
    finally:
        archive.close()
