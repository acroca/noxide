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
            await agent.run(123 if n % 2 else WEB_CHAT_ID, f"message-{n}",
                            source="telegram" if n % 2 else "web")
        restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
        await restored.run(WEB_CHAT_ID, "followup", source="web")
    sent = str(client.chat.call_args.args[0])
    assert "message-0" not in sent and "message-1" not in sent
    for n in range(2, 7):
        assert f"message-{n}" in sent
    history = restored._get_history(123)
    assert "message-0" in history.retrieve("search_history", {"query": "message-0"})
    rows = archive.db.execute("SELECT * FROM messages WHERE role='user'").fetchall()
    assert len(rows) == 8
    assert {r["space"] for r in rows} == {"general"}
    assert {r["source"] for r in rows} == {"telegram", "web"}
    assert restored._get_history(123) is restored._get_history(WEB_CHAT_ID)
    assert restored._get_history(999) is not history


async def test_replies_serialize_roots_run_in_parallel_and_reset_does_not_wait(setup):
    archive, _, agent = setup
    started, release = asyncio.Event(), asyncio.Event()
    calls = {}

    async def reply(messages, *args, **kwargs):
        text = messages[-1]["content"].split("\n")[0]
        calls[text.split("] ", 1)[1]] = messages
        if "first" in text:
            started.set()
            await release.wait()
        return _make_text_response("finished")

    client = MagicMock(chat=AsyncMock(side_effect=reply))
    root = archive.insert("general", "user", "first", "queued", source="telegram")
    with patch("assistant.copilot.get_client", return_value=client):
        first = asyncio.create_task(agent.run(123, "first", message_id=root))
        await started.wait()
        second = asyncio.create_task(agent.run(WEB_CHAT_ID, "second", source="web", reply_to=root))
        third = asyncio.create_task(agent.run(WEB_CHAT_ID, "third", source="web"))
        for _ in range(5):
            await asyncio.sleep(0)
        # The reply waits for its thread; the new root does not.
        assert third.done() and "second" not in calls
        assert [m["role"] for m in calls["third"]] == ["system", "user"]
        # A reset returns at once instead of waiting for the in-flight thread...
        reset = asyncio.create_task(agent.reset_conversation(WEB_CHAT_ID))
        await asyncio.sleep(0)
        assert reset.done() and not first.done()
        release.set()
        # ...so the archive refuses to publish the old generation's run, and the
        # queued reply finds its row dismissed.
        with pytest.raises(ValueError, match="Conversation changed"):
            await first
        assert await second == ""
        await reset
    assert "second" not in calls
    assert agent._get_history(123).messages() == []
    assert "third" in agent._get_history(123).retrieve("search_history", {"query": "third"})
    assert "first" not in str(archive.load_context("general"))
    rows = {r["text"]: r for r in archive.db.execute("SELECT * FROM messages")}
    assert set(rows) == {"first", "second", "third", "finished"}
    assert rows["second"]["status"] == "dismissed" and rows["second"]["thread"] == root
    # The in-flight run's failure must not revive the tombstoned row as retryable work.
    assert rows["first"]["status"] == "dismissed" and rows["first"]["error"] == ""
    assert rows["third"]["status"] == "done" and rows["finished"]["thread"] == rows["third"]["id"]
    assert archive.generation("general") == 1


async def test_reset_dismisses_pending_work_but_keeps_archived_text(setup):
    archive, vault, agent = setup
    mid = archive.insert("general", "user", "secret-pending", "queued", source="telegram")
    client = MagicMock(chat=AsyncMock(side_effect=CopilotUnavailableError("offline")))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run(123, "secret-pending", message_id=mid)
        await agent.reset_conversation(WEB_CHAT_ID)
        restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
        assert await restored.retry_message(123, "secret-pending", "earlier", hot=False, message_id=mid) is None
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
        assert await agent.retry_message(123, "read it", "earlier", hot=True, message_id=mid) == "Done"
        assert await agent.retry_message(123, "read it", "earlier", hot=True, message_id=mid) is None
    assert "x" * 6000 in str(client.chat.call_args.args[0])
    assert archive.get(mid)["status"] == "done"
    assert archive.reply(mid) == "Done"
    assert archive.db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0] == 1
    restored = ConversationHistory(archive=archive, space="general")
    assert "x" * 6000 not in str(restored.messages())
    assert "read it" in str(restored.messages())


def test_atomic_completion_rolls_back_context_and_request_status(setup):
    archive, _, _ = setup
    mid = archive.insert("general", "user", "question", "queued")
    archive.db.execute("""CREATE TRIGGER reject_reply BEFORE INSERT ON messages
                        WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'test'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        archive.save_context("general", [{"role": "user", "content": "question"},
                                         {"role": "assistant", "content": "answer"}], "now",
                             thread=mid, message_id=mid, request_ids={mid})
    assert archive.load_context("general") == []
    assert archive.get(mid)["status"] == "queued"
    assert archive.reply(mid) is None


def test_completion_stamps_the_thread_on_records_and_reply(setup):
    archive, _, _ = setup
    root = archive.insert("general", "user", "question", "done", source="web")
    mid = archive.insert("general", "user", "follow-up", "queued", source="web", reply_to=root)
    exchange = [{"role": "user", "content": "follow-up"}, {"role": "assistant", "content": "answer"}]
    archive.save_context("general", exchange, "now", thread=root, message_id=mid, request_ids={mid})
    assert [r["thread"] for r in archive.load_context("general")] == [root, root]
    assert archive.load_context("general", root) == archive.load_context("general")
    assert archive.load_context("general", mid) == []
    reply = archive.get(f"reply:{mid}")
    assert reply["thread"] == root and reply["reply_to"] == mid and reply["delivery"] == "available"
    assert archive.get(mid)["status"] == "done"
    assert [r["id"] for r in exchange] == [1, 2]


async def test_delivery_root_survives_restart_and_is_shown_once_as_background(setup):
    archive, vault, agent = setup
    archive.insert("general", "assistant", "Medicine reminder", "done", source="telegram", delivery="delivered")
    restored = Agent(vault, archive=archive, home_chat_fn=lambda: 123)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Done")))
    with patch("assistant.copilot.get_client", return_value=client):
        await restored.run(WEB_CHAT_ID, "taken", source="web")
    sent = client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[-1]["content"].count("Medicine reminder") == 1
    assert "Assistant (sent from a scheduled run): Medicine reminder" in sent[-1]["content"]
    assert "Medicine reminder" not in str(Agent(vault, archive=archive)._get_history(WEB_CHAT_ID).messages())
    assert "Medicine reminder" not in str(archive.load_context("general"))
    assert archive.db.execute("SELECT count(*) FROM pending_notes").fetchone()[0] == 0


def test_backfill_gives_pre_thread_rows_a_thread_once(tmp_path):
    archive = ConversationArchive(tmp_path)
    root = archive.insert("general", "user", "question", "done", source="web")
    answer = archive.insert("general", "assistant", "answer", "done", message_id=f"reply:{root}", reply_to=root)
    deeper = archive.insert("general", "user", "and?", "done", source="web", reply_to=answer)
    alone = archive.insert("general", "assistant", "reminder", "done", source="telegram")
    orphan = archive.insert("general", "assistant", "orphan", "done", reply_to="gone")
    archive.db.execute("UPDATE messages SET thread=''")
    archive.db.commit()
    archive.close()
    archive = ConversationArchive(tmp_path)
    try:
        assert {archive.thread_of(i) for i in (root, answer, deeper)} == {root}
        assert archive.thread_of(alone) == alone and archive.thread_of(orphan) == orphan
        assert archive.thread_of("gone") is None
        assert archive.db.execute("SELECT count(*) FROM messages WHERE thread=''").fetchone()[0] == 0
    finally:
        archive.close()


def test_insert_joins_the_parent_thread_or_starts_one(setup):
    archive, _, _ = setup
    root = archive.insert("general", "user", "root", "done", source="telegram")
    reply = archive.insert("general", "assistant", "reply", "done", reply_to=root)
    deeper = archive.insert("general", "user", "deeper", "queued", reply_to=reply)
    fresh = archive.insert("general", "user", "fresh", "queued")
    dangling = archive.insert("general", "user", "dangling", "queued", reply_to="missing")
    assert archive.get(root)["thread"] == root and archive.get(root)["reply_to"] is None
    assert archive.get(reply)["thread"] == root and archive.get(reply)["reply_to"] == root
    assert archive.get(deeper)["thread"] == root and archive.get(deeper)["reply_to"] == reply
    assert archive.get(fresh)["thread"] == fresh
    assert archive.get(dangling)["thread"] == dangling and archive.get(dangling)["reply_to"] == "missing"
    # An idempotent resend keeps the first row (and its thread) untouched.
    assert archive.insert("general", "user", "deeper", "queued", message_id=deeper) == deeper
    assert archive.get(deeper)["thread"] == root
    with pytest.raises(ValueError):
        archive.insert("general", "user", "other text", "queued", message_id=deeper)


def test_recent_threads_orders_by_last_activity_and_filters(setup):
    archive, _, _ = setup

    def at(message_id, created):
        archive.db.execute("UPDATE messages SET created=? WHERE id=?", (created, message_id))
        archive.db.commit()

    old = archive.insert("general", "user", "old era", "done", source="web")
    archive.reset("general")
    at(old, 100)
    a = archive.insert("general", "user", "a root", "done", source="web")
    at(a, 10)
    a_reply = archive.insert("general", "assistant", "a first reply", "done", reply_to=a)
    at(a_reply, 11)
    a_last = archive.insert("general", "assistant", "a latest reply", "done", reply_to=a)
    at(a_last, 50)
    b = archive.insert("general", "assistant", "b delivery", "done", source="telegram")
    at(b, 20)
    b_pending = archive.insert("general", "user", "b pending", "running", reply_to=b)
    at(b_pending, 60)
    c = archive.insert("general", "user", "c root", "done", source="telegram")
    at(c, 30)
    stale = archive.insert("general", "user", "too old", "done", source="web")
    at(stale, 1)
    queued = archive.insert("general", "user", "not done", "queued", source="web")
    at(queued, 70)
    elsewhere = archive.insert("telegram:9", "user", "other chat", "done", source="telegram")
    at(elsewhere, 80)

    def recent(**kwargs):
        return archive.recent_threads("general", **{"generation": 1, "since": 5, "limit": 5, **kwargs})

    threads = recent()
    assert [t["thread"] for t in threads] == [a, c, b]
    assert threads[0] == {"thread": a, "root_role": "user", "root_text": "a root", "root_created": 10,
                          "reply_text": "a latest reply"}
    assert threads[1] == {"thread": c, "root_role": "user", "root_text": "c root", "root_created": 30,
                          "reply_text": None}
    assert threads[2] == {"thread": b, "root_role": "assistant", "root_text": "b delivery",
                          "root_created": 20, "reply_text": None}
    assert [t["thread"] for t in recent(exclude=a)] == [c, b]
    assert [t["thread"] for t in recent(limit=2)] == [a, c]
    assert [t["thread"] for t in recent(limit=2, exclude=a)] == [c, b]
    assert [t["thread"] for t in recent(since=25)] == [a, c]
    assert [t["thread"] for t in recent(generation=0)] == [old]
    assert recent(generation=2) == []
    archive.status(a, "dismissed")
    assert [t["thread"] for t in recent()] == [c, b]


def test_telegram_outputs_resolve_to_their_archived_row(setup):
    archive, _, _ = setup
    archive.db.execute("INSERT INTO telegram_inputs VALUES (123, 1, 'telegram:123:1')")
    archive.db.commit()
    archive.record_outputs(123, [10, 11], "reply:telegram:123:1")
    assert archive.resolve_telegram(123, 1) == "telegram:123:1"
    assert archive.resolve_telegram(123, 10) == archive.resolve_telegram(123, 11) == "reply:telegram:123:1"
    assert archive.resolve_telegram(123, 12) is None
    assert archive.resolve_telegram(124, 10) is None
    archive.record_outputs(123, [11], "proactive")
    assert archive.resolve_telegram(123, 11) == "proactive"
    assert archive.resolve_telegram(123, 10) == "reply:telegram:123:1"
    archive.record_outputs(123, [], "nothing")
    assert archive.db.execute("SELECT count(*) FROM telegram_outputs").fetchone()[0] == 2


def test_legacy_web_context_migration_is_once_and_lands_in_the_home_chat(tmp_path):
    db = sqlite3.connect(tmp_path / "companion.sqlite3")
    db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY, space TEXT, role TEXT, text TEXT,
        status TEXT, created REAL, reply_to TEXT, error TEXT DEFAULT '');
        INSERT INTO messages VALUES ('old','wiki/projects/garden.md','user','Watered','done',1,NULL,'');
        INSERT INTO messages VALUES ('answer','wiki/projects/garden.md','assistant','Noted','done',2,'old','');""")
    db.close()
    archive = ConversationArchive(tmp_path)
    assert archive.load_context("wiki/projects/garden.md") == []
    assert [r["content"] for r in archive.load_context("general")] == ["Watered", "Noted"]
    assert "Watered" in str(Agent(VaultTools(tmp_path / "vault"), archive=archive)._get_history(WEB_CHAT_ID).messages())
    archive.close()
    archive = ConversationArchive(tmp_path)
    assert len(archive.load_context("general")) == 2
    assert archive.get("old")["space"] == "general"
    archive.close()


def _seed_threaded_archive(state_dir):
    db = sqlite3.connect(state_dir / "companion.sqlite3")
    db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY, space TEXT, role TEXT, text TEXT,
        status TEXT, created REAL, reply_to TEXT, error TEXT DEFAULT '');
        CREATE TABLE context_records(id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT, exchange_id TEXT,
        role TEXT, content TEXT, completed_at TEXT, generation INTEGER);
        CREATE TABLE context_generations(space TEXT PRIMARY KEY, generation INTEGER);
        CREATE TABLE pending_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT, content TEXT);
        CREATE TABLE archive_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO archive_meta VALUES ('context_import','1');
        INSERT INTO messages VALUES ('m0','telegram:123:0','user','thread zero','done',1,NULL,'');
        INSERT INTO messages VALUES ('m7','telegram:123:7','user','thread seven','done',2,NULL,'');
        INSERT INTO messages VALUES ('t5','topic:5','user','home topic','done',3,NULL,'');
        INSERT INTO messages VALUES ('g','general','user','home','done',4,NULL,'');
        INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation)
            VALUES ('telegram:123:0','m0','user','thread zero','1',2),
                   ('telegram:123:7','m7','user','thread seven','2',5),
                   ('topic:5','t5','user','home topic','3',0),
                   ('general','g','user','home','4',1);
        INSERT INTO context_generations VALUES ('telegram:123:0',2),('telegram:123:7',5),
                                               ('topic:5',3),('general',1);
        INSERT INTO pending_notes(space,content) VALUES ('telegram:123:0','note zero'),
            ('telegram:123:7','note seven'),('topic:5','note topic'),('general','note home');""")
    db.commit()
    db.close()


def test_threaded_telegram_spaces_are_flattened_once_into_their_chat(tmp_path):
    _seed_threaded_archive(tmp_path)
    archive = ConversationArchive(tmp_path)
    try:
        spaces = {row[0] for table in ("messages", "context_records", "context_generations", "pending_notes")
                  for row in archive.db.execute(f"SELECT DISTINCT space FROM {table}")}
        assert spaces == {"telegram:123", "general"}
        assert {archive.get(i)["space"] for i in ("m0", "m7")} == {"telegram:123"}
        assert [r["content"] for r in archive.load_context("telegram:123")] == ["thread zero", "thread seven"]
        assert [r[0] for r in archive.db.execute("SELECT content FROM pending_notes WHERE space='telegram:123'")] == ["note zero", "note seven"]
        assert archive.generation("telegram:123") == 5
        assert archive.generation("telegram:123:0") == archive.generation("telegram:123:7") == 0
        # The home topic joins the one chat, in the era of the general row it precedes
        # (the message column was just added with default 0; the context record says 1).
        assert archive.get("t5")["space"] == archive.get("g")["space"] == "general"
        assert archive.get("t5")["generation"] == archive.get("g")["generation"] == 0
        assert [(r["content"], r["generation"]) for r in archive.load_context("general")] == [("home topic", 1), ("home", 1)]
        assert [r[0] for r in archive.db.execute("SELECT content FROM pending_notes WHERE space='general'")] == ["note topic", "note home"]
        assert {r["thread"] for r in archive.db.execute("SELECT thread FROM messages")} == {"m0", "m7", "t5", "g"}
        assert archive.generation("topic:5") == 0 and archive.generation("general") == 1
        assert archive.db.execute("SELECT value FROM archive_meta WHERE key='flat_spaces'").fetchone()[0] == "1"
        # Reopening must not migrate again: a threaded space added afterwards stays as written.
        archive.db.execute("INSERT INTO context_generations VALUES ('telegram:123:9', 9)")
        archive.db.commit()
    finally:
        archive.close()
    archive = ConversationArchive(tmp_path)
    try:
        assert archive.generation("telegram:123:9") == 9
        assert archive.generation("telegram:123") == 5
    finally:
        archive.close()


def test_old_rooms_merge_into_the_home_chat_without_inventing_resets(tmp_path):
    db = sqlite3.connect(tmp_path / "companion.sqlite3")
    db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY, space TEXT, role TEXT, text TEXT,
        status TEXT, created REAL, reply_to TEXT, error TEXT DEFAULT '', generation INTEGER DEFAULT 0);
        CREATE TABLE context_records(id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT, exchange_id TEXT,
        role TEXT, content TEXT, completed_at TEXT, generation INTEGER);
        CREATE TABLE context_generations(space TEXT PRIMARY KEY, generation INTEGER);
        CREATE TABLE pending_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT, content TEXT);
        CREATE TABLE archive_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE seen(space TEXT PRIMARY KEY, through REAL);
        INSERT INTO archive_meta VALUES ('context_import','1'),('flat_spaces','1');
        INSERT INTO messages VALUES ('g1','general','user','first','done',1,NULL,'',0);
        INSERT INTO messages VALUES ('g2','general','assistant','after reset','done',5,NULL,'',1);
        INSERT INTO messages VALUES ('ta','topic:9','user','before everything','done',0.5,NULL,'',0);
        INSERT INTO messages VALUES ('tb','topic:9','assistant','old era','done',3,NULL,'',2);
        INSERT INTO messages VALUES ('tc','topic:9','assistant','new era','done',7,NULL,'',2);
        INSERT INTO messages VALUES ('w','wiki/projects/garden.md','user','project chat','done',6,NULL,'',4);
        INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation)
            VALUES ('topic:9','ta','user','before everything','0.5',0),
                   ('general','g1','user','first','1',0),
                   ('topic:9','tb','assistant','old era','3',2),
                   ('general','g2','assistant','after reset','5',1),
                   ('wiki/projects/garden.md','w','user','project chat','6',4),
                   ('topic:9','tc','assistant','new era','7',2);
        INSERT INTO context_generations VALUES ('general',1),('topic:9',2),('wiki/projects/garden.md',4);
        INSERT INTO pending_notes(space,content) VALUES ('topic:9','room note');
        INSERT INTO seen VALUES ('general',4.0),('topic:9',6.5);""")
    db.commit()
    db.close()
    archive = ConversationArchive(tmp_path)
    try:
        rows = archive.db.execute("SELECT id, space, generation FROM messages ORDER BY created").fetchall()
        assert [(r["id"], r["space"], r["generation"]) for r in rows] == [
            ("ta", "general", 0), ("g1", "general", 0), ("tb", "general", 0),
            ("g2", "general", 1), ("w", "general", 1), ("tc", "general", 1)]
        assert [(r["content"], r["generation"]) for r in archive.load_context("general")] == [
            ("before everything", 0), ("first", 0), ("old era", 0),
            ("after reset", 1), ("project chat", 1), ("new era", 1)]
        history = Agent(VaultTools(tmp_path / "vault"), archive=archive)._get_history(WEB_CHAT_ID).messages()
        assert "new era" in str(history) and "project chat" in str(history) and "old era" not in str(history)
        assert [r[0] for r in archive.db.execute("SELECT content FROM pending_notes WHERE space='general'")] == ["room note"]
        assert all(r["thread"] == r["id"] for r in archive.db.execute("SELECT id, thread FROM messages"))
        assert archive.generation("general") == 1 and archive.generation("topic:9") == 0
        assert [tuple(r) for r in archive.db.execute("SELECT space, through FROM seen")] == [("general", 6.5)]
        assert archive.db.execute("SELECT value FROM archive_meta WHERE key='merge_home_spaces'").fetchone()[0] == "1"
        archive.db.execute("INSERT INTO context_generations VALUES ('topic:2', 7)")
        archive.db.commit()
    finally:
        archive.close()
    archive = ConversationArchive(tmp_path)
    try:
        assert archive.generation("topic:2") == 7  # migrated once, never again
    finally:
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
        await bot._answer_batch(123, _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
            items=[_BatchItem(one, "A voice transcript"), _BatchItem(two, "attachments/photo.jpg")]))
        await bot._answer_batch(123, _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
            items=[_BatchItem(two, "attachments/photo.jpg"), _BatchItem(three, "New input")]))
    rows = archive.db.execute("SELECT * FROM messages WHERE role='user' ORDER BY created").fetchall()
    assert len(rows) == 2
    assert rows[0]["source"] == "telegram" and rows[0]["space"] == "general"
    assert json.loads(rows[0]["metadata"])["message_ids"] == [1, 2]
    assert "thread_id" not in json.loads(rows[0]["metadata"])
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
        await bot._answer_batch(123, _PendingBatch(bot=MagicMock(send_chat_action=AsyncMock()),
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
