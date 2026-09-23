"""Private-network web app: the assistant's one interface.

Serves the packaged PWA and its API in the bot's own process. SQLite is a
delivery ledger, not a second knowledge base. Accepted messages survive
disconnects; interrupted work needs explicit retry to avoid replaying side
effects silently after a crash.
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

from .agent import MAX_ITERATIONS_REPLY, Agent
from .atomic import atomic_write_text
from .config import Config
from .conversations import SPACE, ConversationArchive
from .copilot import CopilotUnavailableError
from .models import ModelPicker
from .quiet_hours import parse_window, window_end
from .retry_queue import BACKOFF_INITIAL, next_backoff
from .tools import VaultTools
from .transcribe import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)
# A reply displayed on a focused device within this window notifies no device.
# Longer than the client's poll interval, so the device already showing the
# conversation gets to acknowledge before phones buzz.
PUSH_GRACE_SECONDS = 5
# Newest threads the chat opens with; the same page feeds the 2.2s poll, so it
# is kept small. Earlier pages load behind the timeline's manual link. Most
# threads are two messages; a long one is capped rather than paged.
MESSAGE_PAGE = 20
THREAD_MESSAGES = 100
# Web runs in flight, queued ones included; replies within one thread still
# run one at a time behind the agent's thread lock.
MAX_IN_FLIGHT = 16
# Upload bodies are read from the stream in chunks, so the app-wide JSON body
# limit does not apply to them.
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
# Documents the composer can attach besides images. The model gets each one's
# original name, type and stored path, never the bytes; `extract_attachment`
# reads them on demand. Text types cannot be sniffed by magic: they must decode
# as UTF-8 without NUL bytes. No video: the model can do nothing with it.
_FILE_TYPES = {
    "application/pdf": ((b"%PDF-",), "pdf"),
    "text/plain": ((), "txt"),
    "text/markdown": ((), "md"),
    "text/csv": ((), "csv"),
    "application/json": ((), "json"),
}
_UPLOAD_TYPES = {**_IMAGE_TYPES, **_FILE_TYPES}
_ATTACHMENT_PATH = re.compile(r"attachments/\d{4}-\d{2}-\d{2}-[0-9a-f]{6}\.("
                              + "|".join(ext for _, ext in _UPLOAD_TYPES.values()) + ")")
_IMAGE_NOTE = "[attached image — already stored in the vault at {path}; link it from a note if it is worth keeping, otherwise leave it]"
_IMAGE_NOTE_N = "[attached image {n} of {total} — already stored in the vault at {path}; link it from a note if it is worth keeping, otherwise leave it]"
_FILE_NOTE = "[attached file: {name} ({mime}) — already stored in the vault at {path}; you see its name and path, not its contents: use extract_attachment when they matter, never read_file; link it from a note if it is worth keeping, otherwise leave it]"
_FILE_NOTE_N = "[attached file {n} of {total}: {name} ({mime}) — already stored in the vault at {path}; you see its name and path, not its contents: use extract_attachment when they matter, never read_file; link it from a note if it is worth keeping, otherwise leave it]"
_NO_CAPTION_IMAGE = "The user sent this image without a caption."
_NO_CAPTION = "The user sent this without a caption."
# An original file name as the message shows it: one path component, no
# control characters, short.
MAX_NAME = 120
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A message that failed on a Copilot outage is retried by itself; the text on
# its status line says so, and the prefix a rerun after a restart carries.
OUTAGE_ERROR = "Copilot is unavailable. Your message is saved and will be retried when Copilot answers again."
AUTO_RETRY_PREFIX = "[Automatic retry after a Copilot outage; earlier work may have partially completed. Re-read state before acting.] "
# A reminder still unseen this long after its push is pushed once more, on
# the sweep that runs every NUDGE_POLL_SECONDS. Only reminders: a reply to the
# user's own message is something they asked for and will look for.
NUDGE_AFTER_SECONDS = 30 * 60
NUDGE_POLL_SECONDS = 60
NUDGE_WINDOW_SECONDS = 24 * 3600
# A held push (quiet hours) is released by the same sweep; older rows are
# never revisited, so a hold cannot resurface days later.
HELD_WINDOW_SECONDS = 24 * 3600


def _local_now(tz_name):
    """The user's wall clock (patched in tests)."""
    return datetime.now(ZoneInfo(tz_name))
_ASSETS = {"/": "index.html", "/app.js": "app.js", "/theme.js": "theme.js", "/style.css": "style.css",
           "/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest",
           "/icon.svg": "icon.svg"}


LIFECYCLE = "lifecycle"
STARTED = "Started"
STARTED_WITH_UPDATE = "Restarted with an app update. Reload when you're ready."


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


def _mime_of(path):
    """The declared type a stored attachment was uploaded as, from its extension."""
    ext = path.rsplit(".", 1)[-1]
    return next(mime for mime, (_, stored) in _UPLOAD_TYPES.items() if stored == ext)


def _is_text(data):
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _safe_name(name):
    """One path component, no control characters, capped; empty when nothing is left."""
    base = re.split(r"[/\\]", name)[-1]
    return " ".join(_CONTROL.sub("", base).split())[:MAX_NAME]


class Companion:
    def __init__(self, cfg: Config, agent: Agent, vault: VaultTools, archive: ConversationArchive,
                 transcriber: Transcriber | None = None, models: ModelPicker | None = None):
        self.cfg, self.agent, self.vault = cfg, agent, vault
        self.transcriber = transcriber
        self.models = models
        self.archive = archive
        self.db = self.archive.db
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            DROP TABLE IF EXISTS sessions;
            CREATE TABLE IF NOT EXISTS subscriptions (endpoint TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (space TEXT PRIMARY KEY, through REAL NOT NULL);
            -- Replies from before a space had any seen mark start out read, so
            -- the unread badge never opens on the whole history.
            INSERT OR IGNORE INTO seen (space, through)
                SELECT space, max(created) FROM messages WHERE role='assistant' AND status!='deleted' GROUP BY space;
        """)
        # Restart notices are opt-in per device (2026-09-19): every deploy
        # restarts the bot, and a phone does not need to hear about each one.
        if "lifecycle" not in {row["name"] for row in self.db.execute("PRAGMA table_info(subscriptions)")}:
            self.db.execute("ALTER TABLE subscriptions ADD COLUMN lifecycle INTEGER NOT NULL DEFAULT 0")
        self.db.commit()
        self.tasks: dict[str, asyncio.Task] = {}
        self.push_tasks: set[asyncio.Task] = set()
        # Newest message timestamp a focused device reported displaying, per
        # space: skips the push for a reply already on screen, and everything
        # newer is the unread count behind the app badge. Persisted, since the
        # badge must survive a restart.
        self.seen: dict[str, float] = {row["space"]: row["through"] for row in self.db.execute("SELECT space, through FROM seen")}
        self.hot: set[str] = set()
        # What a running message is doing right now, shown on its status line;
        # in memory only, since a restart marks the message interrupted anyway.
        self.activity: dict[str, str] = {}
        # Set whenever a message fails on a Copilot outage: its archived row,
        # marked unavailable, is the durable queue that `replay_outages`
        # drains, including rows left by a previous process.
        self.outage = asyncio.Event()
        if self.db.execute("SELECT 1 FROM messages WHERE space=? AND role='user' AND status='unavailable'", (SPACE,)).fetchone():
            self.outage.set()
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
        self.app.router.add_get("/api/now", self.now)
        self.app.router.add_get("/api/messages", self.messages)
        self.app.router.add_post("/api/messages", self.submit)
        self.app.router.add_post("/api/retry", self.retry)
        self.app.router.add_post("/api/reset", self.reset)
        self.app.router.add_post("/api/seen", self.mark_seen)
        self.app.router.add_get("/api/models", self.list_models)
        self.app.router.add_post("/api/model", self.select_model)
        self.app.router.add_post("/api/attachments", self.upload)
        self.app.router.add_get("/api/attachment", self.attachment)
        self.app.router.add_post("/api/transcribe", self.transcribe)
        self.app.router.add_get("/api/push", self.push_state)
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
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
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

    async def now(self, request):
        return web.json_response({"content": self._page("wiki/now.md")})

    async def list_models(self, request):
        """The model picker's choices, refreshed from the catalog when it answers."""
        if self.models is None:
            raise web.HTTPConflict(text="Model switching is not available")
        await self.models.refresh()
        return web.json_response(self.models.choices())

    async def select_model(self, request):
        if self.models is None:
            raise web.HTTPConflict(text="Model switching is not available")
        alias = (await request.json()).get("alias")
        if not isinstance(alias, str):
            raise web.HTTPBadRequest(text="A model alias is required")
        try:
            option = self.models.select(alias)
        except KeyError:
            raise web.HTTPBadRequest(text=f"Unknown model {alias!r}") from None
        logger.info("Model switched to %s (%s)", alias, option.id)
        return web.json_response({"ok": True, "current": alias, "id": option.id})

    def _page(self, path):
        content = self.vault.read_file(path)
        if content.startswith("[file not found"):
            return ""
        return content

    async def messages(self, request):
        """A page of threads in the order they started, newest last; ``before`` pages by start time.

        A reply does not move its thread: the list is a timeline of
        interactions, each box staying where its first message put it.
        """
        before = float(request.query.get("before", "inf"))
        heads = self.db.execute(
            "SELECT thread, min(created) AS started FROM messages WHERE space=? AND status!='deleted'"
            " GROUP BY thread HAVING started<? ORDER BY started DESC LIMIT ?",
            (SPACE, before, MESSAGE_PAGE + 1)).fetchall()
        threads = []
        for head in reversed(heads[:MESSAGE_PAGE]):
            # A thread past the cap keeps its newest messages: that is where
            # the reply goes and what the last-activity cursor refers to.
            rows = self.db.execute(
                "SELECT * FROM messages WHERE space=? AND thread=? AND status!='deleted' ORDER BY created DESC LIMIT ?",
                (SPACE, head["thread"], THREAD_MESSAGES)).fetchall()
            messages = [dict(r) for r in reversed(rows)]
            for message in messages:
                if message["id"] in self.activity:
                    message["activity"] = self.activity[message["id"]]
            threads.append({"id": head["thread"], "started": head["started"], "messages": messages})
        # The current generation lets the timeline draw a divider after a
        # reset that no thread has followed yet.
        return web.json_response({"threads": threads,
                                  "before": heads[MESSAGE_PAGE - 1]["started"] if len(heads) > MESSAGE_PAGE else None,
                                  "unread": self.unread_count(),
                                  "generation": self.archive.generation(SPACE)})

    def _insert(self, space, role, text, status, *, message_id=None, reply_to=None, metadata=None):
        return self.archive.insert(space, role, text, status, message_id=message_id, reply_to=reply_to,
                                   metadata=metadata)

    async def submit(self, request):
        if not self.accepting:
            raise web.HTTPServiceUnavailable(text="Service restarting; your draft has not been sent")
        data = await request.json()
        if not self.accepting:
            raise web.HTTPServiceUnavailable(text="Service restarting; your draft has not been sent")
        text, message_id, reply_to = data.get("text"), data.get("id"), data.get("reply_to")
        if reply_to is not None:
            parent = self.archive.get(reply_to) if isinstance(reply_to, str) else None
            if parent is None or parent["space"] != SPACE or parent["status"] == "deleted":
                raise web.HTTPBadRequest(text="The message you are replying to is not in this chat")
        attachments = self._attachments(data.get("attachments", []))
        names = self._names(data.get("names"), attachments)
        if not isinstance(text, str) or len(text) > 20000 or not (text.strip() or attachments):
            raise web.HTTPBadRequest(text="Message must contain 1-20,000 characters or an attachment")
        if not isinstance(message_id, str) or not re.fullmatch(r"[a-f0-9-]{32,36}", message_id):
            raise web.HTTPBadRequest(text="A valid message ID is required")
        existing = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if existing:
            if existing["space"] != SPACE or existing["text"] != text or existing["reply_to"] != reply_to:
                raise web.HTTPConflict(text="Message ID already used")
            return web.json_response({"id": message_id}, status=202)
        if len(self.tasks) >= MAX_IN_FLIGHT:
            raise web.HTTPTooManyRequests(text="Too many messages are in progress. Try again shortly.")
        # Several messages may be pending: each is its own thread and its own
        # run, in parallel; replies within one thread are serialized in
        # arrival order by the agent's thread lock.
        metadata = {"attachments": attachments, **({"names": names} if names else {})} if attachments else None
        self._insert(SPACE, "user", text, "queued", message_id=message_id, reply_to=reply_to, metadata=metadata)
        self._launch(message_id)
        return web.json_response({"id": message_id}, status=202)

    def _attachments(self, value):
        """Validate stored attachment paths a message refers to."""
        if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
            raise web.HTTPBadRequest(text=f"Attach up to {MAX_ATTACHMENTS} files per message")
        for path in value:
            if not isinstance(path, str) or not _ATTACHMENT_PATH.fullmatch(path) or not self._attachment_file(path).is_file():
                raise web.HTTPBadRequest(text="Unknown attachment; upload it again")
        return value

    def _names(self, value, attachments):
        """Original file names by stored path, as the model and the timeline show them."""
        if value is None:
            return {}
        if not isinstance(value, dict) or not set(value) <= set(attachments):
            raise web.HTTPBadRequest(text="File names must belong to the message's attachments")
        names = {}
        for path, name in value.items():
            if not isinstance(name, str):
                raise web.HTTPBadRequest(text="File names must be text")
            if clean := _safe_name(name):
                names[path] = clean
        return names

    def _attachment_file(self, path):
        if not _ATTACHMENT_PATH.fullmatch(path or ""):
            raise web.HTTPBadRequest(text="Not an attachment path")
        return self.vault.abs_path(path)

    def _attachment_turn(self, row):
        """The model-facing text and vision input for a stored web message.

        Images ride the turn as vision input with a stored-path note each;
        other files contribute only a note with their original name, type and
        path. Both kinds are numbered separately when there are several.
        """
        text = row["text"]
        metadata = json.loads(row["metadata"] or "{}")
        attachments, names = metadata.get("attachments", []), metadata.get("names", {})
        if not attachments:
            return text, None
        images = [path for path in attachments if _mime_of(path) in _IMAGE_TYPES]
        files = [path for path in attachments if path not in images]
        notes = [_IMAGE_NOTE.format(path=images[0])] if len(images) == 1 else [
            _IMAGE_NOTE_N.format(n=n, total=len(images), path=path) for n, path in enumerate(images, 1)]
        for n, path in enumerate(files, 1):
            fields = {"name": names.get(path) or path.rsplit("/", 1)[-1], "mime": _mime_of(path), "path": path}
            notes.append(_FILE_NOTE.format(**fields) if len(files) == 1 else _FILE_NOTE_N.format(n=n, total=len(files), **fields))
        urls = [f"data:{_mime_of(path)};base64," + base64.b64encode(self._attachment_file(path).read_bytes()).decode()
                for path in images]
        caption = text.strip() or (_NO_CAPTION_IMAGE if not files else _NO_CAPTION)
        return caption + "\n\n" + "\n".join(notes), urls or None

    def _launch(self, message_id, *, automatic=False):
        task = asyncio.create_task(self._process(message_id, automatic=automatic))
        self.tasks[message_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(message_id, None))

    async def _process(self, message_id, *, automatic=False):
        """Run one archived message; ``automatic`` marks a replay after a Copilot outage."""
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        # Stays 'queued' while waiting behind earlier messages; Agent.run
        # marks it 'running' once it holds the conversation lock.
        self.db.execute("UPDATE messages SET error='' WHERE id=?", (message_id,))
        self.db.commit()

        async def send(text):
            self._insert(SPACE, "assistant", text, "done", reply_to=message_id)

        try:
            text, image_data_urls = self._attachment_turn(row)
            if message_id in self.hot:
                # The failed turn is still in the agent's history: resume it
                # in place rather than replaying the message (and its writes).
                self.db.execute("UPDATE messages SET status='running' WHERE id=?", (message_id,))
                self.db.commit()
                reply = await self.agent.resume(message_id, send_message_fn=send)
            else:
                self.hot.add(message_id)
                if row["error"]:
                    # A rerun, not a resume: the failed turn is gone from
                    # memory (a restart), so the model is told the first
                    # attempt may have acted already.
                    text = (AUTO_RETRY_PREFIX if automatic else
                            "[Explicit retry after interruption; earlier work may have partially completed. Re-read state before acting.] ") + text

                async def researching():
                    self.activity[message_id] = "Searching the web…"

                reply = await self.agent.run(
                    text, message_id=message_id, image_data_urls=image_data_urls,
                    send_message_fn=send, on_research=researching,
                )
            if reply == MAX_ITERATIONS_REPLY:
                raise RuntimeError("Iteration limit reached. Some work may have completed; retry to continue.")
            self.db.execute("UPDATE messages SET status='done', error='' WHERE id=? AND status NOT IN ('deleted','dismissed')", (message_id,))
            self.hot.discard(message_id)
            self.notify_push(reply or "", thread=row["thread"], message_id=f"reply:{message_id}")
        except CopilotUnavailableError:
            self.db.execute("UPDATE messages SET status='unavailable', error=? WHERE id=?", (OUTAGE_ERROR, message_id))
            self.outage.set()
        except asyncio.CancelledError:
            self.db.execute("UPDATE messages SET status='interrupted', error=? WHERE id=?",
                            ("Service stopped. Work may have partially completed; review before retrying.", message_id))
            raise
        except Exception:
            logger.exception("Web message failed")
            self.db.execute("UPDATE messages SET status='failed', error=? WHERE id=?",
                            ("The run could not finish. Work may have partially completed; review before retrying.", message_id))
        finally:
            self.activity.pop(message_id, None)
            self.db.commit()

    async def retry(self, request):
        data = await request.json()
        message_id = data.get("id")
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND role='user'", (message_id,)).fetchone()
        if not row:
            raise web.HTTPNotFound(text="Message not found")
        if not self.accepting or len(self.tasks) >= MAX_IN_FLIGHT:
            raise web.HTTPServiceUnavailable(text="Service busy; try shortly")
        if row["status"] not in ("failed", "unavailable", "interrupted"):
            raise web.HTTPConflict(text="This message is not awaiting retry")
        self.db.execute("UPDATE messages SET status='queued' WHERE id=?", (message_id,))
        self.db.commit()
        self._launch(message_id)
        return web.json_response({"ok": True})

    async def replay_outages(self):
        """Retry messages that failed on a Copilot outage, oldest first, backing off while it lasts.

        The archived rows marked unavailable are the queue, so a restart
        mid-outage loses nothing. In this process the failed turn is resumed
        in place; after a restart the message reruns with a warning. The
        retry is the health probe: no separate check could pass while chat
        still fails. A row the user retried by hand meanwhile is skipped, and
        interrupted work (a restart mid-run) is never replayed: it waits for
        an explicit retry because it may have written already.
        """
        delay = BACKOFF_INITIAL
        while True:
            await self.outage.wait()
            row = self.db.execute(
                "SELECT id FROM messages WHERE space=? AND role='user' AND status='unavailable' ORDER BY created LIMIT 1",
                (SPACE,)).fetchone()
            if row is None or not self.accepting:
                self.outage.clear()
                continue
            message_id = row["id"]
            if message_id not in self.tasks and len(self.tasks) < MAX_IN_FLIGHT:
                self.db.execute("UPDATE messages SET status='queued' WHERE id=?", (message_id,))
                self.db.commit()
                self._launch(message_id, automatic=True)
            if task := self.tasks.get(message_id):
                # Not `await task`: cancelling this loop at shutdown must not
                # cancel the run, which the app's drain is waiting for.
                await asyncio.wait({task})
            if self.archive.get(message_id)["status"] != "unavailable":
                delay = BACKOFF_INITIAL
                continue
            logger.warning("Copilot still unavailable; retrying web message in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = next_backoff(delay)

    async def reset(self, request):
        if self.db.execute("SELECT 1 FROM messages WHERE space=? AND status IN ('running','queued')", (SPACE,)).fetchone():
            raise web.HTTPConflict(text="Wait for the current run to finish")
        await self.agent.reset_conversation()
        self.hot.difference_update(row["id"] for row in self.db.execute("SELECT id FROM messages WHERE space=?", (SPACE,)))
        return web.json_response({"ok": True})

    async def deliver(self, text):
        """A proactive message (a scheduled run's reminder): archive it as a thread root and notify.

        A reply to it continues its thread with the reminder in context; a
        message typed on its own sees it through the ambient block.
        """
        thread = self._insert(SPACE, "assistant", text, "done")
        held_until = self.quiet_until()
        if held_until is not None:
            # Quiet hours: archived now, pushed when the window ends (the
            # nightly compile's notices woke the phone at 03:01 on four of
            # nine nights, 2026-09). Kept on the row so a restart cannot
            # lose it; release_held() sends it through the seen check.
            self._record_push(thread, {"held_until": held_until.timestamp()})
            return
        self.notify_push(text, thread=thread, message_id=thread)

    def quiet_until(self):
        """The end of the current quiet-hours window, or None outside it (or when unset)."""
        window = parse_window(self.cfg.pwa_quiet_hours)
        if window is None:
            return None
        return window_end(_local_now(self.cfg.timezone), window)

    async def release_held(self):
        """Push every reminder whose quiet-hours hold has ended, once, through the seen check."""
        if not self.public_key:
            return
        now = _local_now(self.cfg.timezone).timestamp()
        rows = self.db.execute(
            "SELECT id, text, created, metadata FROM messages WHERE space=? AND role='assistant'"
            " AND status='done' AND id=thread AND created>? ORDER BY created",
            (SPACE, time.time() - HELD_WINDOW_SECONDS)).fetchall()
        for row in rows:
            push = json.loads(row["metadata"] or "{}").get("push") or {}
            held_until = push.get("held_until")
            if held_until is None or held_until > now:
                continue
            metadata = json.loads(row["metadata"] or "{}")
            metadata["push"] = {k: v for k, v in push.items() if k != "held_until"}
            self.db.execute("UPDATE messages SET metadata=? WHERE id=?",
                            (json.dumps(metadata, ensure_ascii=False), row["id"]))
            self.db.commit()
            logger.info("Quiet hours over; pushing a held reminder")
            await self._push_unless_seen(row["text"], row["created"], row["id"], message_id=row["id"])

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
        existing = self.db.execute("SELECT lifecycle FROM subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if existing is None and self.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] >= 20:
            raise web.HTTPConflict(text="Maximum 20 registered devices; disable an old device first")
        # A re-registration that says nothing about restart notices keeps the
        # device's choice; only an explicit flag changes it.
        lifecycle = data.get("lifecycle", bool(existing["lifecycle"]) if existing else False)
        if not isinstance(lifecycle, bool):
            raise web.HTTPBadRequest(text="lifecycle must be true or false")
        self.db.execute("INSERT OR REPLACE INTO subscriptions (endpoint, data, lifecycle) VALUES (?,?,?)",
                        (endpoint, json.dumps({"endpoint": endpoint, "keys": keys}), int(lifecycle)))
        self.db.commit()
        return web.json_response({"ok": True, "lifecycle": lifecycle})

    async def push_state(self, request):
        """Whether this device is registered and asked for restart notices."""
        row = self.db.execute("SELECT lifecycle FROM subscriptions WHERE endpoint=?",
                              (request.query.get("endpoint", ""),)).fetchone()
        return web.json_response({"registered": row is not None, "lifecycle": bool(row and row["lifecycle"])})

    async def unsubscribe(self, request):
        endpoint = (await request.json()).get("endpoint")
        self.db.execute("DELETE FROM subscriptions WHERE endpoint=?", (endpoint,))
        self.db.commit()
        return web.json_response({"ok": True})

    async def test_push(self, request):
        if not self.public_key:
            raise web.HTTPConflict(text="Push is not configured")
        self.notify_push("Hello there! This is the test reminder", grace=False)
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
        """Store one image or document in the vault's attachments folder."""
        declared = request.content_type
        if declared not in _UPLOAD_TYPES:
            raise web.HTTPBadRequest(text="Only images (JPEG, PNG, WebP, GIF), PDFs and text files (plain, Markdown, CSV, JSON) can be attached")
        data = await self._read_upload(request)
        magic, ext = _UPLOAD_TYPES[declared]
        if magic and not data.startswith(magic):
            raise web.HTTPBadRequest(text="That file is not the type it claims to be")
        if not magic and not _is_text(data):
            raise web.HTTPBadRequest(text="That file is not text")
        path = await asyncio.to_thread(self.vault.save_attachment, data, ext)
        return web.json_response({"path": path, "bytes": len(data)})

    async def attachment(self, request):
        file = self._attachment_file(request.query.get("path", ""))
        if not file.is_file():
            raise web.HTTPNotFound(text="Attachment not found")
        return web.Response(body=await asyncio.to_thread(file.read_bytes), content_type=_mime_of(file.name),
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
        """A focused device displayed the chat through the given timestamp."""
        through = (await request.json()).get("through")
        if isinstance(through, bool) or not isinstance(through, (int, float)) or not math.isfinite(through):
            raise web.HTTPBadRequest(text="A message timestamp is required")
        self.seen[SPACE] = max(self.seen.get(SPACE, 0.0), float(through))
        self.db.execute("INSERT INTO seen (space, through) VALUES (?, ?) ON CONFLICT(space) DO UPDATE SET through=max(through, excluded.through)",
                        (SPACE, float(through)))
        self.db.commit()
        return web.json_response({"ok": True})

    def unread_count(self):
        """Replies newer than the seen mark: the app badge number."""
        return self.db.execute(
            "SELECT count(*) FROM messages WHERE role='assistant' AND status!='deleted' AND space=?"
            " AND created > COALESCE((SELECT through FROM seen WHERE space=?), 0)",
            (SPACE, SPACE)).fetchone()[0]

    def notify_push(self, text="", *, thread=None, grace=True, message_id=None):
        """Push a reply or reminder; ``message_id`` names the archived row that records the outcome."""
        if not self.public_key or len(self.push_tasks) >= 8:
            return
        row = self.db.execute("SELECT max(created) FROM messages WHERE space=? AND role='assistant' AND status!='deleted'",
                              (SPACE,)).fetchone()
        created = row[0] if row and row[0] is not None else time.time()
        task = asyncio.create_task(self._push_unless_seen(text, created, thread, message_id) if grace
                                   else self._push(text, thread, message_id=message_id))
        self.push_tasks.add(task)
        task.add_done_callback(self.push_tasks.discard)

    def _record_push(self, message_id, outcome):
        """Merge a push outcome into the archived row's metadata, for the timeline and the nudge sweep."""
        row = self.archive.get(message_id)
        if row is None:
            return
        metadata = json.loads(row["metadata"] or "{}")
        metadata["push"] = {**metadata.get("push", {}), **outcome}
        self.db.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), message_id))
        self.db.commit()

    async def nudge_unseen(self):
        """Push once more each reminder still unseen NUDGE_AFTER_SECONDS after it was pushed.

        Reminders only: threads the assistant started. Each is nudged once,
        marked on its row, and only within the last day, so a device that
        has been away for a week is not buried on its return.
        """
        if not self.public_key or self.quiet_until() is not None:
            return
        now = time.time()
        rows = self.db.execute(
            "SELECT id, text, metadata FROM messages WHERE space=? AND role='assistant' AND status='done'"
            " AND id=thread AND created<? AND created>? AND created>? ORDER BY created",
            (SPACE, now - NUDGE_AFTER_SECONDS, now - NUDGE_WINDOW_SECONDS, self.seen.get(SPACE, 0.0))).fetchall()
        for row in rows:
            push = json.loads(row["metadata"] or "{}").get("push")
            if push is None or push.get("nudged") or push.get("displayed") or "at" not in push:
                continue
            if now - push["at"] < NUDGE_AFTER_SECONDS:
                continue  # a held push released this morning is not stale yet
            self._record_push(row["id"], {"nudged": True})
            logger.info("Reminder still unseen after %d min; pushing again", NUDGE_AFTER_SECONDS // 60)
            await self._push(row["text"], row["id"], message_id=row["id"])

    async def nudge_loop(self):
        while True:
            await asyncio.sleep(NUDGE_POLL_SECONDS)
            try:
                await self.release_held()
                await self.nudge_unseen()
            except Exception:
                logger.exception("Reminder nudge sweep failed")

    def notify_lifecycle(self, text, *, important=False):
        """Push a process event (restarting, started) to the devices that asked.

        Lifecycle text is never archived: it must not become assistant rows the
        model sees. Only devices that opted in hear routine restarts; an
        important event (queued work dropped) reaches every device. Returns the
        push task so a shutdown can wait for an important one.
        """
        if not self.public_key or len(self.push_tasks) >= 8:
            return None
        task = asyncio.create_task(self._push(text, kind=LIFECYCLE, opted_in_only=not important))
        self.push_tasks.add(task)
        task.add_done_callback(self.push_tasks.discard)
        return task

    def startup_message(self):
        """The lifecycle text for this start: plain, or naming an app update.

        The shell revision of the previous start is kept in the state dir; a
        different one now means installed apps will be offered an update, which
        is the restart worth hearing about.
        """
        marker = self.cfg.state_dir / "shell_revision"
        current = shell_revision(self.cfg.agent_name)
        try:
            previous = marker.read_text().strip()
        except OSError:
            previous = ""
        if previous != current:
            atomic_write_text(marker, current + "\n")
        return STARTED_WITH_UPDATE if previous and previous != current else STARTED

    async def _push_unless_seen(self, text, created, thread=None, message_id=None):
        # "Seen" means displayed on a focused, recently used device with the
        # thread scrolled to the end, not proof of reading; a device that
        # acknowledges after the window still gets the push, since it cannot
        # be retracted.
        await asyncio.sleep(PUSH_GRACE_SECONDS)
        if self.seen.get(SPACE, 0.0) >= created:
            logger.debug("Push skipped: already displayed on a focused device")
            if message_id:
                self._record_push(message_id, {"displayed": True})
            return
        await self._push(text, thread, message_id=message_id)

    async def _push(self, text="", thread=None, *, kind="reply", opted_in_only=False, message_id=None):
        """Send one push to every (or every opted-in) device.

        With ``message_id``, the outcome — devices tried and devices whose
        provider accepted the push — is recorded on that archived row. Push
        acceptance is the provider taking the message, not the device
        displaying it; it is still the only delivery signal there is.
        """
        from pywebpush import WebPushException, webpush
        from requests import Session

        # Leave room for encryption overhead under providers' 4 KB payload limit,
        # including text made entirely of four-byte Unicode characters.
        body = text[:500] + ("..." if len(text) > 500 else "")
        if kind == LIFECYCLE:
            # No thread to open and no unread count: the worker keeps these
            # under their own notification tag and leaves the badge alone.
            payload = json.dumps({"body": body, "agent_name": self.cfg.agent_name, "kind": LIFECYCLE},
                                 ensure_ascii=False)
        else:
            payload = json.dumps({"body": body, "agent_name": self.cfg.agent_name, "thread": thread,
                                  "unread": self.unread_count()}, ensure_ascii=False)

        def deliver(subscription):
            with Session() as transport:
                transport.max_redirects = 0
                webpush(subscription_info=subscription,
                        data=payload,
                        vapid_private_key=str(self.push_key),
                        vapid_claims={"sub": self.cfg.pwa_push_contact}, timeout=10, ttl=3600,
                        requests_session=transport)

        query = "SELECT * FROM subscriptions" + (" WHERE lifecycle=1" if opted_in_only else "")
        rows = self.db.execute(query).fetchall()
        accepted = 0
        for row in rows:
            try:
                await asyncio.to_thread(deliver, json.loads(row["data"]))
                accepted += 1
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in (404, 410):
                    self.db.execute("DELETE FROM subscriptions WHERE endpoint=?", (row["endpoint"],))
                    self.db.commit()
                else:
                    logger.warning("Web push delivery failed")
            except Exception:
                logger.warning("Web push delivery failed")
        if message_id:
            self._record_push(message_id, {"at": time.time(), "devices": len(rows), "accepted": accepted})

    def pending(self):
        """Web runs in flight, queued ones included."""
        return len(self.tasks)

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.cfg.pwa_host, self.cfg.pwa_port).start()
        logger.info("Web app listening at %s", self.cfg.pwa_origin)

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
