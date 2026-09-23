"""The web app's trust boundary and accepted-message lifecycle."""

import asyncio
import base64
import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant.companion import SPACE, Companion
from assistant.config import Config, ConfigError
from assistant.conversations import ConversationArchive
from assistant.copilot import CopilotUnavailableError
from assistant.models import ModelOption, ModelPicker
from assistant.tools import VaultTools


def complete(archive, message_id, text, reply):
    """Finish an archived request the way the real agent loop does: context
    records, the reply row, and the request marked done, atomically."""
    archive.save_context(SPACE, [{"role": "user", "content": text}, {"role": "assistant", "content": reply}],
                         "now", thread=archive.thread_of(message_id), message_id=message_id,
                         request_ids={message_id})
    return reply


class FakeAgent:
    """Stands in for Agent: answers every run with a fixed reply, completing the row."""

    def __init__(self, archive, reply="Recorded."):
        self.archive = archive
        self.reply = reply
        self.run = AsyncMock(side_effect=self._run)
        self.resume = AsyncMock(side_effect=self._resume)
        self.reset_conversation = AsyncMock(side_effect=lambda: archive.reset())

    async def _run(self, text, *, message_id=None, **kwargs):
        return complete(self.archive, message_id, text, self.reply)

    async def _resume(self, message_id=None, **kwargs):
        return complete(self.archive, message_id, self.archive.get(message_id)["text"], "Recovered.")


@pytest.fixture
async def companion(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    vault = VaultTools(tmp_path / "vault")
    archive = ConversationArchive(state)
    agent = FakeAgent(archive)
    cfg = Config(state_dir=state, vault_path=tmp_path / "vault")
    service = Companion(cfg, agent, vault, archive=archive)
    server = TestServer(service.app)
    client = TestClient(server)
    await client.start_server()
    cfg.pwa_origin = str(server.make_url("")).rstrip("/")
    client.session.headers.update({"Origin": cfg.pwa_origin, "X-Noxide": "1"})
    try:
        yield service, client
    finally:
        await service.close()
        await client.close()
        archive.close()


async def settle(service):
    await asyncio.gather(*list(service.tasks.values()))
    await asyncio.sleep(0)


def flat(page):
    """The messages of a thread page in display order: threads by start, each oldest first."""
    return [message for thread in page["threads"] for message in thread["messages"]]


async def timeline(client, **query):
    return await (await client.get("/api/messages", params=query)).json()


async def test_no_password_required_but_csrf_and_host_checks_remain(companion):
    service, client = companion
    assert (await client.get("/")).status == 200
    for path in ("/api/session", "/api/now", "/api/messages"):
        response = await client.get(path)
        assert response.status == 200
        assert "Set-Cookie" not in response.headers
    assert (await client.post("/api/reset", json={}, headers={"Origin": "https://evil.test"})).status == 403
    assert (await client.post("/api/reset", json={}, headers={"X-Noxide": ""})).status == 403
    assert (await client.get("/api/now", headers={"Host": "evil.test"})).status == 403
    response = await client.get("/api/now")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert (await client.post("/api/reset", json={})).status == 200
    service.agent.reset_conversation.assert_awaited_once_with()
    # Deleting a chat was removed with its endpoint; reset is the only way to start over.
    assert (await client.post("/api/clear", json={})).status == 404
    # The topic picker and the vault overview went with the topics: one chat, Now is the only page.
    for path in ("/api/topics", "/api/overview", "/api/page?path=wiki/now.md"):
        assert (await client.get(path)).status == 404


async def test_auth_routes_and_stored_sessions_removed(companion):
    service, client = companion
    assert (await client.post("/api/login", json={})).status == 404
    assert (await client.post("/api/logout", json={})).status == 404
    assert service.db.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None
    service.db.execute("CREATE TABLE sessions(token TEXT PRIMARY KEY, expires REAL)")
    service.db.execute("INSERT INTO sessions VALUES ('old-session-hash', 9999999999)")
    service.db.commit()
    upgraded = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    try:
        assert service.db.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None
    finally:
        await upgraded.close()


def test_pwa_configuration_requires_no_password(tmp_path):
    from assistant.copilot import OAUTH_TOKEN_FILENAME

    (tmp_path / OAUTH_TOKEN_FILENAME).write_text("test-token")
    cfg = Config(state_dir=tmp_path, pwa_origin="https://mini.example.ts.net")
    cfg.validate_for_run()
    assert "pwa_password" not in Config.model_fields


async def test_submit_idempotency_and_queueing(companion):
    service, client = companion
    release = asyncio.Event()

    async def run(text, *, message_id, send_message_fn, **kwargs):
        await release.wait()
        await send_message_fn("An intermediate update")
        return complete(service.archive, message_id, text, "Finished")

    service.agent.run.side_effect = run
    data = {"id": "a" * 32, "text": "Watered the plants"}
    try:
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json={**data, "text": "different"})).status == 409
        # A second message is accepted while the first runs; each is its own
        # thread, and threads run in parallel.
        assert (await client.post("/api/messages", json={**data, "id": "b" * 32, "text": "And fed them"})).status == 202
        page = await timeline(client)
        assert [(m["text"], m["status"]) for m in flat(page)] == [("Watered the plants", "queued"), ("And fed them", "queued")]
        # Two messages sent without replying are two threads, each rooted at its own id.
        assert [(t["id"], [m["thread"] for m in t["messages"]]) for t in page["threads"]] == [("a" * 32, ["a" * 32]), ("b" * 32, ["b" * 32])]
        assert (await client.post("/api/reset", json={})).status == 409
    finally:
        release.set()
    await settle(service)
    assert [c.args[0] for c in service.agent.run.call_args_list] == ["Watered the plants", "And fed them"]
    page = await timeline(client)
    assert all(m["status"] == "done" for m in flat(page))
    replies = [m["reply_to"] for m in flat(page) if m["role"] == "assistant"]
    assert sorted(replies) == sorted(["a" * 32] * 2 + ["b" * 32] * 2)
    # Replies land in the thread of the message they answer, not in a thread of their own.
    assert [(t["id"], len(t["messages"])) for t in page["threads"]] == [("a" * 32, 3), ("b" * 32, 3)]
    assert all(m["thread"] == t["id"] for t in page["threads"] for m in t["messages"])
    # The agent completes the archived row itself: every run names it.
    for call in service.agent.run.call_args_list:
        assert call.kwargs["message_id"] in ("a" * 32, "b" * 32)
        assert callable(call.kwargs["on_research"]) and callable(call.kwargs["send_message_fn"])
    rows = flat(await timeline(client))
    assert sorted(r["text"] for r in rows) == sorted([data["text"], "And fed them"] + ["An intermediate update", "Finished"] * 2)
    assert {r["space"] for r in rows} == {SPACE}


async def test_reply_to_joins_the_parent_thread_and_is_validated(companion):
    service, client = companion
    root = {"id": "a" * 32, "text": "Watered the plants"}
    assert (await client.post("/api/messages", json=root)).status == 202
    await settle(service)
    reply = service.db.execute("SELECT id FROM messages WHERE reply_to=?", (root["id"],)).fetchone()["id"]
    # A reply to the assistant's answer continues the root's thread, as does the answer to it.
    follow_up = {"id": "b" * 32, "text": "And fed them", "reply_to": reply}
    assert (await client.post("/api/messages", json=follow_up)).status == 202
    await settle(service)
    assert service.archive.get("b" * 32)["thread"] == root["id"]
    page = await timeline(client)
    assert [t["id"] for t in page["threads"]] == [root["id"]]
    assert [(m["role"], m["text"], m["thread"]) for m in page["threads"][0]["messages"]] == [
        ("user", "Watered the plants", root["id"]), ("assistant", "Recorded.", root["id"]),
        ("user", "And fed them", root["id"]), ("assistant", "Recorded.", root["id"])]
    assert page["threads"][0]["started"] == page["threads"][0]["messages"][0]["created"]
    # An idempotent resend must match the parent too; a different one is a fresh conflict.
    assert (await client.post("/api/messages", json=follow_up)).status == 202
    assert (await client.post("/api/messages", json={**follow_up, "reply_to": None})).status == 409
    assert (await client.post("/api/messages", json={**follow_up, "reply_to": root["id"]})).status == 409
    assert service.db.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0] == 2
    # The parent must be a message of this chat: unknown, Telegram-era, deleted or malformed ids are refused.
    other = service._insert("telegram:555", "user", "Another chat", "done")
    deleted = service._insert(SPACE, "user", "Gone", "deleted")
    for bad in ("f" * 32, other, deleted, 123, ""):
        response = await client.post("/api/messages", json={"id": "c" * 32, "text": "Orphan", "reply_to": bad})
        assert response.status == 400, bad
        assert "not in this chat" in (await response.json())["error"]
    assert service.archive.get("c" * 32) is None


async def test_outage_retry_resumes_the_failed_turn_in_place(companion):
    service, client = companion
    service.agent.run.side_effect = CopilotUnavailableError("offline")
    data = {"id": "a" * 32, "text": "Remember this"}
    await client.post("/api/messages", json=data)
    await settle(service)
    rows = flat(await timeline(client))
    assert rows[0]["status"] == "unavailable"
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 200
    await settle(service)
    service.agent.resume.assert_awaited_once()
    call = service.agent.resume.call_args
    assert call.args == (data["id"],) and callable(call.kwargs["send_message_fn"])
    assert [(m["role"], m["text"], m["status"]) for m in flat(await timeline(client))] == [
        ("user", "Remember this", "done"), ("assistant", "Recovered.", "done")]
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 409


async def test_restart_marks_interrupted_work_without_replaying(companion):
    service, client = companion
    service._insert(SPACE, "user", "Potential side effect", "running", message_id="a" * 32)
    reopened = ConversationArchive(service.cfg.state_dir)
    try:
        row = reopened.get("a" * 32)
        assert row["status"] == "interrupted"
        assert "partially completed" in row["error"]
    finally:
        reopened.close()
    assert service.tasks == {}
    assert (await client.post("/api/retry", json={"id": "a" * 32})).status == 200
    await settle(service)
    assert "Explicit retry after interruption" in service.agent.run.call_args.args[0]


async def test_messages_open_on_a_small_page_with_older_ones_behind_a_cursor(companion):
    service, client = companion
    for i in range(21):
        service._insert(SPACE, "user", f"m{i}", "done")
    data = await timeline(client)
    assert [m["text"] for m in flat(data)] == [f"m{i}" for i in range(1, 21)]
    assert [t["started"] for t in data["threads"]] == [m["created"] for m in flat(data)]
    assert data["before"] == data["threads"][0]["started"]
    older = await timeline(client, before=data["before"])
    assert [m["text"] for m in flat(older)] == ["m0"] and older["before"] is None


async def test_paging_walks_threads_by_their_first_message(companion):
    service, client = companion
    roots = [service._insert(SPACE, "user", f"m{i}", "done") for i in range(21)]
    # A late reply does not move its thread: the timeline is ordered by first
    # message, so the oldest thread stays on the older page, reply included.
    service._insert(SPACE, "assistant", "Answering m0 late", "done", reply_to=roots[0])
    data = await timeline(client)
    assert [t["id"] for t in data["threads"]] == roots[1:]
    assert data["before"] == data["threads"][0]["started"] == service.archive.get(roots[1])["created"]
    older = await timeline(client, before=data["before"])
    assert [t["id"] for t in older["threads"]] == [roots[0]] and older["before"] is None
    assert [(m["text"], m["thread"]) for m in older["threads"][0]["messages"]] == [("m0", roots[0]), ("Answering m0 late", roots[0])]
    assert older["threads"][0]["started"] == service.archive.get(roots[0])["created"]
    # A cursor before everything finds nothing.
    empty = await timeline(client, before=service.archive.get(roots[0])["created"])
    assert empty["threads"] == [] and empty["before"] is None


async def test_web_search_shows_on_the_running_message(companion):
    service, client = companion
    release = asyncio.Event()

    async def run(text, *, message_id, on_research, **kwargs):
        await on_research()
        await release.wait()
        return complete(service.archive, message_id, text, "Found it.")

    service.agent.run.side_effect = run
    data = {"id": "c" * 32, "text": "What is the capital of Bhutan?"}
    assert (await client.post("/api/messages", json=data)).status == 202
    await asyncio.sleep(0)
    assert flat(await timeline(client))[0]["activity"] == "Searching the web…"
    release.set()
    await settle(service)
    assert "activity" not in flat(await timeline(client))[0] and service.activity == {}


async def test_reset_marks_a_new_generation_the_timeline_can_draw(companion):
    service, client = companion
    service._insert(SPACE, "user", "before", "done")
    await service.deliver("A scheduled reminder")
    data = await timeline(client)
    assert data["generation"] == 0 and [m["generation"] for m in flat(data)] == [0, 0]
    assert (await client.post("/api/reset", json={})).status == 200
    service._insert(SPACE, "user", "after", "done")
    data = await timeline(client)
    assert data["generation"] == 1
    assert [(m["text"], m["generation"]) for m in flat(data)] == [("before", 0), ("A scheduled reminder", 0), ("after", 1)]
    # Each is its own thread, so the timeline can draw the divider between the roots' generations.
    assert [(t["id"], t["messages"][0]["generation"]) for t in data["threads"]] == [(m["id"], m["generation"]) for m in flat(data)]


async def test_push_keys_persist_and_endpoints_are_restricted(companion):
    service, client = companion
    service.public_key = "configured"
    keys = {"p256dh": "a" * 87, "auth": "b" * 22}
    for endpoint in ("http://127.0.0.1/push", "https://evil.test/push", "https://fcm.googleapis.com@evil.test/push",
                     "https://fcm.googleapis.com:444/push", "https://fcm.googleapis.com.evil.test/push"):
        assert (await client.post("/api/push", json={"endpoint": endpoint, "keys": keys})).status == 400
    endpoint = "https://fcm.googleapis.com/fcm/send/test"
    assert (await client.post("/api/push", json={"endpoint": endpoint, "keys": keys})).status == 200
    with patch("pywebpush.webpush") as send:
        await service._push("Hello there! This is the test reminder")
        assert json.loads(send.call_args.kwargs["data"]) == {
            "body": "Hello there! This is the test reminder", "agent_name": service.cfg.agent_name, "thread": None, "unread": 0}
        assert send.call_args.kwargs["requests_session"].max_redirects == 0
        # A reply's push names its thread, so the notification click can open it in reply mode.
        await service._push("Recorded.", "a" * 32)
        assert json.loads(send.call_args.kwargs["data"]) == {
            "body": "Recorded.", "agent_name": service.cfg.agent_name, "thread": "a" * 32, "unread": 0}
    assert (await client.delete("/api/push", json={"endpoint": endpoint})).status == 200
    assert service.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0
    service.cfg.pwa_push_contact = "mailto:test@example.com"
    other = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    key = other.public_key
    assert len(key) == 87
    await other.close()
    other = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    assert other.public_key == key
    await other.close()


async def test_push_preview_is_bounded_and_preserves_unicode(companion):
    service, client = companion
    service.public_key = "configured"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/test",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send:
        await service._push("\U0001f331" * 2000)
    payload = send.call_args.kwargs["data"]
    assert len(payload.encode("utf-8")) < 3000
    assert json.loads(payload) == {"body": "\U0001f331" * 500 + "...", "agent_name": service.cfg.agent_name, "thread": None, "unread": 0}


async def test_agent_name_in_shell_manifest_session_and_worker(companion):
    import html

    service, client = companion
    name = 'Juniper " & <script>alert(1)</script>'
    service.cfg.agent_name = name
    shell = await (await client.get('/')).text()
    assert html.escape(name, quote=True) in shell
    assert name not in shell
    assert '__AGENT_NAME__' not in shell
    manifest = await (await client.get('/manifest.webmanifest')).json()
    assert manifest['name'] == manifest['short_name'] == name
    assert manifest['id'] == '/'
    assert (await (await client.get('/api/session')).json())['agent_name'] == name
    worker = await (await client.get('/sw.js')).text()
    assert f'const AGENT_NAME = {json.dumps(name)};' in worker
    service.cfg.agent_name = 'Cedar'
    renamed_worker = await (await client.get('/sw.js')).text()
    def cache_line(text):
        return next(line for line in text.splitlines() if line.startswith('const CACHE'))
    assert cache_line(worker) != cache_line(renamed_worker)


async def test_push_triggers_include_reply_reminder_and_test_text(companion):
    service, client = companion
    with patch.object(service, "notify_push") as notify:
        await service.deliver("Your reminder")
        reminder = service.db.execute("SELECT id, thread FROM messages WHERE text='Your reminder'").fetchone()
        assert reminder["thread"] == reminder["id"]
        notify.assert_called_with("Your reminder", thread=reminder["id"], message_id=reminder["id"])
        await client.post("/api/messages", json={"id": "c" * 32, "text": "Hi"})
        await settle(service)
        notify.assert_called_with("Recorded.", thread="c" * 32, message_id="reply:" + "c" * 32)
        # A reply in an existing thread pushes that thread, not its own message id.
        await client.post("/api/messages", json={"id": "d" * 32, "text": "Thanks", "reply_to": "c" * 32})
        await settle(service)
        notify.assert_called_with("Recorded.", thread="c" * 32, message_id="reply:" + "d" * 32)
        service.public_key = "configured"
        assert (await client.post("/api/push/test", json={})).status == 200
        notify.assert_called_with("Hello there! This is the test reminder", grace=False)


async def test_push_waits_a_grace_period_and_skips_replies_seen_on_a_focused_device(companion):
    service, client = companion
    service.public_key = "configured"
    with patch.object(service, "_push", new_callable=AsyncMock) as push, \
            patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await client.post("/api/messages", json={"id": "d" * 32, "text": "Hi"})
        await settle(service)
        reply = service.db.execute("SELECT created FROM messages WHERE role='assistant' AND space=?", (SPACE,)).fetchone()
        assert service.push_tasks and not push.called
        assert (await client.post("/api/seen", json={"through": reply["created"]})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_not_called()

        # Seen through an older message only: the newer reply still notifies.
        await client.post("/api/messages", json={"id": "e" * 32, "text": "Again"})
        await settle(service)
        assert not push.called
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("Recorded.", "e" * 32, message_id="reply:" + "e" * 32)

        # A scheduled delivery waits out the same grace and notifies unless seen.
        push.reset_mock()
        await service.deliver("Your reminder")
        assert not push.called
        await asyncio.gather(*service.push_tasks)
        reminder = service.db.execute("SELECT id FROM messages WHERE text='Your reminder'").fetchone()["id"]
        push.assert_called_once_with("Your reminder", reminder, message_id=reminder)

        # The test button never waits and is never suppressed.
        push.reset_mock()
        await client.post("/api/seen", json={"through": 1e12})
        assert (await client.post("/api/push/test", json={})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("Hello there! This is the test reminder", None, message_id=None)


async def test_unread_count_follows_seen_marks_and_survives_restart(companion):
    service, client = companion
    first = service._insert(SPACE, "assistant", "One", "done")
    service._insert(SPACE, "assistant", "Two", "done")
    service._insert("telegram:555", "assistant", "A Telegram-era chat", "done")  # never counted
    assert (await (await client.get("/api/messages")).json())["unread"] == 2
    # A companion started over existing replies with no seen marks treats
    # them as read rather than badging the whole history.
    fresh = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    assert fresh.unread_count() == 0 and service.unread_count() == 0
    through = service.db.execute("SELECT created FROM messages WHERE id=?", (first,)).fetchone()["created"]
    assert (await client.post("/api/seen", json={"through": through})).status == 200
    assert service.unread_count() == 0  # the baseline mark is newer and stays
    service._insert(SPACE, "assistant", "Three", "done")
    service._insert("telegram:555", "assistant", "Still another chat", "done")
    assert (await (await client.get("/api/messages")).json())["unread"] == 1
    restarted = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    assert restarted.seen[SPACE] == fresh.seen[SPACE] and restarted.unread_count() == 1
    service.public_key = "configured"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/test",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send:
        await service._push("Three")
    assert json.loads(send.call_args.kwargs["data"])["unread"] == 1


async def test_seen_marker_only_advances_and_rejects_bad_input(companion):
    service, client = companion
    assert (await client.post("/api/seen", json={"through": 20.0})).status == 200
    assert (await client.post("/api/seen", json={"through": 10.0})).status == 200
    assert service.seen[SPACE] == 20.0
    for body in ({}, {"through": "soon"}, {"through": float("nan")}, {"through": True}, {"through": None}):
        assert (await client.post("/api/seen", json=body)).status == 400
    assert service.seen[SPACE] == 20.0


async def test_drain_waits_for_delayed_pushes(companion):
    service, client = companion
    service.public_key = "configured"
    with patch.object(service, "_push", new_callable=AsyncMock) as push, \
            patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await service.deliver("Your reminder")
        assert not push.called
        await service.drain()
        reminder = service.db.execute("SELECT id FROM messages WHERE text='Your reminder'").fetchone()["id"]
        push.assert_called_once_with("Your reminder", reminder, message_id=reminder)


def test_shell_revision_follows_every_shell_file_and_the_instance_name(tmp_path):
    from assistant.companion import _ASSETS, shell_revision

    for name in set(_ASSETS.values()):
        (tmp_path / name).write_bytes(b"v1 " + name.encode())
    base = shell_revision("Juniper", tmp_path)
    assert len(base) == 12 and base == shell_revision("Juniper", tmp_path)
    assert shell_revision("Cedar", tmp_path) != base
    for name in sorted(set(_ASSETS.values())):
        (tmp_path / name).write_bytes(b"v2 " + name.encode())
        changed = shell_revision("Juniper", tmp_path)
        assert changed != base, name
        base = changed


async def test_served_worker_carries_the_shell_revision(companion):
    from assistant.companion import shell_revision
    service, client = companion
    worker = await (await client.get("/sw.js")).text()
    assert f"noxide-shell-{shell_revision(service.cfg.agent_name)}" in worker
    assert "__INSTANCE_VERSION__" not in worker


async def test_pwa_assets_and_shutdown_rejection(companion):
    service, client = companion
    for path in ("/", "/app.js", "/theme.js", "/style.css", "/sw.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"):
        response = await client.get(path)
        assert response.status == 200
        assert await response.read()
    assert (await client.get("/icon-32.png")).status == 404
    await service.drain()
    assert (await client.post("/api/messages", json={"id": "a"*32, "text": "late"})).status == 503


async def test_cancellation_records_interruption_and_drain_waits(companion):
    service, client = companion
    started = asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service.agent.run.side_effect = run
    await client.post("/api/messages", json={"id": "a"*32, "text": "slow"})
    await started.wait()
    assert service.pending() == 1
    drain = asyncio.create_task(service.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    for task in service.tasks.values():
        task.cancel()
    await drain
    assert service.pending() == 0
    row = service.db.execute("SELECT * FROM messages WHERE role='user'").fetchone()
    assert row["status"] == "interrupted"
    assert "partially completed" in row["error"]


async def test_now_returns_unmodified_text_without_model_or_actions(companion):
    service, client = companion
    text = "# Now\n\n## Today\n- [ ] Keep **raw** text <script>alert(1)</script>\n\n## Last 7 days\n- Do not omit me\n"
    service.vault.write_file("wiki/now.md", text)
    assert (await (await client.get("/api/now")).json())["content"] == text
    service.agent.run.assert_not_called()


async def test_deliveries_are_thread_roots_that_push_their_thread(companion):
    service, client = companion
    with patch.object(service, "notify_push") as notify:
        await service.deliver("Time for your medication")
    rows = service.db.execute("SELECT id, space, role, text, status, thread, reply_to FROM messages").fetchall()
    assert [tuple(r)[1:] for r in rows] == [(SPACE, "assistant", "Time for your medication", "done", rows[0]["id"], None)]
    notify.assert_called_once_with("Time for your medication", thread=rows[0]["id"], message_id=rows[0]["id"])
    # The delivery reaches the model as an archived thread: a reply to it
    # continues the thread, a message on its own sees it as background.
    assert service.archive.recent_threads(SPACE, generation=0, since=0, limit=5)[0]["root_text"] == "Time for your medication"


async def test_real_agent_completes_rows_and_reset_keeps_them_searchable(companion):
    from assistant.agent import Agent

    from .test_agent import _make_text_response

    service, client = companion
    service.agent = Agent(service.vault, archive=service.archive)
    model = MagicMock(chat=AsyncMock(return_value=_make_text_response("Noxide reply")))
    with patch("assistant.copilot.get_client", return_value=model):
        assert (await client.post("/api/messages", json={"id": "a"*32, "text": "First input"})).status == 202
        await settle(service)
        assert (await client.post("/api/messages", json={"id": "b"*32, "text": "Second input"})).status == 202
        await settle(service)
        assert "Second input" in str(model.chat.call_args.args[0])
        page = await timeline(client)
        rows = flat(page)
        assert len(rows) == 4
        # Two roots are two threads; the agent's own reply row (reply:<id>) sits
        # in the thread of the message it answers.
        assert [(t["id"], [m["id"] for m in t["messages"]]) for t in page["threads"]] == [
            ("a" * 32, ["a" * 32, "reply:" + "a" * 32]), ("b" * 32, ["b" * 32, "reply:" + "b" * 32])]
        assert all(m["thread"] == t["id"] for t in page["threads"] for m in t["messages"])
        assert (await client.post("/api/reset", json={})).status == 200
        assert len(flat(await timeline(client))) == 4
        # A reset hides the old threads from new messages' background; a reply
        # to one still restores its thread, and the archive stays searchable.
        assert service.archive.recent_threads(SPACE, generation=1, since=0, limit=5) == []
        service.agent = Agent(service.vault, archive=service.archive)
        assert [m["content"] for m in service.agent._get_history("a" * 32).messages()][1:] == ["Noxide reply"]
        assert "First input" in service.agent._get_history("b" * 32).retrieve("search_history", {"query": "First"})


async def test_model_picker_lists_refreshes_and_switches(companion):
    service, client = companion
    assert (await client.get("/api/models")).status == 409
    assert (await client.post("/api/model", json={"alias": "opus"})).status == 409
    set_model = MagicMock()
    refresh = AsyncMock(return_value={"opus": ModelOption(id="claude-opus-5", label="Claude Opus 5")})
    service.models = ModelPicker({"sonnet": ModelOption(id="claude-sonnet-5", label="Claude Sonnet 5")}, "sonnet",
                                 set_model_fn=set_model, refresh_fn=refresh)
    listed = await (await client.get("/api/models")).json()
    refresh.assert_awaited_once()
    assert listed == {"current": "sonnet", "default": "sonnet", "models": [
        {"alias": "opus", "label": "Claude Opus 5", "id": "claude-opus-5"},
        {"alias": "sonnet", "label": "Claude Sonnet 5", "id": "claude-sonnet-5"}]}
    response = await client.post("/api/model", json={"alias": "opus"})
    assert response.status == 200 and await response.json() == {"ok": True, "current": "opus", "id": "claude-opus-5"}
    set_model.assert_called_once_with("claude-opus-5")
    assert (await client.post("/api/model", json={"alias": "gemini"})).status == 400
    assert (await client.post("/api/model", json={"alias": 3})).status == 400
    assert (await (await client.get("/api/models")).json())["current"] == "opus"


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com/path", "https://user@example.com", "https://example.com?x=1"])
def test_pwa_config_rejects_insecure_or_ambiguous_origins(tmp_path, origin):
    cfg = Config(state_dir=tmp_path, pwa_origin=origin)
    with pytest.raises(ConfigError, match="pwa.origin"):
        cfg.validate_for_run()


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


async def test_image_upload_is_sniffed_capped_stored_and_served(companion):
    service, client = companion
    response = await client.post("/api/attachments", data=PNG, headers={"Content-Type": "image/png"})
    assert response.status == 200
    path = (await response.json())["path"]
    assert re.fullmatch(r"attachments/\d{4}-\d{2}-\d{2}-[0-9a-f]{6}\.png", path)
    assert (service.cfg.vault_path / path).read_bytes() == PNG
    served = await client.get("/api/attachment", params={"path": path})
    assert served.status == 200 and served.content_type == "image/png" and await served.read() == PNG
    for bad in ("wiki/now.md", "attachments/../wiki/now.md", "attachments/missing.png", ""):
        assert (await client.get("/api/attachment", params={"path": bad})).status in (400, 404)
    # Bytes that are not the declared image, and undeclared types, are refused.
    assert (await client.post("/api/attachments", data=b"<svg onload=alert(1)>", headers={"Content-Type": "image/png"})).status == 400
    assert (await client.post("/api/attachments", data=b"<svg/>", headers={"Content-Type": "image/svg+xml"})).status == 400
    with patch("assistant.companion.UPLOAD_BYTES", 32):
        assert (await client.post("/api/attachments", data=PNG, headers={"Content-Type": "image/png"})).status == 413
    assert len(list((service.cfg.vault_path / "attachments").iterdir())) == 1


async def test_message_with_attachments_reaches_the_model_as_vision_input(companion):
    service, client = companion
    paths = [(await (await client.post("/api/attachments", data=PNG, headers={"Content-Type": "image/png"})).json())["path"]
             for _ in range(2)]
    assert (await client.post("/api/messages", json={"id": "f" * 32, "text": "", "attachments": ["wiki/now.md"]})).status == 400
    assert (await client.post("/api/messages", json={"id": "f" * 32, "text": "", "attachments": paths * 3})).status == 400
    assert (await client.post("/api/messages", json={"id": "f" * 32, "text": ""})).status == 400
    assert (await client.post("/api/messages", json={"id": "f" * 32, "text": "", "attachments": paths})).status == 202
    await settle(service)
    call = service.agent.run.call_args
    assert call.kwargs["image_data_urls"] == ["data:image/png;base64," + base64.b64encode(PNG).decode()] * 2
    assert "without a caption" in call.args[0]
    assert all(f"[attached image {n} of 2 — already stored in the vault at {path}" in call.args[0] for n, path in enumerate(paths, 1))
    rows = flat(await timeline(client))
    assert rows[0]["text"] == "" and json.loads(rows[0]["metadata"])["attachments"] == paths
    # A caption keeps its own text; the stored-path note follows it.
    service.agent.run.reset_mock()
    assert (await client.post("/api/messages", json={"id": "1" * 32, "text": "What plant?", "attachments": paths[:1]})).status == 202
    await settle(service)
    text = service.agent.run.call_args.args[0]
    assert text.startswith("What plant?\n\n[attached image — already stored in the vault at ")
    assert service.agent.run.call_args.kwargs["image_data_urls"] == ["data:image/png;base64," + base64.b64encode(PNG).decode()]


async def test_transcribe_endpoint_needs_a_transcriber_and_reports_failures(companion):
    from assistant.transcribe import TranscriptionError

    service, client = companion
    assert (await (await client.get("/api/session")).json())["voice"] is False
    assert (await client.post("/api/transcribe", data=b"audio", headers={"Content-Type": "audio/mp4"})).status == 409
    service.transcriber = MagicMock(transcribe=AsyncMock(return_value="hola mundo"))
    assert (await (await client.get("/api/session")).json())["voice"] is True
    response = await client.post("/api/transcribe", data=b"audio", headers={"Content-Type": "audio/mp4"})
    assert response.status == 200 and (await response.json())["text"] == "hola mundo"
    service.transcriber.transcribe.assert_awaited_once_with(b"audio")
    service.transcriber.transcribe.side_effect = TranscriptionError("credits are used up")
    response = await client.post("/api/transcribe", data=b"audio", headers={"Content-Type": "audio/mp4"})
    assert response.status == 502 and "credits" in (await response.json())["error"]
    assert (await client.post("/api/transcribe", data=b"", headers={"Content-Type": "audio/mp4"})).status == 400


async def test_restart_notices_are_opt_in_per_device_and_important_ones_reach_all(companion):
    service, client = companion
    service.public_key = "configured"
    keys = {"p256dh": "a" * 87, "auth": "b" * 22}
    phone = "https://fcm.googleapis.com/fcm/send/phone"
    desk = "https://updates.push.services.mozilla.com/wpush/v2/desk"
    assert (await client.post("/api/push", json={"endpoint": phone, "keys": keys})).status == 200
    reply = await client.post("/api/push", json={"endpoint": desk, "keys": keys, "lifecycle": True})
    assert reply.status == 200 and (await reply.json())["lifecycle"] is True
    assert (await client.post("/api/push", json={"endpoint": desk, "keys": keys, "lifecycle": "yes"})).status == 400
    # Re-registering without a word about restart notices keeps the choice.
    assert (await client.post("/api/push", json={"endpoint": desk, "keys": keys})).status == 200
    assert await (await client.get("/api/push", params={"endpoint": desk})).json() == {"registered": True, "lifecycle": True}
    assert await (await client.get("/api/push", params={"endpoint": phone})).json() == {"registered": True, "lifecycle": False}
    assert await (await client.get("/api/push", params={"endpoint": "https://fcm.googleapis.com/x"})).json() == {
        "registered": False, "lifecycle": False}
    with patch("pywebpush.webpush") as send:
        await service.notify_lifecycle("Restarting...")
        assert [call.kwargs["subscription_info"]["endpoint"] for call in send.call_args_list] == [desk]
        # No thread, no unread count: the worker keeps the badge and the reply tag alone.
        assert json.loads(send.call_args.kwargs["data"]) == {
            "body": "Restarting...", "agent_name": service.cfg.agent_name, "kind": "lifecycle"}
        send.reset_mock()
        await service.notify_lifecycle("Restart cut short — 2 message(s) in progress were interrupted", important=True)
        assert sorted(call.kwargs["subscription_info"]["endpoint"] for call in send.call_args_list) == sorted([phone, desk])
    # Nothing is archived: lifecycle text must not become assistant rows the model sees.
    assert service.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    service.public_key = ""
    assert service.notify_lifecycle("Started") is None


async def test_startup_message_names_an_app_update_once(companion):
    from assistant.companion import STARTED, STARTED_WITH_UPDATE, shell_revision

    service, _ = companion
    marker = service.cfg.state_dir / "shell_revision"
    # A first start establishes the baseline rather than announcing an update.
    assert service.startup_message() == STARTED
    assert marker.read_text().strip() == shell_revision(service.cfg.agent_name)
    assert service.startup_message() == STARTED
    marker.write_text("000000000000\n")
    assert service.startup_message() == STARTED_WITH_UPDATE
    assert marker.read_text().strip() == shell_revision(service.cfg.agent_name)
    assert service.startup_message() == STARTED


PDF = b"%PDF-1.4\n" + b"\0" * 64


async def test_document_upload_is_sniffed_stored_served_and_named_for_the_model(companion):
    service, client = companion
    response = await client.post("/api/attachments", data=PDF, headers={"Content-Type": "application/pdf"})
    assert response.status == 200
    pdf = (await response.json())["path"]
    assert re.fullmatch(r"attachments/\d{4}-\d{2}-\d{2}-[0-9a-f]{6}\.pdf", pdf)
    note = (await (await client.post("/api/attachments", data=b"Hola\n", headers={"Content-Type": "text/markdown"})).json())["path"]
    assert note.endswith(".md")
    served = await client.get("/api/attachment", params={"path": pdf})
    assert served.status == 200 and served.content_type == "application/pdf" and await served.read() == PDF
    # A PDF must start like one; text must be UTF-8 without NUL bytes; other types are refused.
    assert (await client.post("/api/attachments", data=b"<html>", headers={"Content-Type": "application/pdf"})).status == 400
    assert (await client.post("/api/attachments", data=b"a\0b", headers={"Content-Type": "text/plain"})).status == 400
    assert (await client.post("/api/attachments", data=b"\xff\xfe", headers={"Content-Type": "text/plain"})).status == 400
    assert (await client.post("/api/attachments", data=b"x", headers={"Content-Type": "video/mp4"})).status == 400
    # Original names are optional, sanitized, and must belong to the message's attachments.
    assert (await client.post("/api/messages", json={"id": "2" * 32, "text": "", "attachments": [pdf], "names": {"wiki/now.md": "x"}})).status == 400
    assert (await client.post("/api/messages", json={"id": "2" * 32, "text": "", "attachments": [pdf], "names": [pdf]})).status == 400
    names = {pdf: "../etc/Invoice 2026\x00.pdf", note: ""}
    assert (await client.post("/api/messages", json={"id": "2" * 32, "text": "File this", "attachments": [pdf, note], "names": names})).status == 202
    await settle(service)
    call = service.agent.run.call_args
    assert call.kwargs["image_data_urls"] is None
    assert call.args[0].startswith("File this\n\n")
    assert f"[attached file 1 of 2: Invoice 2026.pdf (application/pdf) — already stored in the vault at {pdf}" in call.args[0]
    assert f"[attached file 2 of 2: {note.rsplit('/', 1)[1]} (text/markdown) — already stored in the vault at {note}" in call.args[0]
    assert "extract_attachment" in call.args[0]
    rows = flat(await timeline(client))
    assert json.loads(rows[0]["metadata"]) == {"attachments": [pdf, note], "names": {pdf: "Invoice 2026.pdf"}}
    # Images and documents mix in one message; without a caption the note says so.
    png = (await (await client.post("/api/attachments", data=PNG, headers={"Content-Type": "image/png"})).json())["path"]
    assert (await client.post("/api/messages", json={"id": "3" * 32, "text": "", "attachments": [png, pdf]})).status == 202
    await settle(service)
    call = service.agent.run.call_args
    assert call.args[0].startswith("The user sent this without a caption.\n\n")
    assert "[attached image — already stored" in call.args[0] and f"[attached file: {pdf.rsplit('/', 1)[1]} (application/pdf)" in call.args[0]
    assert len(call.kwargs["image_data_urls"]) == 1


async def test_push_outcome_is_recorded_on_the_archived_row(companion):
    from pywebpush import WebPushException

    service, client = companion
    service.public_key = "configured"
    keys = {"p256dh": "a" * 87, "auth": "b" * 22}
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/phone", "keys": keys})
    await client.post("/api/push", json={"endpoint": "https://web.push.apple.com/desk", "keys": keys})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await service.deliver("Take the pills")
        await asyncio.gather(*service.push_tasks)
        reminder = service.db.execute("SELECT * FROM messages WHERE text='Take the pills'").fetchone()
        outcome = json.loads(reminder["metadata"])["push"]
        assert (outcome["devices"], outcome["accepted"]) == (2, 2) and outcome["at"] > 0
        # A provider refusal is not delivery; the row says so and the timeline can show it.
        send.side_effect = WebPushException("boom", response=MagicMock(status_code=500))
        await client.post("/api/messages", json={"id": "a" * 32, "text": "Hi"})
        await settle(service)
        await asyncio.gather(*service.push_tasks)
        reply = json.loads(service.archive.get("reply:" + "a" * 32)["metadata"])["push"]
        assert (reply["devices"], reply["accepted"]) == (2, 0)
        # No registered device is recorded too, so the timeline can say why nothing arrived.
        send.side_effect = None
        service.db.execute("DELETE FROM subscriptions")
        service.db.commit()
        await service.deliver("Nobody home")
        await asyncio.gather(*service.push_tasks)
        outcome = json.loads(service.db.execute("SELECT metadata FROM messages WHERE text='Nobody home'").fetchone()["metadata"])["push"]
        assert (outcome["devices"], outcome["accepted"]) == (0, 0)
        # A reply already displayed on a focused device records that instead of a push.
        await client.post("/api/messages", json={"id": "b" * 32, "text": "Again"})
        await settle(service)
        await client.post("/api/seen", json={"through": 1e12})
        await asyncio.gather(*service.push_tasks)
        assert json.loads(service.archive.get("reply:" + "b" * 32)["metadata"])["push"] == {"displayed": True}
    assert "push" in json.loads(flat(await timeline(client))[0]["metadata"])


async def test_unseen_reminders_are_pushed_again_once(companion):
    from assistant.companion import NUDGE_AFTER_SECONDS

    service, client = companion
    service.public_key = "configured"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/phone",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0):
        await service.deliver("Take the pills")
        await client.post("/api/messages", json={"id": "a" * 32, "text": "Hi"})
        await settle(service)
        await asyncio.gather(*service.push_tasks)
        assert send.call_count == 2
        await service.nudge_unseen()
        assert send.call_count == 2  # too recent
        old = time.time() - NUDGE_AFTER_SECONDS - 1
        service.db.execute("UPDATE messages SET created=?", (old,))
        service.db.commit()
        for row in service.db.execute("SELECT id, metadata FROM messages").fetchall():
            push = json.loads(row["metadata"]).get("push")
            if push:  # the nudge counts from the push, not the row
                service.archive.merge_metadata(row["id"], {"push": {**push, "at": old}})
        await service.nudge_unseen()
        # Only the reminder (a thread the assistant started) is nudged, and only once.
        assert send.call_count == 3 and json.loads(send.call_args.kwargs["data"])["body"] == "Take the pills"
        await service.nudge_unseen()
        assert send.call_count == 3
        reminder = service.db.execute("SELECT metadata FROM messages WHERE text='Take the pills'").fetchone()
        assert json.loads(reminder["metadata"])["push"]["nudged"] is True
        # A reminder displayed in the meantime is left alone.
        await service.deliver("Another")
        await asyncio.gather(*service.push_tasks)
        service.db.execute("UPDATE messages SET created=? WHERE text='Another'", (old,))
        service.db.commit()
        await client.post("/api/seen", json={"through": time.time()})
        await service.nudge_unseen()
        assert send.call_count == 4


async def wait_for(condition, attempts=100):
    for _ in range(attempts):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


async def test_outage_failed_messages_replay_by_themselves(companion):
    from assistant.companion import AUTO_RETRY_PREFIX

    service, client = companion
    service.agent.run.side_effect = CopilotUnavailableError("offline")
    service.agent.resume.side_effect = CopilotUnavailableError("still offline")
    await client.post("/api/messages", json={"id": "a" * 32, "text": "Remember this"})
    await settle(service)
    row = flat(await timeline(client))[0]
    assert row["status"] == "unavailable" and "retried" in row["error"]
    assert service.outage.is_set()
    with patch("assistant.companion.BACKOFF_INITIAL", 0.01):
        loop = asyncio.create_task(service.replay_outages())
        try:
            # Resumed in place while the outage lasts, backing off between attempts.
            assert await wait_for(lambda: service.agent.resume.await_count >= 2)
            assert flat(await timeline(client))[0]["status"] == "unavailable"
            service.agent.resume.side_effect = service.agent._resume
            assert await wait_for(lambda: service.archive.get("a" * 32)["status"] == "done")
            assert [(m["role"], m["text"], m["status"]) for m in flat(await timeline(client))] == [
                ("user", "Remember this", "done"), ("assistant", "Recovered.", "done")]
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
    # After a restart the failed turn is gone from memory: the message reruns
    # with a warning, oldest first; interrupted work still waits for a hand retry.
    service.agent.run.side_effect = CopilotUnavailableError("offline")
    await client.post("/api/messages", json={"id": "b" * 32, "text": "And this"})
    await settle(service)
    service._insert(SPACE, "user", "Half done", "interrupted", message_id="c" * 32)
    service.agent.run.side_effect = service.agent._run
    restarted = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    try:
        assert restarted.outage.is_set()
        with patch("assistant.companion.BACKOFF_INITIAL", 0.01):
            loop = asyncio.create_task(restarted.replay_outages())
            assert await wait_for(lambda: service.archive.get("b" * 32)["status"] == "done")
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        assert service.agent.run.call_args.args[0] == AUTO_RETRY_PREFIX + "And this"
        assert service.archive.get("c" * 32)["status"] == "interrupted"
    finally:
        await restarted.close()


def _madrid(hh, mm=0, day=23):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("Europe/Madrid"))


async def test_delivery_inside_quiet_hours_is_held_until_the_window_ends(companion):
    """A reminder at 03:01 is archived at once but its push waits for 07:30; the
    hold lives on the row, and the release goes through the seen check."""
    service, client = companion
    service.public_key = "configured"
    service.cfg.timezone, service.cfg.pwa_quiet_hours = "Europe/Madrid", "23:00-07:30"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/phone",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0), \
            patch("assistant.companion._local_now", return_value=_madrid(3, 1)):
        await service.deliver("Compilación hecha. Una tarea venció ayer.")
        await asyncio.gather(*service.push_tasks)
        assert send.call_count == 0
        row = service.db.execute("SELECT * FROM messages WHERE text LIKE 'Compilación%'").fetchone()
        assert json.loads(row["metadata"])["push"] == {"held_until": _madrid(7, 30).timestamp()}
        await service.release_held()  # still night: nothing moves
        assert send.call_count == 0
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0), \
            patch("assistant.companion._local_now", return_value=_madrid(7, 31)):
        await service.release_held()
        assert send.call_count == 1 and json.loads(send.call_args.kwargs["data"])["thread"] == row["id"]
        push = json.loads(service.archive.get(row["id"])["metadata"])["push"]
        assert push["accepted"] == 1 and "held_until" not in push
        await service.release_held()
        assert send.call_count == 1, "released once"


async def test_a_held_reminder_seen_in_the_app_is_not_pushed(companion):
    service, client = companion
    service.public_key = "configured"
    service.cfg.timezone, service.cfg.pwa_quiet_hours = "Europe/Madrid", "23:00-07:30"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/phone",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0), \
            patch("assistant.companion._local_now", return_value=_madrid(3, 1)):
        await service.deliver("Held")
        await client.post("/api/seen", json={"through": time.time()})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0), \
            patch("assistant.companion._local_now", return_value=_madrid(8, 0)):
        await service.release_held()
        assert send.call_count == 0
        row = service.db.execute("SELECT metadata FROM messages WHERE text='Held'").fetchone()
        assert json.loads(row["metadata"])["push"] == {"displayed": True}


async def test_no_nudge_inside_quiet_hours_and_nudges_count_from_the_push(companion):
    from assistant.companion import NUDGE_AFTER_SECONDS

    service, client = companion
    service.public_key = "configured"
    service.cfg.timezone, service.cfg.pwa_quiet_hours = "Europe/Madrid", "23:00-07:30"
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/phone",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send, patch("assistant.companion.PUSH_GRACE_SECONDS", 0), \
            patch("assistant.companion._local_now", return_value=_madrid(12, 0)):
        await service.deliver("Take the pills")
        await asyncio.gather(*service.push_tasks)
        assert send.call_count == 1
        old = time.time() - NUDGE_AFTER_SECONDS - 1
        service.db.execute("UPDATE messages SET created=?", (old,))
        service.db.commit()
        await service.nudge_unseen()
        assert send.call_count == 1, "the push was a moment ago, whatever the row's age"
        service.archive.merge_metadata(
            service.db.execute("SELECT id FROM messages").fetchone()["id"],
            {"push": {"at": old, "devices": 1, "accepted": 1}})
    with patch("pywebpush.webpush") as send, patch("assistant.companion._local_now", return_value=_madrid(23, 30)):
        await service.nudge_unseen()
        assert send.call_count == 0, "no nudges at night"
    with patch("pywebpush.webpush") as send, patch("assistant.companion._local_now", return_value=_madrid(8, 0)):
        await service.nudge_unseen()
        assert send.call_count == 1
