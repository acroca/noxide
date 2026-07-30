"""Telegram bot: long-polling, user filtering, message handling."""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ReactionEmoji
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .transcribe import TranscriptionError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from telegram import Message

    from .agent import Agent
    from .transcribe import Transcriber

logger = logging.getLogger(__name__)

_MAX_MSG_LEN = 4000
# Telegram's Bot API refuses file downloads above 20 MB anyway
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# Updates are handled concurrently up to this cap, so a long agent run in one
# forum topic does not block messages arriving in another. Runs within one
# conversation stay sequential — Agent.run serializes per (chat_id, thread_id).
_CONCURRENT_UPDATES = 8
_CHAT_ID_FILENAME = "chat_id"
_MODEL_CB_PREFIX = "model:"
# Bots may only react with Telegram's fixed emoji set (🔎 isn't in it),
# so 👀 marks messages that triggered a web search
_SEARCH_REACTION = ReactionEmoji.EYES
_REPLY_QUOTE_MAX_CHARS = 300


def _attachment_ext(file_name: str | None, mime_type: str | None) -> str:
    """Best-effort extension for a stored attachment: filename, then MIME type, then 'bin'."""
    if file_name and "." in file_name:
        return file_name.rsplit(".", 1)[1]
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed.lstrip(".")
    return "bin"


def _fmt_wait(seconds: int) -> str:
    """Human wait duration for rate-limit notes: '~3 min' or '~19h' (rounded up)."""
    if seconds >= 3600:
        return f"~{-(-seconds // 3600)}h"
    return f"~{max(1, -(-seconds // 60))} min"


def _reply_context(msg: Message) -> str | None:
    """Quoted-message context for a Telegram reply, or None when not a real reply.

    In forum topics every plain message "replies to" the topic-creation
    service message, so those are skipped. Prefers the partial quote when
    the user quoted a specific passage.
    """
    reply = msg.reply_to_message
    if reply is None or reply.forum_topic_created is not None:
        return None
    quoted = (msg.quote.text if msg.quote else None) or reply.text or reply.caption
    if not quoted:
        return None
    if len(quoted) > _REPLY_QUOTE_MAX_CHARS:
        quoted = quoted[:_REPLY_QUOTE_MAX_CHARS] + "…"
    return f'[replying to: "{quoted}"]'


def _split_message(text: str, max_len: int = _MAX_MSG_LEN) -> list[str]:
    """Split long messages into chunks of at most max_len characters."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_user_ids: list[int],
        agent: Agent,
        transcriber: Transcriber | None = None,
        save_attachment_fn: Callable[[bytes, str], str] | None = None,
        models: dict[str, str] | None = None,
        default_model: str = "",
        set_model_fn: Callable[[str], None] | None = None,
        state_dir: Path | None = None,
        default_chat_id: int | None = None,
    ) -> None:
        self._token = token
        self._allowed_user_ids = allowed_user_ids
        self._agent = agent
        self._transcriber = transcriber
        self._save_attachment_fn = save_attachment_fn
        self._models = models or {}
        self._default_model = default_model
        self._set_model_fn = set_model_fn
        # No persistence: selection resets to the default on every restart
        self._current_alias = default_model
        # Monotonic deadline while Telegram flood-limits group-title changes
        self._title_flood_until: float = 0.0
        self._app: Application | None = None
        # Message updates currently inside _handle_message. With concurrent
        # updates the queue drains into tasks immediately, so qsize alone
        # undercounts what a shutdown drain still has to finish.
        self._inflight_updates = 0
        # chat_id used for proactive sends: restored from state_dir when available,
        # falling back to the configured default until one is learned from an
        # incoming allowed message
        self._state_dir = state_dir
        persisted_chat_id = self._load_chat_id()
        self._chat_id: int | None = (
            persisted_chat_id if persisted_chat_id is not None else default_chat_id
        )

    async def send_message(self, text: str, message_thread_id: int | None = None) -> None:
        """Send a proactive message (e.g. from a scheduled job).

        Pass ``message_thread_id`` to deliver the message into a specific forum topic.
        """
        if self._chat_id is None:
            logger.warning("send_message called but no chat_id set yet; dropping: %r", text[:80])
            return
        if self._app is None:
            logger.warning("send_message called before bot started; dropping.")
            return
        kwargs: dict[str, Any] = {"chat_id": self._chat_id, "text": ""}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        for chunk in _split_message(text):
            await self._app.bot.send_message(**{**kwargs, "text": chunk})

    async def create_forum_topic(self, name: str) -> dict[str, Any]:
        """Create a new forum topic in the group and return its data dict.

        Returns a dict containing at least ``message_thread_id``.
        """
        if self._chat_id is None:
            raise RuntimeError("create_forum_topic called but no chat_id set yet")
        if self._app is None:
            raise RuntimeError("create_forum_topic called before bot started")
        forum_topic = await self._app.bot.create_forum_topic(
            chat_id=self._chat_id,
            name=name,
        )
        return {"message_thread_id": forum_topic.message_thread_id, "name": forum_topic.name}

    # ------------------------------------------------------------------
    # chat_id persistence (survives restarts so proactive sends keep working)
    # ------------------------------------------------------------------

    def _load_chat_id(self) -> int | None:
        if self._state_dir is None:
            return None
        path = self._state_dir / _CHAT_ID_FILENAME
        if not path.exists():
            return None
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            logger.warning("Could not read persisted chat_id from %s", path, exc_info=True)
            return None

    def _remember_chat_id(self, chat_id: int) -> None:
        if self._chat_id is not None:
            if chat_id != self._chat_id:
                logger.warning(
                    "Ignoring chat_id=%d for proactive delivery; home chat_id=%d is pinned",
                    chat_id,
                    self._chat_id,
                )
            return
        self._chat_id = chat_id
        self._persist_chat_id()
        logger.info("Pinned proactive delivery to chat_id=%d", chat_id)

    def _migrate_chat_id(self, old_chat_id: int, new_chat_id: int) -> None:
        """Accept a Telegram-declared migration only for the pinned home chat."""
        if self._chat_id != old_chat_id:
            logger.warning(
                "Ignoring unrelated chat migration %d -> %d; home chat_id=%s",
                old_chat_id,
                new_chat_id,
                self._chat_id,
            )
            return
        self._chat_id = new_chat_id
        self._persist_chat_id()
        logger.info("Migrated proactive delivery from chat_id=%d to %d", old_chat_id, new_chat_id)

    def _persist_chat_id(self) -> None:
        if self._state_dir is None:
            return
        path = self._state_dir / _CHAT_ID_FILENAME
        try:
            path.write_text(str(self._chat_id))
        except OSError:
            logger.warning("Could not persist chat_id to %s", path, exc_info=True)

    # ------------------------------------------------------------------
    # Model-selection group title
    # ------------------------------------------------------------------

    def _strip_alias_suffix(self, title: str) -> str:
        """Drop a trailing ' (<alias>)' left over from a previous selection."""
        for alias in self._models:
            suffix = f" ({alias})"
            if title.endswith(suffix):
                return title[: -len(suffix)]
        return title

    async def _set_group_title(self, alias: str) -> str | None:
        """Retitle the group to `<title> (<alias>)` (plain title for the default model).

        Returns a user-facing note when the title was not updated, None on success.
        Never raises: model switching must work even when retitling can't.
        """
        if self._app is None or self._chat_id is None:
            return "(couldn't update the group title — no group chat known yet)"
        remaining = int(self._title_flood_until - time.monotonic())
        if remaining > 0:
            return (
                "(Telegram is rate-limiting title changes — "
                f"the group title will lag for {_fmt_wait(remaining)})"
            )
        try:
            chat = await self._app.bot.get_chat(self._chat_id)
            base = self._strip_alias_suffix(chat.title or "")
            if not base:
                return "(couldn't update the group title — this chat has no title)"
            title = base if alias == self._default_model else f"{base} ({alias})"
            if title != chat.title:
                await self._app.bot.set_chat_title(self._chat_id, title)
        except RetryAfter as e:
            retry_after = e.retry_after
            seconds = int(
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else retry_after
            )
            self._title_flood_until = time.monotonic() + seconds
            logger.warning("set_chat_title flood-limited for %ds", seconds)
            return (
                "(Telegram is rate-limiting title changes — "
                f"the group title will lag for {_fmt_wait(seconds)})"
            )
        except Exception:
            logger.warning("Could not update group title", exc_info=True)
            return "(couldn't update the group title — it may lag behind)"
        return None

    async def _reconcile_group_title(self) -> None:
        """Remove a stale alias suffix left by a previous run that died while switched."""
        note = await self._set_group_title(self._current_alias)
        if note:
            logger.info("Group title not reconciled at startup: %s", note)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        voice_line = (
            "• You can send me voice messages too.\n"
            if self._transcriber
            else "• Voice messages are disabled (set GITHUB_TOKEN to enable them).\n"
        )
        await update.message.reply_text(
            "Hi! I'm your personal AI assistant backed by GitHub Copilot.\n\n"
            "• I remember things in markdown files (your vault).\n"
            "• I can set reminders and recurring tasks.\n"
            "• Just talk to me in plain text.\n"
            "• Send me photos — I can look at them and file them in the vault.\n"
            "• Send me files (PDFs, videos, …) — I'll store them in the vault.\n"
            f"{voice_line}"
        )

    def _model_button_label(self, alias: str) -> str:
        label = alias
        if alias == self._default_model:
            label += " (default)"
        if alias == self._current_alias:
            label = f"✓ {label}"
        return label

    async def _clear_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/clear — forget the conversation history of the current chat/topic."""
        if not self._is_allowed(update):
            return
        msg = update.message
        thread_id = msg.message_thread_id
        self._agent.clear_history(msg.chat_id, thread_id=thread_id)
        await msg.reply_text(
            "Context cleared — I've forgotten this conversation. Vault notes are untouched.",
            message_thread_id=thread_id,
        )

    async def _model_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/model — show an inline keyboard of model aliases to pick from."""
        if not self._is_allowed(update):
            return
        msg = update.message
        thread_id = msg.message_thread_id
        if not self._models or self._set_model_fn is None:
            await msg.reply_text("No models configured.", message_thread_id=thread_id)
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    self._model_button_label(alias),
                    callback_data=f"{_MODEL_CB_PREFIX}{alias}",
                )
            ]
            for alias in self._models
        ]
        await msg.reply_text(
            f"Current: {self._current_alias} → {self._models[self._current_alias]}",
            message_thread_id=thread_id,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _model_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Tap on a /model picker button: switch models and edit the picker in place."""
        if not self._is_allowed(update):
            return
        query = update.callback_query
        await query.answer()
        alias = query.data.removeprefix(_MODEL_CB_PREFIX)
        # Stale button: config changed since the picker message was sent
        if not self._models or self._set_model_fn is None or alias not in self._models:
            await query.edit_message_text(f"Unknown model {alias!r}.")
            return

        self._set_model_fn(self._models[alias])
        self._current_alias = alias
        reply = f"Switched to {alias} ({self._models[alias]})."
        note = await self._set_group_title(alias)
        if note:
            reply += f"\n{note}"
        await query.edit_message_text(reply)

    async def _handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        if msg.migrate_to_chat_id is not None:
            self._migrate_chat_id(msg.chat_id, msg.migrate_to_chat_id)
            return
        if msg.migrate_from_chat_id is not None:
            self._migrate_chat_id(msg.migrate_from_chat_id, msg.chat_id)
            return
        if not self._is_allowed(update):
            return
        self._inflight_updates += 1
        try:
            await self._handle_message_inner(update, ctx)
        finally:
            self._inflight_updates -= 1

    async def _handle_message_inner(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        msg = update.message
        self._remember_chat_id(msg.chat_id)

        # Extract forum topic thread ID (None for general chat)
        thread_id: int | None = msg.message_thread_id

        audio = msg.voice or msg.audio
        if audio is not None:
            await self._handle_audio(msg, ctx, audio, thread_id)
            return

        if msg.photo:
            await self._handle_photo(msg, ctx, thread_id)
            return

        attachment = msg.document or msg.video
        if attachment is not None:
            await self._handle_file(msg, ctx, attachment, thread_id)
            return

        # Other non-text messages (stickers, contacts, locations, …)
        if not msg.text:
            await msg.reply_text(
                "text, voice, photos and files only for now", message_thread_id=thread_id
            )
            return

        await self._run_agent_and_reply(msg, ctx, msg.text, thread_id)

    async def _handle_audio(
        self, msg: Message, ctx: ContextTypes.DEFAULT_TYPE, audio, thread_id: int | None
    ) -> None:
        """Transcribe a voice note or audio file and feed the transcript to the agent."""
        if self._transcriber is None:
            await msg.reply_text(
                "Voice messages aren't set up — set GITHUB_TOKEN to a fine-grained "
                "PAT with the `models: read` permission and restart the bot.",
                message_thread_id=thread_id,
            )
            return
        if audio.file_size and audio.file_size > _MAX_DOWNLOAD_BYTES:
            await msg.reply_text(
                "That audio is too large for me (20 MB max).", message_thread_id=thread_id
            )
            return

        await ctx.bot.send_chat_action(
            chat_id=msg.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=thread_id,
        )

        try:
            tg_file = await audio.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            transcript = await self._transcriber.transcribe(data)
        except TranscriptionError as e:
            await msg.reply_text(f"Couldn't transcribe that: {e}", message_thread_id=thread_id)
            return
        except Exception:
            logger.exception("Audio handling failed for chat_id=%d", msg.chat_id)
            await msg.reply_text("Couldn't process that audio, sorry.", message_thread_id=thread_id)
            return

        await msg.reply_text(f"🎙 {transcript}", message_thread_id=thread_id)
        await self._run_agent_and_reply(msg, ctx, transcript, thread_id)

    async def _handle_photo(
        self, msg: Message, ctx: ContextTypes.DEFAULT_TYPE, thread_id: int | None
    ) -> None:
        """Save an incoming photo to the vault and show it to the agent as vision input.

        The caption drives intent: the agent may link the stored file from a
        note, answer a question about the image, or both.
        """
        if self._save_attachment_fn is None:
            await msg.reply_text(
                "Photos aren't enabled.", message_thread_id=thread_id
            )
            return

        # Telegram sends multiple sizes, smallest first — use the largest
        photo = msg.photo[-1]
        if photo.file_size and photo.file_size > _MAX_DOWNLOAD_BYTES:
            await msg.reply_text(
                "That photo is too large for me (20 MB max).", message_thread_id=thread_id
            )
            return

        await ctx.bot.send_chat_action(
            chat_id=msg.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=thread_id,
        )

        try:
            tg_file = await photo.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            stored_path = self._save_attachment_fn(data, "jpg")
        except Exception:
            logger.exception("Photo handling failed for chat_id=%d", msg.chat_id)
            await msg.reply_text(
                "Couldn't process that photo, sorry.", message_thread_id=thread_id
            )
            return

        caption = (msg.caption or "").strip() or "The user sent this image without a caption."
        user_message = (
            f"{caption}\n\n"
            f"[attached image — already stored in the vault at {stored_path}; "
            f"link it from a note if it is worth keeping, otherwise leave it]"
        )
        image_data_url = "data:image/jpeg;base64," + base64.b64encode(data).decode()

        await self._run_agent_and_reply(
            msg, ctx, user_message, thread_id, image_data_url=image_data_url
        )

    async def _handle_file(
        self, msg: Message, ctx: ContextTypes.DEFAULT_TYPE, attachment, thread_id: int | None
    ) -> None:
        """Save a document/video attachment to the vault and hand its path to the agent.

        The model never sees the bytes — only the caption, original name/type
        and the stored path; the caption drives intent, mirroring the photo flow.
        """
        if self._save_attachment_fn is None:
            await msg.reply_text(
                "File attachments aren't enabled.",
                message_thread_id=thread_id,
            )
            return
        if attachment.file_size and attachment.file_size > _MAX_DOWNLOAD_BYTES:
            await msg.reply_text(
                "That file is too large for me (20 MB max).", message_thread_id=thread_id
            )
            return

        await ctx.bot.send_chat_action(
            chat_id=msg.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=thread_id,
        )

        file_name = getattr(attachment, "file_name", None)
        mime_type = getattr(attachment, "mime_type", None)
        try:
            tg_file = await attachment.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            stored_path = self._save_attachment_fn(data, _attachment_ext(file_name, mime_type))
        except Exception:
            logger.exception("File handling failed for chat_id=%d", msg.chat_id)
            await msg.reply_text("Couldn't process that file, sorry.", message_thread_id=thread_id)
            return

        caption = (msg.caption or "").strip() or "The user sent this file without a caption."
        described = file_name or "unnamed file"
        mime_note = f", {mime_type}" if mime_type else ""
        user_message = (
            f"{caption}\n\n"
            f"[attached file: {described}{mime_note} — already stored in the vault at "
            f"{stored_path}; use extract_attachment if you need its contents (never "
            f"read_file, it is binary) — link it from a note if it is worth keeping, "
            f"otherwise leave it]"
        )
        await self._run_agent_and_reply(msg, ctx, user_message, thread_id)

    async def _run_agent_and_reply(
        self,
        msg: Message,
        ctx: ContextTypes.DEFAULT_TYPE,
        text: str,
        thread_id: int | None,
        image_data_url: str | None = None,
    ) -> None:
        reply_context = _reply_context(msg)
        if reply_context:
            text = f"{reply_context}\n{text}"

        # Show typing indicator
        await ctx.bot.send_chat_action(
            chat_id=msg.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=thread_id,
        )

        async def react_to_search() -> None:
            await msg.set_reaction(_SEARCH_REACTION)

        agent_kwargs: dict[str, Any] = {
            "thread_id": thread_id,
            "on_research": react_to_search,
        }
        if image_data_url is not None:
            agent_kwargs["image_data_url"] = image_data_url

        try:
            reply = await self._agent.run(msg.chat_id, text, **agent_kwargs)
        except Exception as e:
            logger.exception("Agent error for chat_id=%d thread_id=%s", msg.chat_id, thread_id)
            reply = f"Sorry, something went wrong: {e}"

        for chunk in _split_message(reply or "(no reply)"):
            await msg.reply_text(chunk, message_thread_id=thread_id)

    def _is_allowed(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        if uid not in self._allowed_user_ids:
            logger.info("Ignoring update from user_id=%s (not allowed)", uid)
            return False
        return True

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def build(self) -> Application:
        app = (
            Application.builder()
            .token(self._token)
            .concurrent_updates(_CONCURRENT_UPDATES)
            .build()
        )
        app.add_handler(CommandHandler("start", self._start))
        app.add_handler(CommandHandler("model", self._model_cmd))
        app.add_handler(CommandHandler("clear", self._clear_cmd))
        app.add_handler(CallbackQueryHandler(self._model_callback, pattern=f"^{_MODEL_CB_PREFIX}"))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self._handle_message))
        self._app = app
        return app

    async def notify_lifecycle(self, text: str) -> None:
        """Best-effort status message; must never break startup or shutdown."""
        if self._chat_id is None:
            # Normal on the first boot with a fresh state dir; not worth a warning.
            logger.info("No chat_id known yet; skipping lifecycle message %r", text)
            return
        try:
            await self.send_message(text)
        except Exception:
            logger.warning("Could not send lifecycle message %r", text, exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle — the process-level orchestration lives in lifecycle.py, so
    # these are the individual steps rather than one run-forever call.
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = self.build()
        await app.initialize()
        # Drop a stale "(alias)" title suffix in case a previous run died while switched
        await self._reconcile_group_title()
        # Declare the command menu (replacing whatever a previous incarnation set);
        # /start stays out of it on purpose
        await app.bot.set_my_commands(
            [
                BotCommand("model", "Pick the model"),
                BotCommand("clear", "Forget the current conversation"),
            ]
        )
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram bot started (long polling)")
        await self.notify_lifecycle("Started")

    def pending_updates(self) -> int:
        """Updates fetched but not yet handled. Zero once the queue is drained.

        Counts both queued updates and ones already picked up by a concurrent
        handler task that has not finished yet.
        """
        if self._app is None:
            return 0
        return self._app.update_queue.qsize() + self._inflight_updates

    async def stop_polling(self) -> None:
        """Stop fetching. Also the ack point — see lifecycle.graceful_shutdown."""
        if self._app is not None:
            await self._app.updater.stop()

    async def drain(self) -> None:
        """Process everything already fetched, then stop handling updates."""
        if self._app is not None:
            await self._app.stop()

    async def close(self) -> None:
        if self._app is not None:
            await self._app.shutdown()
