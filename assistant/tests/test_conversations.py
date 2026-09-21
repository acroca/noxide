"""Durable history, reset generations, threads, and what an old archive sheds on open."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant.agent import Agent
from assistant.conversations import SPACE, ConversationArchive
from assistant.copilot import CopilotUnavailableError
from assistant.history import ConversationHistory
from assistant.tools import VaultTools

from .test_agent import _make_text_response, _make_tool_call_response


@pytest.fixture
def setup(tmp_path):
    archive = ConversationArchive(tmp_path)
    vault = VaultTools(tmp_path / "vault")
    agent = Agent(vault, archive=archive)
    yield archive, vault, agent
    archive.close()


async def test_restores_five_exchanges_and_searches_older_text(setup):
    archive, vault, agent = setup
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Recorded")))
    root = archive.insert(SPACE, "user", "message-0", "queued")
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run("message-0", message_id=root)
        for n in range(1, 7):
            await agent.run(f"message-{n}", reply_to=root)
        restored = Agent(vault, archive=archive)
        await restored.run("followup", reply_to=root)
    sent = str(client.chat.call_args.args[0])
    assert "message-0" not in sent and "message-1" not in sent
    for n in range(2, 7):
        assert f"message-{n}" in sent
    history = restored._get_history(root)
    assert "message-0" in history.retrieve("search_history", {"query": "message-0"})
    rows = archive.db.execute("SELECT * FROM messages WHERE role='user'").fetchall()
    assert len(rows) == 8
    assert {r["space"] for r in rows} == {SPACE}
    assert {r["thread"] for r in rows} == {root}


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
    root = archive.insert(SPACE, "user", "first", "queued")
    with patch("assistant.copilot.get_client", return_value=client):
        first = asyncio.create_task(agent.run("first", message_id=root))
        await started.wait()
        second = asyncio.create_task(agent.run("second", reply_to=root))
        third = asyncio.create_task(agent.run("third"))
        for _ in range(5):
            await asyncio.sleep(0)
        # The reply waits for its thread; the new root does not.
        assert third.done() and "second" not in calls
        assert [m["role"] for m in calls["third"]] == ["system", "user"]
        # A reset returns at once instead of waiting for the in-flight thread...
        reset = asyncio.create_task(agent.reset_conversation())
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
    assert agent._get_history(root).messages() == []
    assert "third" in agent._get_history(root).retrieve("search_history", {"query": "third"})
    assert "first" not in str(archive.load_context(SPACE))
    rows = {r["text"]: r for r in archive.db.execute("SELECT * FROM messages")}
    assert set(rows) == {"first", "second", "third", "finished"}
    assert rows["second"]["status"] == "dismissed" and rows["second"]["thread"] == root
    # The in-flight run's failure must not revive the tombstoned row as retryable work.
    assert rows["first"]["status"] == "dismissed" and rows["first"]["error"] == ""
    assert rows["third"]["status"] == "done" and rows["finished"]["thread"] == rows["third"]["id"]
    assert archive.generation() == 1


async def test_reset_dismisses_pending_work_but_keeps_archived_text(setup):
    archive, vault, agent = setup
    mid = archive.insert(SPACE, "user", "secret-pending", "queued")
    client = MagicMock(chat=AsyncMock(side_effect=CopilotUnavailableError("offline")))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run("secret-pending", message_id=mid)
        await agent.reset_conversation()
        assert await agent.resume(mid) is None
    row = archive.get(mid)
    assert row["status"] == "dismissed" and row["text"] == "secret-pending"


async def test_outage_resume_commits_one_reply_and_completed_context(setup):
    archive, vault, agent = setup
    vault.write_file("large.md", "x" * 6000)
    mid = archive.insert(SPACE, "user", "read it", "queued")
    client = MagicMock(chat=AsyncMock(side_effect=[
        _make_tool_call_response("read_file", {"path": "large.md"}),
        CopilotUnavailableError("offline"), _make_text_response("Done"),
    ]))
    with patch("assistant.copilot.get_client", return_value=client):
        with pytest.raises(CopilotUnavailableError):
            await agent.run("read it", message_id=mid)
        assert archive.get(mid)["status"] == "unavailable"
        assert await agent.resume(mid) == "Done"
        assert await agent.resume(mid) is None
    assert "x" * 6000 in str(client.chat.call_args.args[0])
    assert archive.get(mid)["status"] == "done"
    assert archive.reply(mid) == "Done"
    assert archive.db.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0] == 1
    restored = ConversationHistory(archive=archive, space=SPACE)
    assert "x" * 6000 not in str(restored.messages())
    assert "read it" in str(restored.messages())


def test_atomic_completion_rolls_back_context_and_request_status(setup):
    archive, _, _ = setup
    mid = archive.insert(SPACE, "user", "question", "queued")
    archive.db.execute("""CREATE TRIGGER reject_reply BEFORE INSERT ON messages
                        WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'test'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        archive.save_context(SPACE, [{"role": "user", "content": "question"},
                                     {"role": "assistant", "content": "answer"}], "now",
                             thread=mid, message_id=mid, request_ids={mid})
    assert archive.load_context(SPACE) == []
    assert archive.get(mid)["status"] == "queued"
    assert archive.reply(mid) is None


def test_completion_stamps_the_thread_on_records_and_reply(setup):
    archive, _, _ = setup
    root = archive.insert(SPACE, "user", "question", "done")
    mid = archive.insert(SPACE, "user", "follow-up", "queued", reply_to=root)
    exchange = [{"role": "user", "content": "follow-up"}, {"role": "assistant", "content": "answer"}]
    archive.save_context(SPACE, exchange, "now", thread=root, message_id=mid, request_ids={mid})
    assert [r["thread"] for r in archive.load_context(SPACE)] == [root, root]
    assert archive.load_context(SPACE, root) == archive.load_context(SPACE)
    assert archive.load_context(SPACE, mid) == []
    reply = archive.get(f"reply:{mid}")
    assert reply["thread"] == root and reply["reply_to"] == mid
    assert archive.get(mid)["status"] == "done"
    assert [r["id"] for r in exchange] == [1, 2]


async def test_delivery_root_survives_restart_and_is_shown_once_as_background(setup):
    archive, vault, agent = setup
    archive.insert(SPACE, "assistant", "Medicine reminder", "done")
    restored = Agent(vault, archive=archive)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Done")))
    with patch("assistant.copilot.get_client", return_value=client):
        await restored.run("taken")
    sent = client.chat.call_args.args[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[-1]["content"].count("Medicine reminder") == 1
    assert "Assistant (sent from a scheduled run): Medicine reminder" in sent[-1]["content"]
    assert "Medicine reminder" not in str(archive.load_context(SPACE))


def test_insert_joins_the_parent_thread_or_starts_one(setup):
    archive, _, _ = setup
    root = archive.insert(SPACE, "user", "root", "done")
    reply = archive.insert(SPACE, "assistant", "reply", "done", reply_to=root)
    deeper = archive.insert(SPACE, "user", "deeper", "queued", reply_to=reply)
    fresh = archive.insert(SPACE, "user", "fresh", "queued")
    dangling = archive.insert(SPACE, "user", "dangling", "queued", reply_to="missing")
    assert archive.get(root)["thread"] == root and archive.get(root)["reply_to"] is None
    assert archive.get(reply)["thread"] == root and archive.get(reply)["reply_to"] == root
    assert archive.get(deeper)["thread"] == root and archive.get(deeper)["reply_to"] == reply
    assert archive.get(fresh)["thread"] == fresh
    assert archive.get(dangling)["thread"] == dangling and archive.get(dangling)["reply_to"] == "missing"
    # An idempotent resend keeps the first row (and its thread) untouched.
    assert archive.insert(SPACE, "user", "deeper", "queued", message_id=deeper) == deeper
    assert archive.get(deeper)["thread"] == root
    with pytest.raises(ValueError):
        archive.insert(SPACE, "user", "other text", "queued", message_id=deeper)


def test_recent_threads_orders_by_last_activity_and_filters(setup):
    archive, _, _ = setup

    def at(message_id, created):
        archive.db.execute("UPDATE messages SET created=? WHERE id=?", (created, message_id))
        archive.db.commit()

    old = archive.insert(SPACE, "user", "old era", "done")
    archive.reset()
    at(old, 100)
    a = archive.insert(SPACE, "user", "a root", "done")
    at(a, 10)
    a_reply = archive.insert(SPACE, "assistant", "a first reply", "done", reply_to=a)
    at(a_reply, 11)
    a_last = archive.insert(SPACE, "assistant", "a latest reply", "done", reply_to=a)
    at(a_last, 50)
    b = archive.insert(SPACE, "assistant", "b delivery", "done")
    at(b, 20)
    b_pending = archive.insert(SPACE, "user", "b pending", "running", reply_to=b)
    at(b_pending, 60)
    c = archive.insert(SPACE, "user", "c root", "done")
    at(c, 30)
    stale = archive.insert(SPACE, "user", "too old", "done")
    at(stale, 1)
    queued = archive.insert(SPACE, "user", "not done", "queued")
    at(queued, 70)
    # Rows of a Telegram-era chat stay in the database but never surface.
    elsewhere = archive.insert("telegram:9", "user", "other chat", "done")
    at(elsewhere, 80)

    def recent(**kwargs):
        return archive.recent_threads(SPACE, **{"generation": 1, "since": 5, "limit": 5, **kwargs})

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


def test_opening_an_old_archive_drops_telegram_tables_and_keeps_its_rows(tmp_path):
    """An archive from the Telegram era opens as-is: its extra columns and other
    chats' rows stay (harmless, never shown), the Telegram lookup tables go."""
    db = sqlite3.connect(tmp_path / "companion.sqlite3")
    db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY, space TEXT, role TEXT, text TEXT,
        status TEXT, created REAL, reply_to TEXT, error TEXT DEFAULT '', source TEXT DEFAULT 'web',
        metadata TEXT DEFAULT '{}', generation INTEGER DEFAULT 0, delivery TEXT DEFAULT '', thread TEXT DEFAULT '');
        CREATE TABLE context_records(id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT, exchange_id TEXT,
        role TEXT, content TEXT, completed_at TEXT, generation INTEGER, thread TEXT);
        CREATE TABLE context_generations(space TEXT PRIMARY KEY, generation INTEGER);
        CREATE TABLE archive_meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE pending_notes(id INTEGER PRIMARY KEY, space TEXT, content TEXT);
        CREATE TABLE telegram_inputs(chat_id INTEGER, message_id INTEGER, request_id TEXT);
        CREATE TABLE telegram_outputs(chat_id INTEGER, message_id INTEGER, archive_id TEXT);
        INSERT INTO messages VALUES ('h','general','user','home','done',1,NULL,'','telegram','{}',0,'delivered','h');
        INSERT INTO messages VALUES ('r','general','assistant','answer','done',2,'h','','telegram','{}',0,'delivered','h');
        INSERT INTO messages VALUES ('o','telegram:555','user','other chat','done',3,NULL,'','telegram','{}',0,'','o');
        INSERT INTO messages VALUES ('p','general','user','pending','running',4,NULL,'','web','{}',0,'','p');
        INSERT INTO context_records(space,exchange_id,role,content,completed_at,generation,thread)
            VALUES ('general','h','user','home','1',0,'h'),('general','h','assistant','answer','2',0,'h');
        INSERT INTO context_generations VALUES ('general',0),('telegram:555',3);""")
    db.commit()
    db.close()
    archive = ConversationArchive(tmp_path)
    try:
        tables = {r[0] for r in archive.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {"telegram_inputs", "telegram_outputs", "pending_notes"}
        assert archive.get("o")["space"] == "telegram:555"
        assert archive.get("p")["status"] == "interrupted"
        assert [r["content"] for r in archive.load_context(SPACE, "h")] == ["home", "answer"]
        history = Agent(VaultTools(tmp_path / "vault"), archive=archive)._get_history("h").messages()
        assert [m["content"] for m in history] == [] or "home" in str(history)
        # New rows still insert alongside the legacy columns.
        fresh = archive.insert(SPACE, "user", "new", "queued")
        assert archive.get(fresh)["source"] == "web" and archive.get(fresh)["delivery"] == ""
    finally:
        archive.close()


async def test_database_reopen_restores_only_completed_text(tmp_path):
    vault = VaultTools(tmp_path / "vault")
    archive = ConversationArchive(tmp_path)
    agent = Agent(vault, archive=archive)
    client = MagicMock(chat=AsyncMock(return_value=_make_text_response("Remembered answer")))
    root = archive.insert(SPACE, "user", "Remembered question", "queued")
    with patch("assistant.copilot.get_client", return_value=client):
        await agent.run("Remembered question", message_id=root)
    archive.close()
    archive = ConversationArchive(tmp_path)
    try:
        restored = Agent(vault, archive=archive)
        with patch("assistant.copilot.get_client", return_value=client):
            await restored.run("Follow up", reply_to=root)
        sent = str(client.chat.call_args.args[0])
        assert "Remembered question" in sent and "Remembered answer" in sent
        assert len(archive.load_context(SPACE)) == 4
    finally:
        archive.close()
