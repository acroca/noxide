"""Tests for Telegram message handling (text and voice)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from telegram import BotCommand
from telegram.error import InvalidToken, NetworkError, TimedOut

from assistant import telegram_bot
from assistant.telegram_bot import TelegramBot
from assistant.transcribe import TranscriptionError

_USER_ID = 42


def _bot(
    transcriber: MagicMock | None = None,
    save_attachment_fn: MagicMock | None = None,
) -> tuple[TelegramBot, AsyncMock]:
    agent = MagicMock()
    agent.run = AsyncMock(return_value="agent reply")
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        transcriber=transcriber,
        save_attachment_fn=save_attachment_fn,
    )
    return bot, agent


def _transcriber(text: str = "hello world") -> MagicMock:
    t = MagicMock()
    t.transcribe = AsyncMock(return_value=text)
    return t


def _voice(data: bytes = b"OGGBYTES", file_size: int = 4096) -> MagicMock:
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    voice = MagicMock()
    voice.file_size = file_size
    voice.get_file = AsyncMock(return_value=tg_file)
    return voice


def _photo_sizes(data: bytes = b"JPEGBYTES", file_size: int = 2048) -> tuple[MagicMock, ...]:
    """Mimic Telegram's PhotoSize tuple, smallest first."""
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    small = MagicMock()
    small.file_size = 128
    small.get_file = AsyncMock()
    large = MagicMock()
    large.file_size = file_size
    large.get_file = AsyncMock(return_value=tg_file)
    return (small, large)


def _update(
    text: str | None = None,
    voice: MagicMock | None = None,
    photo: tuple[MagicMock, ...] = (),
    caption: str | None = None,
    document: MagicMock | None = None,
    video: MagicMock | None = None,
    reply_to: MagicMock | None = None,
    quote: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.text = text
    msg.voice = voice
    msg.audio = None
    msg.photo = photo
    msg.caption = caption
    msg.document = document
    msg.video = video
    msg.chat_id = 777
    msg.message_thread_id = None
    msg.migrate_to_chat_id = None
    msg.migrate_from_chat_id = None
    msg.reply_to_message = reply_to
    msg.quote = quote
    msg.reply_text = AsyncMock()
    msg.set_reaction = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_user.id = _USER_ID
    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    return update, ctx


def _replies(msg: MagicMock) -> list[str]:
    return [c.args[0] for c in msg.reply_text.call_args_list]


# ------------------------------------------------------------------
# Text messages (existing behavior)
# ------------------------------------------------------------------

async def test_text_message_runs_agent_and_replies() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="remind me tomorrow")

    await bot._handle_message(update, ctx)

    agent.run.assert_awaited_once_with(777, "remind me tomorrow", thread_id=None, on_research=ANY)
    assert _replies(update.message) == ["agent reply"]


# ------------------------------------------------------------------
# Copilot outage: failed messages are queued for retry
# ------------------------------------------------------------------

async def test_outage_queues_message_and_tells_user() -> None:
    from assistant.copilot import CopilotUnavailableError

    agent = MagicMock()
    agent.run = AsyncMock(side_effect=CopilotUnavailableError("HTTP 502"))
    queue_fn = MagicMock()
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        queue_message_fn=queue_fn,
    )
    update, ctx = _update(text="pastilla tomada")

    await bot._handle_message(update, ctx)

    queue_fn.assert_called_once_with(777, None, "pastilla tomada")
    (reply,) = _replies(update.message)
    assert "queued" in reply
    assert "Copilot" in reply


async def test_outage_without_queue_keeps_generic_error_reply() -> None:
    from assistant.copilot import CopilotUnavailableError

    bot, agent = _bot()
    agent.run = AsyncMock(side_effect=CopilotUnavailableError("HTTP 502"))
    update, ctx = _update(text="hola")

    await bot._handle_message(update, ctx)

    (reply,) = _replies(update.message)
    assert reply.startswith("Sorry, something went wrong")


# ------------------------------------------------------------------
# Replies (Telegram reply-to context)
# ------------------------------------------------------------------

def _reply_msg(
    text: str | None = None,
    caption: str | None = None,
    forum_topic_created: MagicMock | None = None,
) -> MagicMock:
    reply = MagicMock()
    reply.text = text
    reply.caption = caption
    reply.forum_topic_created = forum_topic_created
    return reply


async def test_reply_prepends_quoted_message_to_agent_text() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="yes, do that one", reply_to=_reply_msg(text="buy oat milk"))

    await bot._handle_message(update, ctx)

    sent = agent.run.call_args.args[1]
    assert sent == '[replying to: "buy oat milk"]\nyes, do that one'


async def test_reply_partial_quote_wins_over_full_message() -> None:
    quote = MagicMock()
    quote.text = "the second option"
    update, ctx = _update(
        text="this one",
        reply_to=_reply_msg(text="first option ... the second option ... third"),
        quote=quote,
    )
    bot, agent = _bot()

    await bot._handle_message(update, ctx)

    sent = agent.run.call_args.args[1]
    assert sent == '[replying to: "the second option"]\nthis one'


async def test_reply_to_media_falls_back_to_caption() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="file it", reply_to=_reply_msg(caption="wine invoice"))

    await bot._handle_message(update, ctx)

    sent = agent.run.call_args.args[1]
    assert sent == '[replying to: "wine invoice"]\nfile it'


async def test_reply_without_any_text_adds_no_context() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="what is this?", reply_to=_reply_msg())

    await bot._handle_message(update, ctx)

    agent.run.assert_awaited_once_with(777, "what is this?", thread_id=None, on_research=ANY)


async def test_reply_quote_is_truncated() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="summarize", reply_to=_reply_msg(text="x" * 500))

    await bot._handle_message(update, ctx)

    sent = agent.run.call_args.args[1]
    assert sent == f'[replying to: "{"x" * 300}…"]\nsummarize'


async def test_forum_topic_service_message_is_not_a_reply() -> None:
    """In forum topics every plain message 'replies to' the topic-creation
    service message — that must not be treated as a user reply."""
    bot, agent = _bot()
    reply = _reply_msg(text=None, forum_topic_created=MagicMock())
    update, ctx = _update(text="plain topic message", reply_to=reply)
    update.message.message_thread_id = 55

    await bot._handle_message(update, ctx)

    agent.run.assert_awaited_once_with(
        777, "plain topic message", thread_id=55, on_research=ANY
    )


async def test_reply_context_applies_to_photo_captions() -> None:
    bot, agent = _bot(save_attachment_fn=_save_fn())
    update, ctx = _update(
        photo=_photo_sizes(),
        caption="here is the label",
        reply_to=_reply_msg(text="which wine was it?"),
    )

    await bot._handle_message(update, ctx)

    sent = agent.run.call_args.args[1]
    assert sent.startswith('[replying to: "which wine was it?"]\n')
    assert "here is the label" in sent


# ------------------------------------------------------------------
# Web-search reaction
# ------------------------------------------------------------------

async def test_research_reacts_to_triggering_message() -> None:
    from telegram.constants import ReactionEmoji

    bot, agent = _bot()

    async def run_with_research(chat_id: int, text: str, **kwargs) -> str:
        await kwargs["on_research"]()
        return "agent reply"

    agent.run = AsyncMock(side_effect=run_with_research)
    update, ctx = _update(text="what's the weather in Girona?")

    await bot._handle_message(update, ctx)

    update.message.set_reaction.assert_awaited_once_with(ReactionEmoji.EYES)
    assert _replies(update.message) == ["agent reply"]


async def test_no_reaction_when_agent_does_not_research() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="hello")

    await bot._handle_message(update, ctx)

    update.message.set_reaction.assert_not_awaited()


# ------------------------------------------------------------------
# Voice messages
# ------------------------------------------------------------------

async def test_voice_message_is_transcribed_and_fed_to_agent() -> None:
    transcriber = _transcriber("buy milk tomorrow")
    bot, agent = _bot(transcriber)
    update, ctx = _update(voice=_voice(b"OGGBYTES"))

    await bot._handle_message(update, ctx)

    transcriber.transcribe.assert_awaited_once_with(b"OGGBYTES")
    agent.run.assert_awaited_once_with(777, "buy milk tomorrow", thread_id=None, on_research=ANY)
    replies = _replies(update.message)
    assert replies[0] == "🎙 buy milk tomorrow"
    assert replies[1] == "agent reply"


async def test_voice_in_forum_topic_replies_into_topic() -> None:
    transcriber = _transcriber("topic note")
    bot, agent = _bot(transcriber)
    update, ctx = _update(voice=_voice())
    update.message.message_thread_id = 55

    await bot._handle_message(update, ctx)

    agent.run.assert_awaited_once_with(777, "topic note", thread_id=55, on_research=ANY)
    for call in update.message.reply_text.call_args_list:
        assert call.kwargs["message_thread_id"] == 55


async def test_voice_without_transcriber_replies_setup_hint() -> None:
    bot, agent = _bot(transcriber=None)
    update, ctx = _update(voice=_voice())

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    assert "GITHUB_TOKEN" in _replies(update.message)[0]


async def test_voice_transcription_error_replies_and_skips_agent() -> None:
    transcriber = MagicMock()
    transcriber.transcribe = AsyncMock(side_effect=TranscriptionError("rate limit reached"))
    bot, agent = _bot(transcriber)
    update, ctx = _update(voice=_voice())

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    assert "rate limit reached" in _replies(update.message)[0]


async def test_voice_too_large_is_rejected_before_download() -> None:
    transcriber = _transcriber()
    bot, agent = _bot(transcriber)
    voice = _voice(file_size=25 * 1024 * 1024)
    update, ctx = _update(voice=voice)

    await bot._handle_message(update, ctx)

    voice.get_file.assert_not_awaited()
    transcriber.transcribe.assert_not_awaited()
    agent.run.assert_not_awaited()
    assert "too large" in _replies(update.message)[0].lower()


# ------------------------------------------------------------------
# Other non-text messages
# ------------------------------------------------------------------

async def test_sticker_message_gets_fallback_reply() -> None:
    bot, agent = _bot(_transcriber())
    update, ctx = _update(text=None, voice=None)  # e.g. a sticker or video

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    assert len(_replies(update.message)) == 1


async def test_disallowed_user_is_ignored() -> None:
    bot, agent = _bot(_transcriber())
    update, ctx = _update(voice=_voice())
    update.effective_user.id = 999

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


# ------------------------------------------------------------------
# Photo messages
# ------------------------------------------------------------------

def _save_fn(path: str = "attachments/2026-07-21-abc123.jpg") -> MagicMock:
    return MagicMock(return_value=path)


async def test_photo_is_saved_and_fed_to_agent_with_vision() -> None:
    import base64

    save_fn = _save_fn()
    bot, agent = _bot(save_attachment_fn=save_fn)
    update, ctx = _update(photo=_photo_sizes(b"JPEGBYTES"), caption="save this receipt")

    await bot._handle_message(update, ctx)

    save_fn.assert_called_once_with(b"JPEGBYTES", "jpg")
    agent.run.assert_awaited_once()
    call = agent.run.call_args
    assert call.args[0] == 777
    assert "save this receipt" in call.args[1]
    assert "attachments/2026-07-21-abc123.jpg" in call.args[1]
    expected_url = "data:image/jpeg;base64," + base64.b64encode(b"JPEGBYTES").decode()
    assert call.kwargs["image_data_url"] == expected_url
    assert call.kwargs["thread_id"] is None
    assert _replies(update.message) == ["agent reply"]


async def test_photo_downloads_largest_size() -> None:
    photo = _photo_sizes()
    bot, agent = _bot(save_attachment_fn=_save_fn())
    update, ctx = _update(photo=photo)

    await bot._handle_message(update, ctx)

    photo[-1].get_file.assert_awaited_once()
    photo[0].get_file.assert_not_awaited()


async def test_photo_without_caption_gets_default_message() -> None:
    bot, agent = _bot(save_attachment_fn=_save_fn())
    update, ctx = _update(photo=_photo_sizes(), caption=None)

    await bot._handle_message(update, ctx)

    agent.run.assert_awaited_once()
    user_message = agent.run.call_args.args[1]
    assert user_message.strip() != ""
    assert "attachments/" in user_message


async def test_photo_without_save_fn_gets_fallback_reply() -> None:
    bot, agent = _bot(save_attachment_fn=None)
    update, ctx = _update(photo=_photo_sizes())

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    assert len(_replies(update.message)) == 1


def _mock_app() -> MagicMock:
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.bot.set_my_commands = AsyncMock()
    return app


async def test_start_registers_model_command_menu() -> None:
    bot, _ = _bot()
    app = _mock_app()
    bot.build = MagicMock(return_value=app)

    await bot.start()

    app.bot.set_my_commands.assert_awaited_once_with(
        [
            BotCommand("model", "Pick the model"),
            BotCommand("clear", "Forget the current conversation"),
        ]
    )


# ------------------------------------------------------------------
# Startup network resilience — a host reboot can leave DNS down for
# minutes; startup must wait it out instead of crash-looping.
# ------------------------------------------------------------------


async def test_start_retries_initialize_while_network_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_bot, "_STARTUP_RETRY_INITIAL", 0.001)
    bot, _ = _bot()
    app = _mock_app()
    app.initialize = AsyncMock(side_effect=[NetworkError("dns down"), TimedOut(), None])
    bot.build = MagicMock(return_value=app)

    await bot.start()

    assert app.initialize.await_count == 3
    app.updater.start_polling.assert_awaited_once()


async def test_start_retries_command_menu_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_bot, "_STARTUP_RETRY_INITIAL", 0.001)
    bot, _ = _bot()
    app = _mock_app()
    app.bot.set_my_commands = AsyncMock(side_effect=[NetworkError("flap"), None])
    bot.build = MagicMock(return_value=app)

    await bot.start()

    assert app.bot.set_my_commands.await_count == 2
    app.updater.start_polling.assert_awaited_once()


async def test_start_polling_retries_bootstrap_indefinitely() -> None:
    bot, _ = _bot()
    app = _mock_app()
    bot.build = MagicMock(return_value=app)

    await bot.start()

    assert app.updater.start_polling.await_args.kwargs["bootstrap_retries"] == -1


async def test_start_gives_up_when_shutdown_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_bot, "_STARTUP_RETRY_INITIAL", 0.001)
    bot, _ = _bot()
    app = _mock_app()
    app.initialize = AsyncMock(side_effect=NetworkError("dns down"))
    bot.build = MagicMock(return_value=app)
    abort = asyncio.Event()
    abort.set()

    await bot.start(abort=abort)

    app.start.assert_not_awaited()
    app.updater.start_polling.assert_not_awaited()


async def test_start_does_not_retry_non_network_errors() -> None:
    bot, _ = _bot()
    app = _mock_app()
    app.initialize = AsyncMock(side_effect=InvalidToken("bad token"))
    bot.build = MagicMock(return_value=app)

    with pytest.raises(InvalidToken):
        await bot.start()

    assert app.initialize.await_count == 1


# ------------------------------------------------------------------
# File attachments (documents, videos)
# ------------------------------------------------------------------


def _file_attachment(
    file_name: str | None = "report.pdf",
    mime_type: str | None = "application/pdf",
    data: bytes = b"PDFBYTES",
    file_size: int = 2048,
) -> MagicMock:
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    attachment = MagicMock()
    attachment.file_name = file_name
    attachment.mime_type = mime_type
    attachment.file_size = file_size
    attachment.get_file = AsyncMock(return_value=tg_file)
    return attachment


def test_attachment_ext_prefers_filename() -> None:
    from assistant.telegram_bot import _attachment_ext

    assert _attachment_ext("Report.V2.PDF", "application/octet-stream") == "PDF"
    assert _attachment_ext(None, "application/pdf") == "pdf"
    assert _attachment_ext(None, None) == "bin"
    assert _attachment_ext("noextension", None) == "bin"


async def test_pdf_document_is_saved_and_fed_to_agent() -> None:
    save_fn = MagicMock(return_value="attachments/2026-07-22-abc123.pdf")
    bot, agent = _bot(save_attachment_fn=save_fn)
    update, ctx = _update(document=_file_attachment(), caption="file this receipt")

    await bot._handle_message(update, ctx)

    save_fn.assert_called_once_with(b"PDFBYTES", "pdf")
    agent.run.assert_awaited_once()
    user_message = agent.run.call_args.args[1]
    assert "file this receipt" in user_message
    assert "report.pdf" in user_message
    assert "attachments/2026-07-22-abc123.pdf" in user_message
    assert _replies(update.message) == ["agent reply"]


async def test_video_extension_falls_back_to_mime_type() -> None:
    save_fn = MagicMock(return_value="attachments/2026-07-22-abc123.mp4")
    bot, agent = _bot(save_attachment_fn=save_fn)
    video = _file_attachment(file_name=None, mime_type="video/mp4", data=b"MP4BYTES")
    update, ctx = _update(video=video, caption="save this clip")

    await bot._handle_message(update, ctx)

    save_fn.assert_called_once_with(b"MP4BYTES", "mp4")
    agent.run.assert_awaited_once()


async def test_document_without_caption_gets_default_message() -> None:
    save_fn = MagicMock(return_value="attachments/2026-07-22-abc123.pdf")
    bot, agent = _bot(save_attachment_fn=save_fn)
    update, ctx = _update(document=_file_attachment(), caption=None)

    await bot._handle_message(update, ctx)

    user_message = agent.run.call_args.args[1]
    assert user_message.strip() != ""
    assert "attachments/" in user_message


async def test_document_too_large_is_rejected_before_download() -> None:
    save_fn = MagicMock()
    bot, agent = _bot(save_attachment_fn=save_fn)
    doc = _file_attachment(file_size=25 * 1024 * 1024)
    update, ctx = _update(document=doc)

    await bot._handle_message(update, ctx)

    doc.get_file.assert_not_awaited()
    save_fn.assert_not_called()
    agent.run.assert_not_awaited()
    assert "too large" in _replies(update.message)[0].lower()


async def test_document_without_save_fn_gets_fallback_reply() -> None:
    bot, agent = _bot(save_attachment_fn=None)
    update, ctx = _update(document=_file_attachment())

    await bot._handle_message(update, ctx)

    agent.run.assert_not_awaited()
    assert len(_replies(update.message)) == 1


async def test_document_in_forum_topic_threads_replies() -> None:
    save_fn = MagicMock(return_value="attachments/2026-07-22-abc123.pdf")
    bot, agent = _bot(save_attachment_fn=save_fn)
    update, ctx = _update(document=_file_attachment(), caption="wine invoice")
    update.message.message_thread_id = 55

    await bot._handle_message(update, ctx)

    assert agent.run.call_args.kwargs["thread_id"] == 55
    for call in update.message.reply_text.call_args_list:
        assert call.kwargs["message_thread_id"] == 55


async def test_photo_in_forum_topic_threads_replies() -> None:
    bot, agent = _bot(save_attachment_fn=_save_fn())
    update, ctx = _update(photo=_photo_sizes(), caption="wine label")
    update.message.message_thread_id = 55

    await bot._handle_message(update, ctx)

    assert agent.run.call_args.kwargs["thread_id"] == 55
    for call in update.message.reply_text.call_args_list:
        assert call.kwargs["message_thread_id"] == 55


# ------------------------------------------------------------------
# Model selection
# ------------------------------------------------------------------

_MODELS = {"sonnet": "claude-sonnet-4.6", "opus": "claude-opus-41"}


def _model_bot(title: str = "Family") -> tuple[TelegramBot, MagicMock]:
    agent = MagicMock()
    agent.run = AsyncMock(return_value="agent reply")
    set_model_fn = MagicMock()
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        models=_MODELS,
        default_model="sonnet",
        set_model_fn=set_model_fn,
    )
    bot._chat_id = 777
    bot._app = MagicMock()
    chat = MagicMock()
    chat.title = title
    bot._app.bot.get_chat = AsyncMock(return_value=chat)
    bot._app.bot.set_chat_title = AsyncMock()
    return bot, set_model_fn


def _mock_app(title: str = "Family") -> MagicMock:
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.bot.set_my_commands = AsyncMock()
    app.bot.send_message = AsyncMock()
    chat = MagicMock()
    chat.title = title
    app.bot.get_chat = AsyncMock(return_value=chat)
    app.bot.set_chat_title = AsyncMock()
    return app


def test_strip_alias_suffix_removes_known_alias() -> None:
    bot, _ = _model_bot()
    assert bot._strip_alias_suffix("Family (opus)") == "Family"


def test_strip_alias_suffix_keeps_unknown_suffix() -> None:
    bot, _ = _model_bot()
    assert bot._strip_alias_suffix("Family (dev)") == "Family (dev)"


def test_strip_alias_suffix_without_suffix() -> None:
    bot, _ = _model_bot()
    assert bot._strip_alias_suffix("Family") == "Family"


# ------------------------------------------------------------------
# Lifecycle notifications ("Started" / "Restarting...") and chat_id persistence
# ------------------------------------------------------------------


def _sent_texts(app: MagicMock) -> list[str]:
    return [c.kwargs["text"] for c in app.bot.send_message.call_args_list]


def _lifecycle_bot(chat_id: int | None = 777) -> tuple[TelegramBot, MagicMock]:
    bot, _ = _bot()
    bot._chat_id = chat_id
    app = _mock_app()
    bot.build = MagicMock(return_value=app)
    bot._app = app
    return bot, app


async def test_start_notifies_started() -> None:
    bot, app = _lifecycle_bot()

    await bot.start()

    assert _sent_texts(app) == ["Started"]


async def test_shutdown_steps_map_to_the_application() -> None:
    bot, app = _lifecycle_bot()
    await bot.start()

    await bot.stop_polling()
    await bot.drain()
    await bot.close()

    app.updater.stop.assert_awaited_once()
    app.stop.assert_awaited_once()
    app.shutdown.assert_awaited_once()


async def test_shutdown_steps_are_noops_before_start() -> None:
    """A failed startup must not turn shutdown into a second crash."""
    bot, _ = _bot()

    assert bot.pending_updates() == 0
    await bot.stop_polling()
    await bot.drain()
    await bot.close()


async def test_pending_updates_reports_queue_depth() -> None:
    bot, app = _lifecycle_bot()
    app.update_queue.qsize = MagicMock(return_value=4)
    await bot.start()

    assert bot.pending_updates() == 4


async def test_send_message_returns_delivered_chat_id() -> None:
    """The sender reports where it delivered, so the agent can mirror
    scheduled-run messages into that conversation's history."""
    bot, app = _lifecycle_bot()
    await bot.start()

    assert await bot.send_message("hola") == 777


async def test_send_message_returns_none_when_dropped() -> None:
    bot, _ = _lifecycle_bot(chat_id=None)
    await bot.start()

    assert await bot.send_message("hola") is None


async def test_send_message_chat_id_override_bypasses_pinned_chat() -> None:
    """Retry-queue replays deliver to the conversation the message came from,
    which needs a chat other than the pinned one to be addressable."""
    bot, app = _lifecycle_bot()
    await bot.start()

    result = await bot.send_message("hola", chat_id=555)

    assert result == 555
    assert app.bot.send_message.call_args.kwargs["chat_id"] == 555


async def test_lifecycle_messages_dropped_without_chat_id() -> None:
    bot, app = _lifecycle_bot(chat_id=None)

    await bot.start()

    app.bot.send_message.assert_not_awaited()


async def test_lifecycle_skip_without_chat_id_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    bot, app = _lifecycle_bot(chat_id=None)

    with caplog.at_level(logging.INFO, logger="assistant.telegram_bot"):
        await bot.notify_lifecycle("Started")

    app.bot.send_message.assert_not_awaited()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("skipping lifecycle" in r.getMessage() for r in caplog.records)


async def test_notify_failure_does_not_block_startup() -> None:
    bot, app = _lifecycle_bot()
    app.bot.send_message = AsyncMock(side_effect=Exception("network down"))

    await bot.start()

    app.updater.start_polling.assert_awaited_once()


async def test_chat_id_is_persisted_after_message(tmp_path: Path) -> None:
    agent = MagicMock()
    agent.run = AsyncMock(return_value="agent reply")
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        state_dir=tmp_path,
    )
    update, ctx = _update(text="hello")

    await bot._handle_message(update, ctx)

    assert (tmp_path / "chat_id").read_text() == "777"


async def test_pinned_chat_id_is_not_retargeted_by_later_messages(tmp_path: Path) -> None:
    (tmp_path / "chat_id").write_text("555")
    agent = MagicMock()
    agent.run = AsyncMock(return_value="agent reply")
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        state_dir=tmp_path,
    )
    update, ctx = _update(text="hello from another chat")

    await bot._handle_message(update, ctx)

    assert bot._chat_id == 555
    assert (tmp_path / "chat_id").read_text() == "555"
    agent.run.assert_awaited_once_with(
        777, "hello from another chat", thread_id=None, on_research=ANY
    )
    assert _replies(update.message) == ["agent reply"]


async def test_pinned_chat_id_follows_telegram_migration(tmp_path: Path) -> None:
    (tmp_path / "chat_id").write_text("777")
    agent = MagicMock()
    agent.run = AsyncMock()
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=agent,
        state_dir=tmp_path,
    )
    update, ctx = _update()
    update.effective_user = None
    update.message.migrate_to_chat_id = 888

    await bot._handle_message(update, ctx)

    assert bot._chat_id == 888
    assert (tmp_path / "chat_id").read_text() == "888"
    agent.run.assert_not_awaited()


async def test_unrelated_telegram_migration_cannot_retarget_home_chat(
    tmp_path: Path,
) -> None:
    (tmp_path / "chat_id").write_text("555")
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=MagicMock(),
        state_dir=tmp_path,
    )
    update, ctx = _update()
    update.effective_user = None
    update.message.migrate_to_chat_id = 888

    await bot._handle_message(update, ctx)

    assert bot._chat_id == 555
    assert (tmp_path / "chat_id").read_text() == "555"


def test_chat_id_is_restored_from_state_dir(tmp_path: Path) -> None:
    (tmp_path / "chat_id").write_text("777")

    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=MagicMock(),
        state_dir=tmp_path,
    )

    assert bot._chat_id == 777


def test_default_chat_id_used_when_nothing_persisted(tmp_path: Path) -> None:
    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=MagicMock(),
        state_dir=tmp_path,
        default_chat_id=555,
    )

    assert bot._chat_id == 555


def test_persisted_chat_id_wins_over_default(tmp_path: Path) -> None:
    (tmp_path / "chat_id").write_text("777")

    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=MagicMock(),
        state_dir=tmp_path,
        default_chat_id=555,
    )

    assert bot._chat_id == 777


def test_corrupt_chat_id_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "chat_id").write_text("not-a-number")

    bot = TelegramBot(
        token="tg-token",
        allowed_user_ids=[_USER_ID],
        agent=MagicMock(),
        state_dir=tmp_path,
    )

    assert bot._chat_id is None


# ------------------------------------------------------------------
# /model command (inline-keyboard picker)
# ------------------------------------------------------------------

def _callback_update(data: str) -> tuple[MagicMock, MagicMock]:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = _USER_ID
    ctx = MagicMock()
    return update, ctx


def _picker_buttons(msg: MagicMock) -> list[tuple[str, str]]:
    """(text, callback_data) per button of the picker sent via reply_text."""
    markup = msg.reply_text.call_args.kwargs["reply_markup"]
    return [(btn.text, btn.callback_data) for row in markup.inline_keyboard for btn in row]


async def test_model_command_shows_picker() -> None:
    bot, set_model_fn = _model_bot()
    update, ctx = _update(text="/model")
    ctx.args = []

    await bot._model_cmd(update, ctx)

    assert _replies(update.message) == ["Current: sonnet → claude-sonnet-4.6"]
    assert _picker_buttons(update.message) == [
        ("✓ sonnet (default)", "model:sonnet"),
        ("opus", "model:opus"),
    ]
    set_model_fn.assert_not_called()


async def test_model_command_ignores_arguments() -> None:
    bot, set_model_fn = _model_bot()
    update, ctx = _update(text="/model opus")
    ctx.args = ["opus"]

    await bot._model_cmd(update, ctx)

    set_model_fn.assert_not_called()
    assert len(_picker_buttons(update.message)) == 2


async def test_model_command_ignores_disallowed_user() -> None:
    bot, set_model_fn = _model_bot()
    update, ctx = _update(text="/model")
    ctx.args = []
    update.effective_user.id = 999

    await bot._model_cmd(update, ctx)

    set_model_fn.assert_not_called()
    update.message.reply_text.assert_not_awaited()


async def test_model_command_without_models_configured() -> None:
    bot, _ = _bot()  # no models / set_model_fn wired
    update, ctx = _update(text="/model")
    ctx.args = []

    await bot._model_cmd(update, ctx)

    assert "No models configured" in _replies(update.message)[0]


async def test_model_callback_switches_and_retitles_group() -> None:
    bot, set_model_fn = _model_bot()
    update, _ctx = _callback_update("model:opus")

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_called_once_with("claude-opus-41")
    assert bot._current_alias == "opus"
    bot._app.bot.set_chat_title.assert_awaited_once_with(777, "Family (opus)")
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "Switched to opus (claude-opus-41)."
    )


async def test_model_callback_back_to_default_restores_plain_title() -> None:
    bot, set_model_fn = _model_bot(title="Family (opus)")
    bot._current_alias = "opus"
    update, _ctx = _callback_update("model:sonnet")

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_called_once_with("claude-sonnet-4.6")
    bot._app.bot.set_chat_title.assert_awaited_once_with(777, "Family")


async def test_model_callback_skips_noop_retitle() -> None:
    bot, _ = _model_bot(title="Family (opus)")
    update, _ctx = _callback_update("model:opus")

    await bot._model_callback(update, _ctx)

    bot._app.bot.set_chat_title.assert_not_awaited()
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "Switched to opus (claude-opus-41)."
    )


async def test_model_callback_switch_survives_retitle_failure() -> None:
    bot, set_model_fn = _model_bot()
    bot._app.bot.set_chat_title = AsyncMock(side_effect=Exception("boom"))
    update, _ctx = _callback_update("model:opus")

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_called_once_with("claude-opus-41")
    edited = update.callback_query.edit_message_text.call_args.args[0]
    assert "Switched to opus" in edited
    assert "group title" in edited


async def test_model_callback_flood_wait_skips_api_until_window_ends() -> None:
    from telegram.error import RetryAfter

    bot, _ = _model_bot()
    bot._app.bot.set_chat_title = AsyncMock(side_effect=RetryAfter(65299))
    update, _ctx = _callback_update("model:opus")

    await bot._model_callback(update, _ctx)

    edited = update.callback_query.edit_message_text.call_args.args[0]
    assert "Switched to opus" in edited
    assert "rate-limiting" in edited
    assert "~19h" in edited

    # The flood window is remembered: the next switch must not touch the API
    update2, _ctx2 = _callback_update("model:sonnet")
    await bot._model_callback(update2, _ctx2)

    bot._app.bot.get_chat.assert_awaited_once()
    assert bot._app.bot.set_chat_title.await_count == 1
    edited2 = update2.callback_query.edit_message_text.call_args.args[0]
    assert "Switched to sonnet" in edited2
    assert "rate-limiting" in edited2


async def test_model_callback_without_chat_id_notes_title_lag() -> None:
    bot, set_model_fn = _model_bot()
    bot._chat_id = None
    update, _ctx = _callback_update("model:opus")

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_called_once_with("claude-opus-41")
    edited = update.callback_query.edit_message_text.call_args.args[0]
    assert "Switched to opus" in edited
    assert "group title" in edited


async def test_reconcile_strips_stale_alias_suffix_from_group_title() -> None:
    bot, _ = _model_bot(title="Family (opus)")

    await bot._reconcile_group_title()

    bot._app.bot.set_chat_title.assert_awaited_once_with(777, "Family")


async def test_reconcile_leaves_clean_title_alone() -> None:
    bot, _ = _model_bot(title="Family")

    await bot._reconcile_group_title()

    bot._app.bot.set_chat_title.assert_not_awaited()


async def test_start_reconciles_group_title() -> None:
    bot, app = _lifecycle_bot()
    bot._reconcile_group_title = AsyncMock()

    await bot.start()

    bot._reconcile_group_title.assert_awaited_once()


async def test_model_callback_rejects_stale_alias() -> None:
    bot, set_model_fn = _model_bot()
    update, _ctx = _callback_update("model:gpt9")

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_not_called()
    bot._app.bot.set_chat_title.assert_not_awaited()
    edited = update.callback_query.edit_message_text.call_args.args[0]
    assert "gpt9" in edited


async def test_model_callback_ignores_disallowed_user() -> None:
    bot, set_model_fn = _model_bot()
    update, _ctx = _callback_update("model:opus")
    update.effective_user.id = 999

    await bot._model_callback(update, _ctx)

    set_model_fn.assert_not_called()
    update.callback_query.answer.assert_not_awaited()
    update.callback_query.edit_message_text.assert_not_awaited()


def test_build_registers_model_command_and_callback() -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    bot, _ = _model_bot()

    app = bot.build()

    commands = {
        cmd
        for handler in app.handlers[0]
        if isinstance(handler, CommandHandler)
        for cmd in handler.commands
    }
    assert "model" in commands
    assert any(isinstance(h, CallbackQueryHandler) for h in app.handlers[0])


# ------------------------------------------------------------------
# /clear command
# ------------------------------------------------------------------

async def test_clear_command_clears_current_history_and_confirms() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="/clear")

    await bot._clear_cmd(update, ctx)

    agent.clear_history.assert_called_once_with(777, thread_id=None)
    assert "cleared" in _replies(update.message)[0].lower()


async def test_clear_command_in_forum_topic_clears_only_that_topic() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="/clear")
    update.message.message_thread_id = 5

    await bot._clear_cmd(update, ctx)

    agent.clear_history.assert_called_once_with(777, thread_id=5)
    assert update.message.reply_text.call_args.kwargs.get("message_thread_id") == 5


async def test_clear_command_ignores_disallowed_user() -> None:
    bot, agent = _bot()
    update, ctx = _update(text="/clear")
    update.effective_user.id = 999

    await bot._clear_cmd(update, ctx)

    agent.clear_history.assert_not_called()
    update.message.reply_text.assert_not_awaited()


def test_build_registers_clear_command() -> None:
    from telegram.ext import CommandHandler

    bot, _ = _bot()

    app = bot.build()

    commands = {
        cmd
        for handler in app.handlers[0]
        if isinstance(handler, CommandHandler)
        for cmd in handler.commands
    }
    assert "clear" in commands


# ------------------------------------------------------------------
# Concurrent update processing
# ------------------------------------------------------------------

def test_build_enables_concurrent_updates() -> None:
    """Updates run concurrently so one topic's long agent run does not block
    another topic; per-conversation ordering is Agent.run's job."""
    bot, _ = _bot()

    app = bot.build()

    assert app.concurrent_updates == 8


async def test_pending_updates_counts_inflight_handlers() -> None:
    """With concurrent updates the queue drains into tasks immediately, so the
    shutdown drain report must include handlers still running."""
    bot, agent = _bot()
    app = MagicMock()
    app.update_queue.qsize = MagicMock(return_value=1)
    bot._app = app
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(*args, **kwargs):
        started.set()
        await release.wait()
        return "done"

    agent.run = AsyncMock(side_effect=slow_run)
    update, ctx = _update(text="hi")

    task = asyncio.create_task(bot._handle_message(update, ctx))
    await started.wait()
    assert bot.pending_updates() == 2  # 1 still queued + 1 in flight

    release.set()
    await task
    assert bot.pending_updates() == 1  # only the queued one remains
