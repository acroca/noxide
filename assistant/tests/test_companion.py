"""The web companion's trust boundary and accepted-message lifecycle."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant.companion import WEB_CHAT_ID, Companion
from assistant.config import Config, ConfigError
from assistant.copilot import CopilotUnavailableError
from assistant.schedule import Scheduler
from assistant.tools import VaultTools


@pytest.fixture
async def companion(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    vault = VaultTools(tmp_path / "vault")
    agent = MagicMock(run=AsyncMock(return_value="Recorded."), retry_message=AsyncMock(return_value="Recovered."))
    scheduler = Scheduler(vault, AsyncMock())
    cfg = Config(state_dir=state, vault_path=tmp_path / "vault", pwa_enabled=True)
    service = Companion(cfg, agent, vault, scheduler)
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


async def test_no_password_required_but_csrf_and_host_checks_remain(companion):
    service, client = companion
    assert (await client.get("/")).status == 200
    for path in ("/api/session", "/api/topics", "/api/now", "/api/overview", "/api/messages"):
        response = await client.get(path)
        assert response.status == 200
        assert "Set-Cookie" not in response.headers
    assert (await client.post("/api/clear", json={"space": "general"}, headers={"Origin": "https://evil.test"})).status == 403
    assert (await client.post("/api/clear", json={"space": "general"}, headers={"X-Noxide": ""})).status == 403
    assert (await client.get("/api/overview", headers={"Host": "evil.test"})).status == 403
    response = await client.get("/api/overview")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert (await client.post("/api/clear", json={"space": "general"})).status == 200


async def test_auth_routes_and_stored_sessions_removed(companion):
    service, client = companion
    assert (await client.post("/api/login", json={})).status == 404
    assert (await client.post("/api/logout", json={})).status == 404
    assert service.db.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None
    service.db.execute("CREATE TABLE sessions(token TEXT PRIMARY KEY, expires REAL)")
    service.db.execute("INSERT INTO sessions VALUES ('old-session-hash', 9999999999)")
    service.db.commit()
    upgraded = Companion(service.cfg, service.agent, service.vault, service.scheduler, archive=service.archive)
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


async def test_overview_reads_vault_and_schedules_without_llm(companion):
    service, client = companion
    service.vault.write_file("wiki/now.md", "# Now\n## Today\n- [ ] Pay invoice")
    service.vault.write_file("wiki/projects/garden.md", "# The garden\n\n**Status:** Growing nicely.\n- [ ] Water plants")
    service.scheduler.schedule("2099-01-01T09:00:00", "Water the plants", False)
    data = await (await client.get("/api/overview")).json()
    assert "Pay invoice" in data["now"]
    assert data["projects"][0]["title"] == "The garden"
    assert data["projects"][0]["tasks"] == 1
    assert data["reminders"][0]["prompt"] == "Water the plants"
    service.agent.run.assert_not_called()
    for path in ("../secret", "system/schedule.md", "/etc/passwd", "wiki/projects/../../secret.md"):
        assert (await client.get("/api/page", params={"path": path})).status == 400
    data = await (await client.get("/api/page", params={"path": "wiki/projects/garden.md"})).json()
    assert "Growing nicely" in data["content"]


async def test_submit_idempotency_pending_guard_and_project_context(companion):
    service, client = companion
    service.vault.write_file("wiki/projects/garden.md", "# Garden")
    release = asyncio.Event()

    async def run(*args, **kwargs):
        await release.wait()
        await kwargs["send_message_fn"]("An intermediate update")
        return "Finished"

    service.agent.run.side_effect = run
    data = {"id": "a" * 32, "space": "wiki/projects/garden.md", "text": "Watered the plants"}
    try:
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json=data)).status == 202
        assert (await client.post("/api/messages", json={**data, "text": "different"})).status == 409
        assert (await client.post("/api/messages", json={**data, "id": "b" * 32})).status == 409
        assert (await client.post("/api/clear", json={"space": data["space"]})).status == 409
    finally:
        release.set()
    await settle(service)
    service.agent.run.assert_awaited_once()
    call = service.agent.run.call_args
    assert call.args[0] == WEB_CHAT_ID
    assert "wiki/projects/garden.md" in call.args[1]
    assert call.kwargs["thread_id"] == service._space(data["space"])
    rows = (await (await client.get("/api/messages", params={"space": data["space"]})).json())["messages"]
    assert [r["text"] for r in rows] == [data["text"], "An intermediate update", "Finished"]
    assert all(r["status"] == "done" for r in rows)
    assert (await (await client.get("/api/messages")).json())["messages"] == []


async def test_hot_outage_retry_preserves_sender_and_no_automatic_duplicate(companion):
    service, client = companion
    service.agent.run.side_effect = CopilotUnavailableError("offline")
    data = {"id": "a" * 32, "space": "general", "text": "Remember this"}
    await client.post("/api/messages", json=data)
    await settle(service)
    rows = (await (await client.get("/api/messages")).json())["messages"]
    assert rows[0]["status"] == "unavailable"
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 200
    await settle(service)
    service.agent.retry_message.assert_awaited_once()
    assert service.agent.retry_message.call_args.kwargs["hot"] is True
    assert callable(service.agent.retry_message.call_args.kwargs["send_message_fn"])
    assert (await client.post("/api/retry", json={"id": data["id"]})).status == 409


async def test_restart_marks_interrupted_work_without_replaying(companion):
    service, client = companion
    service._insert("general", "user", "Potential side effect", "running", message_id="a" * 32)
    other = Companion(service.cfg, service.agent, service.vault, service.scheduler)
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


async def test_clear_only_one_web_space_and_mirrored_delivery(companion):
    service, client = companion
    service.vault.write_file("wiki/projects/one.md", "# One")
    service._insert("wiki/projects/one.md", "user", "private thread", "done")
    await service.observe_delivery("A scheduled reminder")
    service.agent._queue_sent_note.assert_called_once_with(WEB_CHAT_ID, None, "A scheduled reminder")
    await client.post("/api/clear", json={"space": "general"})
    assert service.db.execute("SELECT count(*) FROM messages WHERE status!='deleted'").fetchone()[0] == 1
    service.agent.clear_history.assert_called_once_with(WEB_CHAT_ID, None)


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
        await service._push("general", "Hello there! This is the test reminder")
        assert json.loads(send.call_args.kwargs["data"]) == {
            "space": "general", "body": "Hello there! This is the test reminder", "agent_name": service.cfg.agent_name, "channel_name": "General"}
        assert send.call_args.kwargs["requests_session"].max_redirects == 0
    assert (await client.delete("/api/push", json={"endpoint": endpoint})).status == 200
    assert service.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0
    service.cfg.pwa_push_contact = "mailto:test@example.com"
    other = Companion(service.cfg, service.agent, service.vault, service.scheduler)
    key = other.public_key
    assert len(key) == 87
    await other.close()
    other = Companion(service.cfg, service.agent, service.vault, service.scheduler)
    assert other.public_key == key
    await other.close()


async def test_push_preview_is_bounded_and_preserves_unicode(companion):
    service, client = companion
    service.public_key = "configured"
    service.vault.write_file("system/topics/index.md", "| 10 | health | Health |")
    await client.post("/api/push", json={"endpoint": "https://fcm.googleapis.com/fcm/send/test",
                                       "keys": {"p256dh": "a" * 87, "auth": "b" * 22}})
    with patch("pywebpush.webpush") as send:
        await service._push("topic:10", "\U0001f331" * 2000)
    payload = send.call_args.kwargs["data"]
    assert len(payload.encode("utf-8")) < 3000
    assert json.loads(payload) == {"space": "topic:10", "body": "\U0001f331" * 500 + "...", "agent_name": service.cfg.agent_name, "channel_name": "Health"}


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
    assert worker.splitlines()[1] != renamed_worker.splitlines()[1]


async def test_push_triggers_include_reply_reminder_and_test_text(companion):
    service, client = companion
    with patch.object(service, "notify_push") as notify:
        await service.observe_delivery("Your reminder")
        notify.assert_called_with("general", "Your reminder")
        await client.post("/api/messages", json={"id": "c" * 32, "space": "general", "text": "Hi"})
        await settle(service)
        notify.assert_called_with("general", "Recorded.")
        service.public_key = "configured"
        assert (await client.post("/api/push/test", json={})).status == 200
        notify.assert_called_with("general", "Hello there! This is the test reminder", grace=False)


async def test_push_waits_a_grace_period_and_skips_replies_seen_on_a_focused_device(companion):
    service, client = companion
    service.public_key = "configured"
    with patch.object(service, "_push", new_callable=AsyncMock) as push, \
            patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await client.post("/api/messages", json={"id": "d" * 32, "space": "general", "text": "Hi"})
        await settle(service)
        reply = service.db.execute("SELECT created FROM messages WHERE role='assistant' AND space='general'").fetchone()
        assert service.push_tasks and not push.called
        assert (await client.post("/api/seen", json={"space": "general", "through": reply["created"]})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_not_called()

        # Seen through an older message only: the newer reply still notifies.
        await client.post("/api/messages", json={"id": "e" * 32, "space": "general", "text": "Again"})
        await settle(service)
        assert not push.called
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("general", "Recorded.")

        # A device on another topic acknowledges nothing for this one.
        push.reset_mock()
        await service.observe_delivery("Your reminder")
        assert (await client.post("/api/seen", json={"space": "topic:7", "through": 1e12})).status == 404
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("general", "Your reminder")

        # The test button never waits and is never suppressed.
        push.reset_mock()
        await client.post("/api/seen", json={"space": "general", "through": 1e12})
        assert (await client.post("/api/push/test", json={})).status == 200
        await asyncio.gather(*service.push_tasks)
        push.assert_called_once_with("general", "Hello there! This is the test reminder")


async def test_seen_marker_only_advances_and_rejects_bad_input(companion):
    service, client = companion
    assert (await client.post("/api/seen", json={"space": "general", "through": 20.0})).status == 200
    assert (await client.post("/api/seen", json={"space": "general", "through": 10.0})).status == 200
    assert service.seen["general"] == 20.0
    for body in ({"space": "general"}, {"space": "general", "through": "soon"},
                 {"space": "general", "through": float("nan")}, {"space": "wiki/../x.md", "through": 1.0}):
        assert (await client.post("/api/seen", json=body)).status in (400, 404)


async def test_drain_waits_for_delayed_pushes(companion):
    service, client = companion
    service.public_key = "configured"
    with patch.object(service, "_push", new_callable=AsyncMock) as push, \
            patch("assistant.companion.PUSH_GRACE_SECONDS", 0.05):
        await service.observe_delivery("Your reminder")
        assert not push.called
        await service.drain()
        push.assert_called_once_with("general", "Your reminder")


async def test_pwa_assets_and_shutdown_rejection(companion):
    service, client = companion
    for path in ("/", "/app.js", "/style.css", "/sw.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"):
        response = await client.get(path)
        assert response.status == 200
        assert await response.read()
    assert (await client.get("/icon-32.png")).status == 404
    await service.drain()
    assert (await client.post("/api/messages", json={"id": "a"*32, "space": "general", "text": "late"})).status == 503


async def test_cancellation_records_interruption_and_drain_waits(companion):
    service, client = companion
    started = asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service.agent.run.side_effect = run
    await client.post("/api/messages", json={"id": "a"*32, "space": "general", "text": "slow"})
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


async def test_web_spaces_cannot_follow_vault_symlinks(companion):
    service, client = companion
    service.vault.write_file("wiki/projects/one.md", "# One")
    service.vault.write_file("system/private.md", "private")
    (service.cfg.vault_path / "wiki/projects/alias.md").symlink_to(service.cfg.vault_path / "system/private.md")
    response = await client.get("/api/page", params={"path": "wiki/projects/alias.md"})
    assert response.status == 400
    data = await (await client.get("/api/overview")).json()
    assert [p["path"] for p in data["projects"]] == ["wiki/projects/one.md"]


async def test_topics_use_telegram_index_not_wiki_projects(companion):
    service, client = companion
    service.vault.write_file("system/topics/index.md", r"""# Topics
| topic_id | slug | name |
|---|---|---|
| 10 | work | Work \| Projects |
| 20 | health | Health |
| 10 | duplicate | Duplicate |
| 0 | invalid | Invalid |
| 30 | ../../escape | Invalid |
""")
    service.vault.write_file("wiki/projects/garden.md", "# Garden")
    data = await (await client.get("/api/topics")).json()
    assert data["topics"] == [{"id": "general", "name": "General"},
                              {"id": "topic:10", "name": "Work | Projects"},
                              {"id": "topic:20", "name": "Health"}]
    assert service._space("topic:10") == 10
    for invalid in ("topic:010", "topic:99", "topic:-10"):
        assert (await client.get("/api/messages", params={"space": invalid})).status in (400, 404)
    service.agent.run.assert_not_called()


async def test_topic_run_uses_topic_prompt_and_isolated_web_history(companion):
    from assistant.agent import Agent

    from .test_agent import _make_text_response

    service, client = companion
    service.vault.write_file("system/topics/index.md", "| 10 | health | Health |")
    service.vault.write_file("system/topics/health/AGENTS.md", "Unique health instructions")
    service.agent = Agent(service.vault)
    model = MagicMock(chat=AsyncMock(return_value=_make_text_response("Noted")))
    with patch("assistant.copilot.get_client", return_value=model):
        await client.post("/api/messages", json={"id": "a"*32, "space": "topic:10", "text": "Health update"})
        await settle(service)
        assert "Unique health instructions" in model.chat.call_args.args[0][0]["content"]
        await client.post("/api/messages", json={"id": "b"*32, "space": "general", "text": "General update"})
        await settle(service)
        assert "Unique health instructions" not in model.chat.call_args.args[0][0]["content"]
        assert "Health update" not in str(model.chat.call_args.args[0])
    assert (WEB_CHAT_ID, 10) in service.agent._histories
    assert (WEB_CHAT_ID, None) in service.agent._histories


async def test_now_returns_unmodified_text_without_model_or_actions(companion):
    service, client = companion
    text = "# Now\n\n## Today\n- [ ] Keep **raw** text <script>alert(1)</script>\n\n## Last 7 days\n- Do not omit me\n"
    service.vault.write_file("wiki/now.md", text)
    assert (await (await client.get("/api/now")).json())["content"] == text
    service.agent.run.assert_not_called()


async def test_saved_legacy_chats_remain_available_without_new_project_navigation(companion):
    service, client = companion
    service.vault.write_file("wiki/projects/garden.md", "# Garden")
    service._insert("wiki/projects/garden.md", "user", "Earlier web chat", "done")
    topics = (await (await client.get("/api/topics")).json())["topics"]
    assert topics[-1] == {"id": "wiki/projects/garden.md", "name": "garden", "legacy": True}


async def test_topic_deliveries_mirror_to_matching_web_channel(companion):
    service, client = companion
    service.vault.write_file("system/topics/index.md", "| 10 | health | Health |")
    await service.observe_delivery("Time for your medication", 10)
    service.agent._queue_sent_note.assert_called_once_with(WEB_CHAT_ID, 10, "Time for your medication")
    assert service.db.execute("SELECT space FROM messages").fetchone()["space"] == "topic:10"
    await service.observe_delivery("Unknown topic reminder", 999)
    assert service.db.execute("SELECT space FROM messages ORDER BY created DESC").fetchone()["space"] == "general"


async def test_shared_timeline_reset_delete_and_restart_context(companion):
    from assistant.agent import Agent

    from .test_agent import _make_text_response

    service, client = companion
    service.agent = Agent(service.vault, archive=service.archive, home_chat_fn=lambda: 123)
    model = MagicMock(chat=AsyncMock(return_value=_make_text_response("Noxide reply")))
    with patch("assistant.copilot.get_client", return_value=model):
        await service.agent.run(123, "Telegram input")
        assert (await client.post("/api/messages", json={"id": "a"*32, "space": "general", "text": "Web input"})).status == 202
        await settle(service)
        assert "Telegram input" in str(model.chat.call_args.args[0])
        rows = (await (await client.get("/api/messages")).json())["messages"]
        assert [r["source"] for r in rows] == ["telegram", "telegram", "web", "web"]
        assert len(rows) == 4
        assert (await client.post("/api/reset", json={"space": "general"})).status == 200
        assert len((await (await client.get("/api/messages")).json())["messages"]) == 4
        service.agent = Agent(service.vault, archive=service.archive, home_chat_fn=lambda: 123)
        assert service.agent._get_history(123).messages() == []
        assert "Telegram input" in service.agent._get_history(123).retrieve("get_history", {})
        assert (await client.post("/api/clear", json={"space": "general"})).status == 200
        assert (await (await client.get("/api/messages")).json())["messages"] == []
        assert "Telegram input" not in service.agent._get_history(123).retrieve("get_history", {})


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com/path", "https://user@example.com", "https://example.com?x=1"])
def test_pwa_config_rejects_insecure_or_ambiguous_origins(tmp_path, origin):
    cfg = Config(state_dir=tmp_path, pwa_enabled=True, pwa_origin=origin)
    with pytest.raises(ConfigError, match="pwa.origin"):
        cfg.validate_for_run()
