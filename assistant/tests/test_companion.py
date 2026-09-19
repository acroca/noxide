"""The web companion's trust boundary and accepted-message lifecycle."""

import asyncio
import base64
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant.companion import WEB_CHAT_ID, Companion
from assistant.config import Config, ConfigError
from assistant.copilot import CopilotUnavailableError
from assistant.tools import VaultTools


@pytest.fixture
async def companion(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    vault = VaultTools(tmp_path / "vault")
    agent = MagicMock(run=AsyncMock(return_value="Recorded."), retry_message=AsyncMock(return_value="Recovered."))
    cfg = Config(state_dir=state, vault_path=tmp_path / "vault", pwa_enabled=True)
    service = Companion(cfg, agent, vault)
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


async def settle(service):
    await asyncio.gather(*list(service.tasks.values()))
    await asyncio.sleep(0)


def flat(page):
    """The messages of a thread page in display order: threads by last activity, each oldest first."""
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
    service.agent.reset_conversation = AsyncMock()
    assert (await client.post("/api/reset", json={})).status == 200
    service.agent.reset_conversation.assert_awaited_once_with(WEB_CHAT_ID)
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
    cfg = Config(state_dir=tmp_path, pwa_enabled=True, pwa_origin="https://mini.example.ts.net",
                 telegram_bot_token="test-token", allowed_user_ids=[123])
    cfg.validate_for_run()
    assert "pwa_password" not in Config.model_fields


async def test_submit_idempotency_and_queueing_in_the_home_chat(companion):
    from assistant.companion import _WEB_CONTEXT

    service, client = companion
    release = asyncio.Event()

    async def run(*args, **kwargs):
        await release.wait()
        await kwargs["send_message_fn"]("An intermediate update")
        return "Finished"

    service.agent.run.side_effect = run
    data = {"id": "a" * 32, "text": "Watered the plants"}
    try:
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json={**data, "text": "different"})).status == 409
        # A second message queues behind the first instead of being refused;
        # Agent.run's conversation lock answers them in order.
        assert (await client.post("/api/messages", json={**data, "id": "b" * 32, "text": "And fed them"})).status == 202
        page = await timeline(client)
        assert [(m["text"], m["status"]) for m in flat(page)] == [("Watered the plants", "queued"), ("And fed them", "queued")]
        # Two messages sent without replying are two threads, each rooted at its own id.
        assert [(t["id"], [m["thread"] for m in t["messages"]]) for t in page["threads"]] == [("a" * 32, ["a" * 32]), ("b" * 32, ["b" * 32])]
        assert (await client.post("/api/reset", json={})).status == 409
    finally:
        release.set()
    await settle(service)
    assert [c.args[1].split("]")[-1].strip() for c in service.agent.run.call_args_list] == ["Watered the plants", "And fed them"]
    # The mock agent has no conversation lock, so its runs interleave here;
    # ordering is Agent.run's job. Each message still gets its own replies.
    page = await timeline(client)
    assert all(m["status"] == "done" for m in flat(page))
    replies = [m["reply_to"] for m in flat(page) if m["role"] == "assistant"]
    assert sorted(replies) == sorted(["a" * 32] * 2 + ["b" * 32] * 2)
    # Replies land in the thread of the message they answer, not in a thread of their own.
    assert [(t["id"], len(t["messages"])) for t in page["threads"]] == [("a" * 32, 3), ("b" * 32, 3)]
    assert all(m["thread"] == t["id"] for t in page["threads"] for m in t["messages"])
    # Every web run is the home conversation: no Telegram thread id, one context
    # shared with the pinned Telegram chat.
    for call in service.agent.run.call_args_list:
        assert call.args[0] == WEB_CHAT_ID
        assert "thread_id" not in call.kwargs
        assert call.kwargs["extra_context"] == _WEB_CONTEXT and call.kwargs["source"] == "web"
    rows = flat(await timeline(client))
    assert sorted(r["text"] for r in rows) == sorted([data["text"], "And fed them"] + ["An intermediate update", "Finished"] * 2)
    assert {r["space"] for r in rows} == {"general"}


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
    # The parent must be a message of this chat: unknown, foreign-chat, deleted or malformed ids are refused.
    other = service._insert("telegram:555", "user", "Another chat", "done")
    deleted = service._insert("general", "user", "Gone", "deleted")
    for bad in ("f" * 32, other, deleted, 123, ""):
        response = await client.post("/api/messages", json={"id": "c" * 32, "text": "Orphan", "reply_to": bad})
        assert response.status == 400, bad
        assert "not in this chat" in (await response.json())["error"]
    assert service.archive.get("c" * 32) is None


async def test_hot_outage_retry_preserves_sender_and_no_automatic_duplicate(companion):
    service, client = companion
    service.agent.run.side_effect = CopilotUnavailableError("offline")
    data = {"id": "a" * 32, "text": "Remember this"}
    await client.post("/api/messages", json=data)
    await settle(service)
    rows = flat(await timeline(client))
    assert rows[0]["status"] == "unavailable"
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 200
    await settle(service)
    service.agent.retry_message.assert_awaited_once()
    call = service.agent.retry_message.call_args
    assert call.args[0] == WEB_CHAT_ID and call.args[1] == "Remember this"
    assert call.kwargs["hot"] is True and "thread_id" not in call.kwargs
    assert callable(call.kwargs["send_message_fn"])
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 409


async def test_restart_marks_interrupted_work_without_replaying(companion):
    service, client = companion
    service._insert("general", "user", "Potential side effect", "running", message_id="a" * 32)
    other = Companion(service.cfg, service.agent, service.vault)
    try:
        row = other.db.execute("SELECT * FROM messages").fetchone()
        assert row["status"] == "interrupted"
        assert "partially completed" in row["error"]
        assert other.tasks == {}
    finally:
        await other.close()
    assert (await client.post("/api/retry", json={"id": "a" * 32})).status == 200
    await settle(service)
    assert "Explicit retry after interruption" in service.agent.run.call_args.args[1]


async def test_messages_open_on_a_small_page_with_older_ones_behind_a_cursor(companion):
    service, client = companion
    for i in range(21):
        service._insert("general", "user", f"m{i}", "done")
    data = await timeline(client)
    assert [m["text"] for m in flat(data)] == [f"m{i}" for i in range(1, 21)]
    assert [t["started"] for t in data["threads"]] == [m["created"] for m in flat(data)]
    assert data["before"] == data["threads"][0]["started"]
    older = await timeline(client, before=data["before"])
    assert [m["text"] for m in flat(older)] == ["m0"] and older["before"] is None


async def test_paging_walks_threads_by_their_first_message(companion):
    service, client = companion
    roots = [service._insert("general", "user", f"m{i}", "done") for i in range(21)]
    # A late reply does not move its thread: the timeline is ordered by first
    # message, so the oldest thread stays on the older page, reply included.
    service._insert("general", "assistant", "Answering m0 late", "done", reply_to=roots[0])
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

    async def run(*args, **kwargs):
        await kwargs["on_research"]()
        await release.wait()
        return "Found it."

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
    service._insert("general", "user", "before", "done")
    await service.observe_delivery("A scheduled reminder")
    # Deliveries are thread roots now; nothing is queued into a pending-notes mirror.
    service.agent._queue_sent_note.assert_not_called()
    data = await timeline(client)
    assert data["generation"] == 0 and [m["generation"] for m in flat(data)] == [0, 0]
    service.agent.reset_conversation = AsyncMock(side_effect=lambda *a, **k: service.archive.reset("general"))
    assert (await client.post("/api/reset", json={})).status == 200
    service._insert("general", "user", "after", "done")
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
    other = Companion(service.cfg, service.agent, service.vault)
    key = other.public_key
    assert len(key) == 87
    await other.close()
    other = Companion(service.cfg, service.agent, service.vault)
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
        await service.observe_delivery("Your reminder")
        reminder = service.db.execute("SELECT id, thread FROM messages WHERE text='Your reminder'").fetchone()
        assert reminder["thread"] == reminder["id"]
        notify.assert_called_with("Your reminder", thread=reminder["id"])
        await client.post("/api/messages", json={"id": "c" * 32, "text": "Hi"})
        await settle(service)
        notify.assert_called_with("Recorded.", thread="c" * 32)
        # A reply in an existing thread pushes that thread, not its own message id.
        await client.post("/api/messages", json={"id": "d" * 32, "text": "Thanks", "reply_to": "c" * 32})
        await settle(service)
        notify.assert_called_with("Recorded.", thread="c" * 32)
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
        reply = service.db.execute("SELECT created FROM messages WHERE role='assistant' AND space='general'").fetchone()
        assert service.push_tasks and not push.called
        assert (await client.post("/api/seen", json={"through": reply["created"]})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_not_called()

        # Seen through an older message only: the newer reply still notifies.
        await client.post("/api/messages", json={"id": "e" * 32, "text": "Again"})
        await settle(service)
        assert not push.called
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("Recorded.", "e" * 32)

        # A scheduled delivery waits out the same grace and notifies unless seen.
        push.reset_mock()
        await service.observe_delivery("Your reminder")
        assert not push.called
        await asyncio.gather(*service.push_tasks)
        reminder = service.db.execute("SELECT id FROM messages WHERE text='Your reminder'").fetchone()["id"]
        push.assert_called_once_with("Your reminder", reminder)

        # The test button never waits and is never suppressed.
        push.reset_mock()
        await client.post("/api/seen", json={"through": 1e12})
        assert (await client.post("/api/push/test", json={})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("Hello there! This is the test reminder", None)


async def test_unread_count_follows_seen_marks_and_survives_restart(companion):
    service, client = companion
    first = service._insert("general", "assistant", "One", "done")
    service._insert("general", "assistant", "Two", "done")
    service._insert("telegram:555", "assistant", "Another Telegram chat", "done")  # not the home chat: never counted
    assert (await (await client.get("/api/messages")).json())["unread"] == 2
    # A companion started over existing replies with no seen marks treats
    # them as read rather than badging the whole history.
    fresh = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    assert fresh.unread_count() == 0 and service.unread_count() == 0
    through = service.db.execute("SELECT created FROM messages WHERE id=?", (first,)).fetchone()["created"]
    assert (await client.post("/api/seen", json={"through": through})).status == 200
    assert service.unread_count() == 0  # the baseline mark is newer and stays
    service._insert("general", "assistant", "Three", "done")
    service._insert("telegram:555", "assistant", "Still another chat", "done")
    assert (await (await client.get("/api/messages")).json())["unread"] == 1
    restarted = Companion(service.cfg, service.agent, service.vault, archive=service.archive)
    assert restarted.seen["general"] == fresh.seen["general"] and restarted.unread_count() == 1
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
    assert service.seen["general"] == 20.0
    for body in ({}, {"through": "soon"}, {"through": float("nan")}, {"through": True}, {"through": None}):
        assert (await client.post("/api/seen", json=body)).status == 400
    assert service.seen["general"] == 20.0


async def test_drain_waits_for_delayed_pushes(companion):
    service, client = companion
    service.public_key = "configured"
    with patch.object(service, "_push", new_callable=AsyncMock) as push, \
            patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await service.observe_delivery("Your reminder")
        assert not push.called
        await service.drain()
        reminder = service.db.execute("SELECT id FROM messages WHERE text='Your reminder'").fetchone()["id"]
        push.assert_called_once_with("Your reminder", reminder)


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
    drain = asyncio.create_task(service.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    for task in service.tasks.values():
        task.cancel()
    await drain
    row = service.db.execute("SELECT * FROM messages WHERE role='user'").fetchone()
    assert row["status"] == "interrupted"
    assert "partially completed" in row["error"]


async def test_now_returns_unmodified_text_without_model_or_actions(companion):
    service, client = companion
    text = "# Now\n\n## Today\n- [ ] Keep **raw** text <script>alert(1)</script>\n\n## Last 7 days\n- Do not omit me\n"
    service.vault.write_file("wiki/now.md", text)
    assert (await (await client.get("/api/now")).json())["content"] == text
    service.agent.run.assert_not_called()


async def test_deliveries_mirror_to_the_web_chat_unless_the_agent_shares_the_archive(companion):
    service, client = companion
    # An agent with its own archive (the fake here) needs the companion to
    # record the delivery as a thread root of its own and push that thread.
    with patch.object(service, "notify_push") as notify:
        await service.observe_delivery("Time for your medication")
    rows = service.db.execute("SELECT id, space, role, text, status, thread, reply_to FROM messages").fetchall()
    assert [tuple(r)[1:] for r in rows] == [("general", "assistant", "Time for your medication", "done", rows[0]["id"], None)]
    notify.assert_called_once_with("Time for your medication", thread=rows[0]["id"])
    # There is no pending-notes mirror any more: the delivery reaches the model
    # as an archived thread, through the ambient block and native replies.
    service.agent._queue_sent_note.assert_not_called()
    # An agent on the same archive already recorded it in the main path, which
    # passes the row id along: the companion only notifies, with that thread,
    # or the reminder would show twice.
    service.agent.archive = service.archive
    with patch.object(service, "notify_push") as notify:
        await service.observe_delivery("Second reminder", thread="root-from-main")
    notify.assert_called_once_with("Second reminder", thread="root-from-main")
    assert service.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    service.agent._queue_sent_note.assert_not_called()


async def test_shared_timeline_reset_delete_and_restart_context(companion):
    from assistant.agent import Agent

    from .test_agent import _make_text_response

    service, client = companion
    service.agent = Agent(service.vault, archive=service.archive, home_chat_fn=lambda: 123)
    model = MagicMock(chat=AsyncMock(return_value=_make_text_response("Noxide reply")))
    with patch("assistant.copilot.get_client", return_value=model):
        await service.agent.run(123, "Telegram input")
        assert (await client.post("/api/messages", json={"id": "a"*32, "text": "Web input"})).status == 202
        await settle(service)
        assert "Telegram input" in str(model.chat.call_args.args[0])
        page = await timeline(client)
        rows = flat(page)
        assert [r["source"] for r in rows] == ["telegram", "telegram", "web", "web"]
        assert len(rows) == 4
        # The Telegram exchange and the web exchange are two threads; the agent's
        # own reply row (reply:<id>) sits in the thread of the message it answers.
        assert [(t["id"], [m["id"] for m in t["messages"]]) for t in page["threads"]] == [
            (rows[0]["id"], [rows[0]["id"], f"reply:{rows[0]['id']}"]), ("a" * 32, ["a" * 32, "reply:" + "a" * 32])]
        assert all(m["thread"] == t["id"] for t in page["threads"] for m in t["messages"])
        assert (await client.post("/api/reset", json={})).status == 200
        assert len(flat(await timeline(client))) == 4
        service.agent = Agent(service.vault, archive=service.archive, home_chat_fn=lambda: 123)
        assert service.agent._get_history(123).messages() == []
        assert "Telegram input" in service.agent._get_history(123).retrieve("get_history", {})


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com/path", "https://user@example.com", "https://example.com?x=1"])
def test_pwa_config_rejects_insecure_or_ambiguous_origins(tmp_path, origin):
    cfg = Config(state_dir=tmp_path, pwa_enabled=True, pwa_origin=origin)
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
    assert "without a caption" in call.args[1]
    assert all(f"[attached image {n} of 2 — already stored in the vault at {path}" in call.args[1] for n, path in enumerate(paths, 1))
    rows = flat(await timeline(client))
    assert rows[0]["text"] == "" and json.loads(rows[0]["metadata"])["attachments"] == paths
    # A caption keeps its own text; the stored-path note follows it.
    service.agent.run.reset_mock()
    assert (await client.post("/api/messages", json={"id": "1" * 32, "text": "What plant?", "attachments": paths[:1]})).status == 202
    await settle(service)
    text = service.agent.run.call_args.args[1]
    assert text.startswith("What plant?\n\n[attached image — already stored in the vault at ")


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
        await service.notify_lifecycle("Restart cut short — 2 queued message(s) were dropped", important=True)
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
