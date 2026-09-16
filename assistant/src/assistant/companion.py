"""Private-network PWA companion, sharing the running agent and markdown vault.

SQLite is a delivery ledger, not a second knowledge base. Accepted messages
survive disconnects; interrupted work needs explicit retry to avoid replaying
side effects silently after a crash.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiohttp import web

from .agent import MAX_ITERATIONS_REPLY, Agent, _parse_topic_row
from .atomic import atomic_write_text
from .config import Config
from .conversations import WEB_CHAT_ID, ConversationArchive
from .copilot import CopilotUnavailableError
from .schedule import Scheduler
from .tools import VaultTools
from .transcribe import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)
# A reply displayed on a focused device within this window notifies no device.
# Longer than the client's poll interval, so the device already showing the
# conversation gets to acknowledge before phones buzz.
PUSH_GRACE_SECONDS = 5
# Newest messages a topic opens with; the same page feeds the 2.2s poll, so it
# is kept small. Earlier pages load behind the timeline's manual link.
MESSAGE_PAGE = 20
# Uploads match Telegram's 20 MB download cap; bodies are read from the
# stream in chunks, so the app-wide JSON body limit does not apply to them.
UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 4
# Declared type → (magic prefixes, stored extension). Sniffing keeps an
# SVG or HTML body from landing in the vault under an image name.
_IMAGE_TYPES = {
    "image/jpeg": ((b"\xff\xd8\xff",), "jpg"),
    "image/png": ((b"\x89PNG\r\n\x1a\n",), "png"),
    "image/webp": ((b"RIFF",), "webp"),
    "image/gif": ((b"GIF87a", b"GIF89a"), "gif"),
}
_ATTACHMENT_PATH = re.compile(r"attachments/\d{4}-\d{2}-\d{2}-[0-9a-f]{6}\.(jpg|png|webp|gif)")
_IMAGE_NOTE = "[attached image — already stored in the vault at {path}; link it from a note if it is worth keeping, otherwise leave it]"
_IMAGE_NOTE_N = "[attached image {n} of {total} — already stored in the vault at {path}; link it from a note if it is worth keeping, otherwise leave it]"
_NO_CAPTION = "The user sent this image without a caption."
_ASSETS = {"/": "index.html", "/app.js": "app.js", "/theme.js": "theme.js", "/style.css": "style.css",
           "/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest",
           "/icon.svg": "icon.svg"}


def shell_revision(agent_name, root=None):
    """The worker's cache version: a hash of the instance name and every shell file.

    Any change to a packaged asset yields a new version, so a device that
    installed the previous one is offered the update; a manual bump used to be
    required and was skipped once (2026-09-16). Renaming the instance refreshes
    the shell without changing app identity.
    """
    digest = hashlib.sha256(agent_name.encode())
    root = root if root is not None else files("assistant") / "pwa"
    for name in sorted(set(_ASSETS.values())):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()[:12]


_MIMES = {".html": "text/html", ".js": "application/javascript", ".css": "text/css",
          ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}
_WEB_CONTEXT = (
    "This conversation is in the Noxide web companion, not Telegram. Reply directly here. "
    "Web channels use the existing Telegram topic index and topic instructions, but have "
    "shared archived conversation history. Durable knowledge still belongs in the vault. "
    "Recent completed exchanges are restored after restart; older messages are available via history tools."
)


class Companion:
    def __init__(self, cfg: Config, agent: Agent, vault: VaultTools, scheduler: Scheduler,
                 archive: ConversationArchive | None = None, transcriber: Transcriber | None = None):
        self.cfg, self.agent, self.vault, self.scheduler = cfg, agent, vault, scheduler
        self.transcriber = transcriber
        self._owns_archive = archive is None
        self.archive = archive or ConversationArchive(cfg.state_dir)
        self.db = self.archive.db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            DROP TABLE IF EXISTS sessions;
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, space TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                reply_to TEXT, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS message_space ON messages(space, created);
            CREATE TABLE IF NOT EXISTS subscriptions (endpoint TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)
        self.db.commit()
        self.tasks: dict[str, asyncio.Task] = {}
        self.push_tasks: set[asyncio.Task] = set()
        # Newest message timestamp a focused device reported displaying, per
        # space. In-memory only: it only matters within the push grace window.
        self.seen: dict[str, float] = {}
        self.hot: set[str] = set()
        self.accepting = True
        self.runner: web.AppRunner | None = None
        self.push_key = cfg.state_dir / "webpush.pem"
        self.public_key = ""
        if cfg.pwa_push_contact:
            from py_vapid import Vapid

            if not self.push_key.exists():
                from cryptography.hazmat.primitives import serialization

                key = Vapid()
                key.generate_keys()
                atomic_write_text(self.push_key, key.private_key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()).decode())
            from cryptography.hazmat.primitives import serialization

            key = Vapid.from_file(str(self.push_key))
            import base64

            self.public_key = base64.urlsafe_b64encode(key.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )).decode().rstrip("=")

        self.app = web.Application(middlewares=[self.security], client_max_size=64 * 1024)
        self.app.router.add_get("/api/session", self.session)
        self.app.router.add_get("/api/topics", self.topics)
        self.app.router.add_get("/api/now", self.now)
        self.app.router.add_get("/api/overview", self.overview)
        self.app.router.add_get("/api/page", self.page)
        self.app.router.add_get("/api/messages", self.messages)
        self.app.router.add_post("/api/messages", self.submit)
        self.app.router.add_post("/api/retry", self.retry)
        self.app.router.add_post("/api/reset", self.reset)
        self.app.router.add_post("/api/seen", self.mark_seen)
        self.app.router.add_post("/api/attachments", self.upload)
        self.app.router.add_get("/api/attachment", self.attachment)
        self.app.router.add_post("/api/transcribe", self.transcribe)
        self.app.router.add_post("/api/push", self.subscribe)
        self.app.router.add_delete("/api/push", self.unsubscribe)
        self.app.router.add_post("/api/push/test", self.test_push)
        self.app.router.add_get("/icon-{size}.png", self.icon)
        for route in _ASSETS:
            self.app.router.add_get(route, self.asset)

    @web.middleware
    async def security(self, request, handler):
        try:
            if request.host != urlsplit(self.cfg.pwa_origin).netloc:
                raise web.HTTPForbidden(text="Unrecognized host")
            if request.method not in ("GET", "HEAD"):
                if (request.headers.get("Origin") != self.cfg.pwa_origin
                        or request.headers.get("X-Noxide") != "1"):
                    raise web.HTTPForbidden(text="Same-origin requests required")
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text}, status=exc.status)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({"error": "Invalid request"}, status=400)
        except Exception:
            logger.exception("Companion request failed")
            response = web.json_response({"error": "Request failed; check server logs"}, status=500)
        response.headers.update({
            "Cache-Control": response.headers.get("Cache-Control", "no-store"), "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        })
        return response

    async def asset(self, request):
        name = _ASSETS[request.path]
        content = (files("assistant") / "pwa" / name).read_text(encoding="utf-8")
        if name == "index.html":
            content = content.replace("__AGENT_NAME__", html.escape(self.cfg.agent_name, quote=True))
        elif name == "manifest.webmanifest":
            manifest = json.loads(content)
            manifest.update(name=self.cfg.agent_name, short_name=self.cfg.agent_name)
            content = json.dumps(manifest, ensure_ascii=False)
        elif name == "sw.js":
            content = content.replace("__INSTANCE_VERSION__", shell_revision(self.cfg.agent_name))
            content = content.replace('"__AGENT_NAME__"', json.dumps(self.cfg.agent_name))
        return web.Response(text=content,
                            content_type=_MIMES["." + name.rsplit(".", 1)[-1]])

    async def icon(self, request):
        from PIL import Image, ImageDraw

        size = int(request.match_info["size"])
        if size not in (192, 512):
            raise web.HTTPNotFound()
        image = Image.new("RGB", (512, 512), "#eeeee7")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((140, 174, 202, 360), radius=8, fill="#303d32")
        draw.rounded_rectangle((178, 174, 365, 360), radius=75, fill="#303d32")
        draw.rounded_rectangle((202, 225, 304, 405), radius=43, fill="#eeeee7")
        draw.ellipse((348, 117, 398, 167), fill="#d8a66b")
        output = io.BytesIO()
        image.resize((size, size)).save(output, "PNG")
        return web.Response(body=output.getvalue(), content_type="image/png")

    async def session(self, request):
        return web.json_response({"push_key": self.public_key, "timezone": self.cfg.timezone,
                                  "agent_name": self.cfg.agent_name,
                                  "voice": self.transcriber is not None})

    def _space(self, value):
        if value == "general":
            return None
        if isinstance(value, str) and re.fullmatch(r"topic:[1-9][0-9]*", value):
            for topic in self._topics():
                if topic["id"] == value:
                    return int(value.split(":")[1])
            raise web.HTTPNotFound(text="This topic is no longer in the Telegram topic index")
        # Keep previously persisted project conversations accessible after the
        # switch to Telegram topics; never silently merge their histories.
        if not isinstance(value, str) or not re.fullmatch(r"wiki/(projects|areas)/[\w/-]+\.md", value):
            raise web.HTTPBadRequest(text="Choose an existing project or General")
        path = self.vault.abs_path(value)
        if path != self.cfg.vault_path / value:
            raise web.HTTPBadRequest(text="Project aliases are not supported")
        if not path.is_file():
            raise web.HTTPNotFound(text="This project no longer exists")
        thread = int.from_bytes(hashlib.sha256(value.encode()).digest()[:7], "big")
        self.agent.register_legacy_space(thread, value)
        return thread

    def _topics(self):
        topics = [{"id": "general", "name": "General"}]
        seen = set()
        for line in self._page("system/topics/index.md").splitlines():
            row = _parse_topic_row(line)
            if row is None:
                continue
            thread_id, slug, name = row
            if thread_id <= 0 or thread_id in seen or not re.fullmatch(r"[a-z0-9-]+", slug):
                continue
            seen.add(thread_id)
            topics.append({"id": f"topic:{thread_id}", "name": name or slug})
        return topics

    async def topics(self, request):
        topics = self._topics()
        # Earlier PWA builds stored project chats. Offer only those with saved
        # messages, so the UI change cannot strand existing conversations.
        for row in self.db.execute("SELECT DISTINCT space FROM messages WHERE space LIKE 'wiki/%' AND status!='deleted'"):
            try:
                self._space(row["space"])
            except (web.HTTPException, PermissionError):
                continue
            topics.append({"id": row["space"],
                           "name": row["space"].rsplit("/", 1)[-1][:-3], "legacy": True})
        return web.json_response({"topics": topics})

    async def now(self, request):
        return web.json_response({"content": self._page("wiki/now.md")})

    def _page(self, path):
        content = self.vault.read_file(path)
        if content.startswith("[file not found"):
            return ""
        return content

    async def overview(self, request):
        projects = []
        for glob in ("wiki/projects/**/*.md", "wiki/areas/**/*.md"):
            for path in self.vault.list_files(glob).splitlines():
                if path.startswith("[") or path.endswith("/index.md"):
                    continue
                if len(projects) >= 200:
                    break
                try:
                    self._space(path)
                except (web.HTTPException, PermissionError):
                    continue
                content = self._page(path)
                title = re.search(r"^# (.+)$", content, re.M)
                status = re.search(r"\*\*Status:\*\*\s*(.+)", content)
                projects.append({"path": path, "title": title[1] if title else path.rsplit("/", 1)[-1][:-3],
                                 "status": status[1][:500] if status else "Open this space to see its current state.",
                                 "tasks": len(re.findall(r"^\s*- \[ \] ", content, re.M))})
        return web.json_response({
            "date": datetime.now(ZoneInfo(self.cfg.timezone)).strftime("%A, %B %-d"),
            "now": self._page("wiki/now.md"), "projects": projects,
            "reminders": self.scheduler.entries_view(),
            "project_limit": 200,
        })

    async def page(self, request):
        path = request.query.get("path", "")
        self._space(path)
        if path == "general":
            raise web.HTTPBadRequest(text="General has no project page")
        return web.json_response({"path": path, "content": self._page(path)})

    async def messages(self, request):
        space = request.query.get("space", "general")
        self._space(space)
        before = float(request.query.get("before", "inf"))
        rows = self.db.execute("SELECT * FROM messages WHERE space=? AND status!='deleted' AND created<? ORDER BY created DESC LIMIT ?",
                               (space, before, MESSAGE_PAGE + 1)).fetchall()
        # The current generation lets the timeline draw a divider after a
        # reset that no message has followed yet.
        return web.json_response({"messages": [dict(r) for r in reversed(rows[:MESSAGE_PAGE])],
                                  "before": rows[MESSAGE_PAGE - 1]["created"] if len(rows) > MESSAGE_PAGE else None,
                                  "generation": self.archive.generation(space)})

    def _insert(self, space, role, text, status, *, message_id=None, reply_to=None, metadata=None):
        return self.archive.insert(space, role, text, status, message_id=message_id, reply_to=reply_to,
                                   metadata=metadata)

    async def submit(self, request):
        if not self.accepting:
            raise web.HTTPServiceUnavailable(text="Service restarting; your draft has not been sent")
        data = await request.json()
        if not self.accepting:
            raise web.HTTPServiceUnavailable(text="Service restarting; your draft has not been sent")
        space, text, message_id = data.get("space"), data.get("text"), data.get("id")
        self._space(space)
        attachments = self._attachments(data.get("attachments", []))
        if not isinstance(text, str) or len(text) > 20000 or not (text.strip() or attachments):
            raise web.HTTPBadRequest(text="Message must contain 1-20,000 characters or an image")
        if not isinstance(message_id, str) or not re.fullmatch(r"[a-f0-9-]{32,36}", message_id):
            raise web.HTTPBadRequest(text="A valid message ID is required")
        existing = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if existing:
            if existing["space"] != space or existing["text"] != text:
                raise web.HTTPConflict(text="Message ID already used")
            return web.json_response({"id": message_id}, status=202)
        if len(self.tasks) >= 4:
            raise web.HTTPTooManyRequests(text="Four messages are already in progress. Try again shortly.")
        if self.db.execute("SELECT 1 FROM messages WHERE space=? AND source='web' AND role='user' AND status NOT IN ('done','dismissed','deleted')",
                           (space,)).fetchone():
            raise web.HTTPConflict(text="Finish or clear the pending message in this space first")
        self._insert(space, "user", text, "queued", message_id=message_id,
                     metadata={"attachments": attachments} if attachments else None)
        self._launch(message_id)
        return web.json_response({"id": message_id}, status=202)

    def _attachments(self, value):
        """Validate stored attachment paths a message refers to."""
        if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
            raise web.HTTPBadRequest(text=f"Attach up to {MAX_ATTACHMENTS} images per message")
        for path in value:
            if not isinstance(path, str) or not _ATTACHMENT_PATH.fullmatch(path) or not self._attachment_file(path).is_file():
                raise web.HTTPBadRequest(text="Unknown attachment; upload it again")
        return value

    def _attachment_file(self, path):
        if not _ATTACHMENT_PATH.fullmatch(path or ""):
            raise web.HTTPBadRequest(text="Not an attachment path")
        return self.vault.abs_path(path)

    def _image_turn(self, row):
        """The model-facing text and vision input for a stored web message."""
        text = row["text"]
        attachments = json.loads(row["metadata"] or "{}").get("attachments", [])
        if not attachments:
            return text, None
        notes = [_IMAGE_NOTE.format(path=attachments[0])] if len(attachments) == 1 else [
            _IMAGE_NOTE_N.format(n=n, total=len(attachments), path=path) for n, path in enumerate(attachments, 1)]
        urls = []
        for path in attachments:
            data = self._attachment_file(path).read_bytes()
            mime = next((mime for mime, (_, ext) in _IMAGE_TYPES.items() if path.endswith("." + ext)), "image/jpeg")
            urls.append(f"data:{mime};base64," + base64.b64encode(data).decode())
        return (text.strip() or _NO_CAPTION) + "\n\n" + "\n".join(notes), urls

    def _launch(self, message_id):
        task = asyncio.create_task(self._process(message_id))
        self.tasks[message_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(message_id, None))

    async def _process(self, message_id):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.db.execute("UPDATE messages SET status='running', error='' WHERE id=?", (message_id,))
        self.db.commit()

        async def send(text, thread_id=None):
            self._insert(row["space"], "assistant", text, "done", reply_to=message_id)
            if getattr(self.agent, "archive", None) is self.archive:
                self.agent._queue_sent_note(WEB_CHAT_ID, self._space(row["space"]), text)
            return WEB_CHAT_ID

        try:
            thread = self._space(row["space"])
            archive_kwargs = {"message_id": message_id} if getattr(self.agent, "archive", None) is self.archive else {}
            text, image_data_urls = self._image_turn(row)
            if image_data_urls:
                archive_kwargs["image_data_urls"] = image_data_urls
            if message_id in self.hot:
                reply = await self.agent.retry_message(WEB_CHAT_ID, thread, text,
                                                       str(row["created"]), hot=True,
                                                       send_message_fn=send, extra_context=_WEB_CONTEXT, **archive_kwargs)
            else:
                self.hot.add(message_id)
                if row["error"]:
                    text = "[Explicit retry after interruption; earlier work may have partially completed. Re-read state before acting.] " + text
                if row["space"].startswith("wiki/"):
                    text = f"[Web space: {row['space']}. Read this owning page for relevant state.]\n{text}"
                reply = await self.agent.run(
                    WEB_CHAT_ID, text, thread_id=thread, send_message_fn=send,
                    extra_context=_WEB_CONTEXT,
                    source="web", **archive_kwargs,
                )
            if reply == MAX_ITERATIONS_REPLY:
                raise RuntimeError("Iteration limit reached. Some work may have completed; retry to continue.")
            if reply and not archive_kwargs:
                self._insert(row["space"], "assistant", reply, "done", reply_to=message_id)
            self.db.execute("UPDATE messages SET status='done', error='' WHERE id=? AND status NOT IN ('deleted','dismissed')", (message_id,))
            self.hot.discard(message_id)
            self.notify_push(row["space"], reply or "")
        except CopilotUnavailableError:
            self.db.execute("UPDATE messages SET status='unavailable', error=? WHERE id=?",
                            ("Copilot is unavailable. Your message is saved; retry when ready.", message_id))
        except asyncio.CancelledError:
            self.db.execute("UPDATE messages SET status='interrupted', error=? WHERE id=?",
                            ("Service stopped. Work may have partially completed; review before retrying.", message_id))
            raise
        except Exception:
            logger.exception("Web message failed")
            self.db.execute("UPDATE messages SET status='failed', error=? WHERE id=?",
                            ("The run could not finish. Work may have partially completed; review before retrying.", message_id))
        finally:
            self.db.commit()

    async def retry(self, request):
        data = await request.json()
        message_id = data.get("id")
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND role='user'", (message_id,)).fetchone()
        if not row or row["source"] != "web":
            raise web.HTTPNotFound(text="Message not found")
        if not self.accepting or len(self.tasks) >= 4:
            raise web.HTTPServiceUnavailable(text="Service busy; try shortly")
        if row["status"] not in ("failed", "unavailable", "interrupted"):
            raise web.HTTPConflict(text="This message is not awaiting retry")
        self.db.execute("UPDATE messages SET status='queued' WHERE id=?", (message_id,))
        self.db.commit()
        self._launch(message_id)
        return web.json_response({"ok": True})

    async def reset(self, request):
        space = (await request.json()).get("space")
        thread = self._space(space)
        if self.db.execute("SELECT 1 FROM messages WHERE space=? AND status IN ('running','queued')", (space,)).fetchone():
            raise web.HTTPConflict(text="Wait for the current run to finish")
        await self.agent.reset_conversation(WEB_CHAT_ID, thread)
        self.hot.difference_update(row["id"] for row in self.db.execute("SELECT id FROM messages WHERE space=?", (space,)))
        return web.json_response({"ok": True})

    async def observe_delivery(self, text, thread_id=None):
        space = f"topic:{thread_id}" if thread_id is not None else "general"
        if not any(topic["id"] == space for topic in self._topics()):
            space, thread_id = "general", None
        if getattr(self.agent, "archive", None) is not self.archive:
            self._insert(space, "assistant", text, "done")
            self.agent._queue_sent_note(WEB_CHAT_ID, thread_id, text)
        self.notify_push(space, text)

    async def subscribe(self, request):
        if not self.public_key:
            raise web.HTTPConflict(text="Push is not configured on this server")
        data = await request.json()
        endpoint = data.get("endpoint", "")
        url = urlsplit(endpoint)
        host = url.hostname or ""
        # Subscription endpoints are browser supplied, but never arbitrary URLs:
        # accepting them blindly turns the push sender into an SSRF proxy.
        allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
        allowed |= host.endswith(".notify.windows.com")
        keys = data.get("keys", {})
        if (not allowed or url.scheme != "https" or url.port not in (None, 443)
                or url.username or url.password or url.fragment or len(endpoint) > 4096
                or not re.fullmatch(r"[A-Za-z0-9_-]{80,100}", keys.get("p256dh", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{20,30}", keys.get("auth", ""))):
            raise web.HTTPBadRequest(text="Unsupported push subscription")
        if (self.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] >= 20
                and not self.db.execute("SELECT 1 FROM subscriptions WHERE endpoint=?", (endpoint,)).fetchone()):
            raise web.HTTPConflict(text="Maximum 20 registered devices; disable an old device first")
        self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?)",
                        (endpoint, json.dumps({"endpoint": endpoint, "keys": keys})))
        self.db.commit()
        return web.json_response({"ok": True})

    async def unsubscribe(self, request):
        endpoint = (await request.json()).get("endpoint")
        self.db.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
        self.db.commit()
        return web.json_response({"ok": True})

    async def test_push(self, request):
        if not self.public_key:
            raise web.HTTPConflict(text="Push is not configured")
        self.notify_push("general", "Hello there! This is the test reminder", grace=False)
        return web.json_response({"ok": True})

    async def _read_upload(self, request):
        """Read a raw upload body in chunks, refusing past the cap."""
        chunks, size = [], 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > UPLOAD_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_BYTES, actual_size=size,
                                                    text=f"Uploads are limited to {UPLOAD_BYTES // 1024 // 1024} MB")
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise web.HTTPBadRequest(text="Empty upload")
        return data

    async def upload(self, request):
        """Store one image in the vault's attachments folder, as Telegram photos are."""
        declared = request.content_type
        if declared not in _IMAGE_TYPES:
            raise web.HTTPBadRequest(text="Only JPEG, PNG, WebP and GIF images can be attached")
        data = await self._read_upload(request)
        magic, ext = _IMAGE_TYPES[declared]
        if not data.startswith(magic):
            raise web.HTTPBadRequest(text="That file is not the image type it claims to be")
        path = await asyncio.to_thread(self.vault.save_attachment, data, ext)
        return web.json_response({"path": path, "bytes": len(data)})

    async def attachment(self, request):
        file = self._attachment_file(request.query.get("path", ""))
        if not file.is_file():
            raise web.HTTPNotFound(text="Attachment not found")
        mime = next(mime for mime, (_, ext) in _IMAGE_TYPES.items() if file.suffix == "." + ext)
        return web.Response(body=await asyncio.to_thread(file.read_bytes), content_type=mime,
                            headers={"Cache-Control": "private, max-age=86400"})

    async def transcribe(self, request):
        """Turn a browser recording into text; the client sends the text as a normal message."""
        if self.transcriber is None:
            raise web.HTTPConflict(text="Voice messages aren't set up: set ELEVENLABS_API_KEY on the server")
        data = await self._read_upload(request)
        try:
            text = await self.transcriber.transcribe(data)
        except TranscriptionError as exc:
            raise web.HTTPBadGateway(text=f"Couldn't transcribe that: {exc}") from exc
        return web.json_response({"text": text})

    async def mark_seen(self, request):
        """A focused device displayed this space through the given timestamp."""
        data = await request.json()
        space, through = data.get("space"), data.get("through")
        self._space(space)
        if isinstance(through, bool) or not isinstance(through, (int, float)) or not math.isfinite(through):
            raise web.HTTPBadRequest(text="A message timestamp is required")
        self.seen[space] = max(self.seen.get(space, 0.0), float(through))
        return web.json_response({"ok": True})

    def notify_push(self, space="general", text="", *, grace=True):
        if not self.public_key or len(self.push_tasks) >= 8:
            return
        row = self.db.execute("SELECT max(created) FROM messages WHERE space=? AND role='assistant' AND status!='deleted'",
                              (space,)).fetchone()
        created = row[0] if row and row[0] is not None else time.time()
        task = asyncio.create_task(self._push_unless_seen(space, text, created) if grace
                                   else self._push(space, text))
        self.push_tasks.add(task)
        task.add_done_callback(self.push_tasks.discard)

    async def _push_unless_seen(self, space, text, created):
        # "Seen" means displayed on a focused device with the thread scrolled
        # to the end, not proof of reading; a device that acknowledges after
        # the window still gets the push, since it cannot be retracted.
        await asyncio.sleep(PUSH_GRACE_SECONDS)
        if self.seen.get(space, 0.0) >= created:
            logger.debug("Push skipped for %s: already displayed on a focused device", space)
            return
        await self._push(space, text)

    async def _push(self, space, text=""):
        from pywebpush import WebPushException, webpush
        from requests import Session

        # Leave room for encryption overhead under providers' 4 KB payload limit,
        # including text made entirely of four-byte Unicode characters.
        body = text[:500] + ("..." if len(text) > 500 else "")
        channel = next((topic["name"] for topic in self._topics() if topic["id"] == space), "General")
        if space.startswith("wiki/"):
            channel = space.rsplit("/", 1)[-1].removesuffix(".md")
        payload = json.dumps({"space": space, "body": body, "agent_name": self.cfg.agent_name,
                              "channel_name": channel[:100]}, ensure_ascii=False)

        def deliver(subscription):
            with Session() as transport:
                transport.max_redirects = 0
                webpush(subscription_info=subscription,
                        data=payload,
                        vapid_private_key=str(self.push_key),
                        vapid_claims={"sub": self.cfg.pwa_push_contact}, timeout=10, ttl=3600,
                        requests_session=transport)

        for row in self.db.execute("SELECT * FROM subscriptions").fetchall():
            try:
                await asyncio.to_thread(deliver, json.loads(row["data"]))
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in (404, 410):
                    self.db.execute("DELETE FROM subscriptions WHERE endpoint=?", (row["endpoint"],))
                    self.db.commit()
                else:
                    logger.warning("Web push delivery failed")
            except Exception:
                logger.warning("Web push delivery failed")

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.cfg.pwa_host, self.cfg.pwa_port).start()
        logger.info("Web companion listening at %s", self.cfg.pwa_origin)

    async def drain(self):
        self.accepting = False
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        # Replies finishing just before a restart still notify: their pushes
        # are only a grace period away.
        await asyncio.gather(*list(self.push_tasks), return_exceptions=True)

    async def close(self):
        self.accepting = False
        tasks = [*self.tasks.values(), *self.push_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self._owns_archive:
            self.archive.close()
