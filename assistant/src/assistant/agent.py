"""Agent loop: OpenAI-style tool-calling against GitHub Copilot API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from . import copilot, usage
from .backup import VaultBackup
from .conversations import WEB_CHAT_ID, ConversationArchive, conversation_space
from .history import ConversationHistory, history_tool_schemas
from .skills import SkillLibrary
from .tools import VaultTools

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 20
# Returned as the run's reply when the loop is abandoned at the iteration cap.
# Public so callers that must not mistake an abandoned run for a completed one
# (inbox ingestion clears processed entries) can recognize it.
MAX_ITERATIONS_REPLY = "[Reached maximum tool-call iterations. Please rephrase your request.]"
_TOOL_TIMEOUT = 60.0
# Tools that make their own model calls get longer budgets: research and
# extraction run one sub-agent/vision call, fan_out runs a whole batch of
# concurrent workers (its own per-worker timeouts keep one stuck item from
# consuming the budget alone).
_TOOL_TIMEOUTS = {
    "research": 300.0,
    "extract_attachment": 300.0,
    "fan_out": 1800.0,
}

# Failed tool results all use a bracketed sentinel prefix ("[tool error: ...]",
# "[file not found: ...]", "[move error: ...]", ...). Matching them here gives
# every failed call a WARNING — error strings only go back to the model, so a
# failure it silently works around is otherwise invisible to operators (a
# production move_file failure left zero log trace).
_ERROR_RESULT_RX = re.compile(
    r"^\[[^\]\n]*\b(error|not found|timed out|denied|unknown tool)\b"
)

# A scheduled run replies with this sentinel (prompts/schedule.md) when it
# finds its purpose already met; run_job then delivers nothing.
_SILENT_SENTINEL = "[silent]"

# A proactive sender: delivers text to the home chat and returns the chat id
# it delivered to, or None when the message was dropped. The chat id is how
# run_job learns the real conversation key when mirroring a delivery into
# that conversation's history.
SendMessageFn = Callable[[str], Coroutine[Any, Any, int | None]]

# Scheduled runs close with a JSON object matching this schema (the contract
# in prompts/schedule.md). It also rides job-run requests as response_format:
# Copilot currently ignores it (verified 2026-07-29 — accepted, not enforced,
# for every model the bot can use), so the tolerant parse in run_job is the
# real mechanism; if the endpoint ever starts enforcing it, conformance
# arrives with no code change here.
_JOB_CLOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "silent": {"type": "boolean"},
        "message": {"type": ["string", "null"]},
    },
    "required": ["silent", "message"],
    "additionalProperties": False,
}
_JOB_CLOSE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "job_close", "strict": True, "schema": _JOB_CLOSE_SCHEMA},
}


# Fire-time state snapshot: a scheduled run's live turn carries the current
# content of every vault page its prompt names, so the "is this reminder
# still needed?" check happens against state the model cannot fail to see
# (a follow-up once asked about an interview whose outcome was already
# recorded in the very page its prompt named as the ingest destination).
_SNAPSHOT_MAX_FILES = 5
_SNAPSHOT_MAX_CHARS = 5000
_SNAPSHOT_HEADER = (
    "[state snapshot, fetched at fire time — current content of pages this job references]"
)
_VAULT_PATH_RX = re.compile(r"[\w./-]+\.md\b")


def _extract_vault_paths(prompt: str) -> list[str]:
    """Vault-relative markdown paths named in a job prompt, deduped in order.

    Bare basenames don't count — job prompts name pages vault-relative, and a
    lone "framer.md" would only inline a misleading not-found sentinel.
    """
    paths: list[str] = []
    for match in _VAULT_PATH_RX.findall(prompt):
        if "/" not in match or match in paths:
            continue
        paths.append(match)
        if len(paths) == _SNAPSHOT_MAX_FILES:
            break
    return paths


def _parse_job_close(reply: str) -> dict[str, Any] | None:
    """Parse a scheduled run's closing reply against ``_JOB_CLOSE_SCHEMA``.

    Tolerant on purpose — without API enforcement the object may arrive
    fenced or wrapped in prose. A missing ``message`` is treated as null.
    Returns None when no conforming object is found; run_job then falls back
    to the legacy sentinel rules.
    """
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("silent"), bool):
            continue
        message = data.get("message")
        if message is None or isinstance(message, str):
            return {"silent": data["silent"], "message": message}
    return None

# Tools that mutate the vault. Their dispatch holds the backup lock (a commit
# must not snapshot a file mid-write) and their paths are attributed to the
# run's backup commit.
_VAULT_MUTATING_TOOLS = frozenset({
    "create_file",
    "rewrite_file",
    "edit_file",
    "append_file",
    "move_file",
    "schedule",
    "cancel_scheduled",
})


def _paths_touched(name: str, args: dict[str, Any]) -> set[str]:
    """Vault paths a tool call may have written, for backup commit attribution.

    Over-reporting is safe — staging an unchanged path is a no-op — so this
    records what the call *could* touch without checking whether it succeeded.
    """
    if name in ("create_file", "rewrite_file", "edit_file", "append_file"):
        path = args.get("path")
        return {path} if path else set()
    if name == "move_file":
        # A move deletes the source and adds the destination; stage both.
        return {p for p in (args.get("path"), args.get("new_path")) if p}
    if name in ("schedule", "cancel_scheduled"):
        return {"system/schedule.md"}
    return set()

# Shared with fanout.py: workers expose the same research tool to their model.
RESEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "research",
        "description": (
            "Research a question on the public web via an isolated research "
            "sub-agent that runs searches, reads pages, and returns a summary "
            "with source URLs. Pass one self-contained question of at most "
            "400 characters. Use it whenever current or external information "
            "is needed. Never include personal or vault content in the question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
            },
            "required": ["question"],
        },
    },
}


def _read_prompt(name: str) -> str:
    """Read a capability prompt shipped inside the package (src/assistant/prompts/)."""
    return (files("assistant") / "prompts" / name).read_text(encoding="utf-8").strip()


def extract_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return tool calls from an assistant message, tolerating legacy shapes."""
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        return tool_calls
    # Legacy single-call shape: {"function_call": {"name": ..., "arguments": ...}}
    fc = msg.get("function_call")
    if fc:
        return [{
            "id": "call_0",
            "type": "function",
            "function": {
                "name": fc.get("name", ""),
                "arguments": fc.get("arguments", "{}"),
            },
        }]
    return []


class Agent:
    """Runs the tool-calling loop."""

    def __init__(
        self,
        vault_tools: VaultTools,
        schedule_dispatcher: Callable[[str, dict[str, Any]], str] | None = None,
        schedule_schemas: list[dict[str, Any]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        research_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        extract_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        fan_out_fn: Callable[[str, list[str]], Coroutine[Any, Any, str]] | None = None,
        skills: SkillLibrary | None = None,
        backup: VaultBackup | None = None,
        history_exchanges: int = 5,
        tz_name: str = "UTC",
        archive: ConversationArchive | None = None,
        home_chat_fn: Callable[[], int | None] | None = None,
        agent_name: str = "Noxide",
    ) -> None:
        self._vault = vault_tools
        self._agent_name = agent_name
        self._schedule_dispatcher = schedule_dispatcher
        self._schedule_schemas = schedule_schemas or []
        self._send_message_fn = send_message_fn
        self._research_fn = research_fn
        self._extract_fn = extract_fn
        self._fan_out_fn = fan_out_fn
        self._skills = skills
        self._backup = backup
        self._histories: dict[int, ConversationHistory] = {}
        # Scheduled-run deliveries queued for mirroring into the target
        # conversation's history, keyed like _histories. Queued at send time,
        # drained by that conversation's next run — appending directly from
        # the job run could interleave into an in-flight run's tool sequence,
        # and waiting for the target's run lock inside a send_message dispatch
        # could outlive the tool timeout and bait the model into re-sending.
        self._pending_notes: dict[int, deque[str]] = {}
        # One lock per conversation: concurrent runs for the same chat would
        # interleave appends into one history and produce tool messages the
        # API rejects. Different conversations (other chats, scheduled jobs
        # on chat 0) run in parallel.
        self._run_locks: dict[int, asyncio.Lock] = {}
        self._history_exchanges = history_exchanges
        self.archive = archive
        self._home_chat_fn = home_chat_fn
        self._home_chat_aliases: set[int] = set()
        self._tz = ZoneInfo(tz_name)

    def _get_history(self, chat_id: int) -> ConversationHistory:
        key = self.conversation_key(chat_id)
        if key not in self._histories:
            self._histories[key] = ConversationHistory(
                self._history_exchanges, archive=self.archive if chat_id != 0 else None,
                space=self.conversation_space(key),
            )
        return self._histories[key]

    def conversation_key(self, chat_id: int) -> int:
        """The canonical conversation: the pinned Telegram home chat is the web chat."""
        if self._home_chat_fn and (home := self._home_chat_fn()) is not None:
            self._home_chat_aliases.add(home)
        if chat_id != 0 and chat_id in self._home_chat_aliases:
            return WEB_CHAT_ID
        return chat_id

    def conversation_space(self, chat_id: int) -> str:
        return conversation_space(self.conversation_key(chat_id))

    async def reset_conversation(self, chat_id: int):
        key = self.conversation_key(chat_id)
        async with self._run_locks.setdefault(key, asyncio.Lock()):
            self._histories.pop(key, None)
            self._pending_notes.pop(key, None)
            if self.archive:
                self.archive.reset(self.conversation_space(key))

    def _local_stamp(self) -> str:
        return datetime.now(tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M local")

    def _queue_sent_note(self, chat_id: int, text: str) -> None:
        """Queue a scheduled-run delivery for the target conversation's history.

        The stamp is frozen now, like user-message stamps; the provenance
        prefix tells the model this is a message it already sent, not one to
        send again.
        """
        key = self.conversation_key(chat_id)
        note = f"[{self._local_stamp()}, sent from a scheduled run] {text}"
        if self.archive:
            self.archive.queue_note(self.conversation_space(key), note)
        else:
            self._pending_notes.setdefault(key, deque()).append(note)

    def _job_state_snapshot(self, prompt: str) -> str | None:
        """Current content of the vault pages a job prompt names.

        Injected into the run's live turn only (never stored in history —
        chat 0 would otherwise re-pay every old snapshot on every job run).
        A missing page inlines the not-found sentinel: a broken premise is
        itself something the run should see rather than guess around.
        """
        paths = _extract_vault_paths(prompt)
        if not paths:
            return None
        blocks = [_SNAPSHOT_HEADER]
        for path in paths:
            try:
                content = self._vault.read_file(path)
            except Exception as e:  # jail violations from odd matches (../, /abs)
                content = f"[unreadable: {e}]"
            if len(content) > _SNAPSHOT_MAX_CHARS:
                content = content[:_SNAPSHOT_MAX_CHARS] + "\n[truncated]"
            blocks.append(f"--- {path} ---\n{content}")
        return "\n\n".join(blocks)

    def clear_history(self, chat_id: int) -> None:
        """Forget one chat's conversation; the next run starts fresh."""
        key = self.conversation_key(chat_id)
        self._histories.pop(key, None)
        self._pending_notes.pop(key, None)
        if self.archive:
            self.archive.reset(self.conversation_space(key))

    def _base_prompt(self) -> str:
        """Embedded capability prompt: ships with the code, sections gated by enabled features."""
        sections = [_read_prompt("base.md"), _read_prompt("wiki.md")]
        if self._schedule_dispatcher:
            sections.append(_read_prompt("schedule.md"))
        if self._research_fn:
            sections.append(_read_prompt("research.md"))
        if self._extract_fn:
            sections.append(_read_prompt("extract.md"))
        if self._fan_out_fn:
            sections.append(_read_prompt("fanout.md"))
        if self._skills:
            sections.append(_read_prompt("skills.md"))
        return "\n\n".join(sections)

    def _load_system_prompt(self) -> str:
        """Assemble: embedded capabilities → vault AGENTS.md → skills menu.

        The embedded part documents what the bot can do and updates with the
        code; the vault parts carry user- and deployment-specific conventions
        and take precedence on conflict (they come later in the prompt).

        The result must be stable across runs — the current time rides on the
        newest user message instead, so the provider's prompt cache keeps
        covering the system prompt and older history.
        """
        parts = [self._base_prompt(),
                 f"Your assistant instance name is {json.dumps(self._agent_name, ensure_ascii=False)}. "
                 "Noxide is the software project, not necessarily your name."]
        vault_prompt = self._vault.read_file("AGENTS.md")
        if not vault_prompt.startswith("[file not found"):
            parts.append(vault_prompt)
        # The menu is the one volatile part of the prompt, so it goes last:
        # a change invalidates only the tail of the provider's prompt cache.
        # It is built from triggers only, so refining a skill body changes nothing.
        if self._skills:
            menu = self._skills.menu()
            if menu:
                parts.append(menu)
        return "\n\n".join(parts)

    def _all_tools(self) -> list[dict[str, Any]]:
        tools = list(self._vault.tool_schemas())
        tools.extend(history_tool_schemas())
        tools.extend(self._schedule_schemas)
        if self._skills:
            tools.extend(self._skills.tool_schemas())
        if self._send_message_fn:
            tools.append({
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": (
                        "Send a message to the user's Telegram chat. "
                        "Use this when scheduled jobs need to deliver output."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                },
            })
        if self._research_fn:
            tools.append(RESEARCH_TOOL_SCHEMA)
        if self._extract_fn:
            tools.append({
                "type": "function",
                "function": {
                    "name": "extract_attachment",
                    "description": (
                        "Extract the readable content of a stored vault attachment. "
                        "Returns the text of PDFs and plain-text files; scanned PDFs "
                        "and images are transcribed/described via a vision model. "
                        "Use it when a request depends on what is inside an attachment. "
                        "Long documents are truncated."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Vault-relative path, e.g. attachments/2026-07-22-ab12cd.pdf",
                            },
                        },
                        "required": ["path"],
                    },
                },
            })
        if self._fan_out_fn:
            tools.append({
                "type": "function",
                "function": {
                    "name": "fan_out",
                    "description": (
                        "Apply one instruction to many independent items in parallel. "
                        "Concurrent worker sub-agents each process one item with a "
                        "fresh context and return one result per item. Workers are "
                        "read-only: they can read/search the vault, load skills, and "
                        "research the web, but cannot write files, schedule jobs, or "
                        "send messages. The instruction must be fully self-contained — "
                        "workers see nothing of this conversation."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instruction": {
                                "type": "string",
                                "description": (
                                    "Task to apply to every item, including any file "
                                    "paths, skill names, and the expected result format."
                                ),
                            },
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Independent items, one worker each (max 50).",
                            },
                        },
                        "required": ["instruction", "items"],
                    },
                },
            })
        return tools

    async def _dispatch_tool(
        self,
        name: str,
        args: dict[str, Any],
        send_message_fn: SendMessageFn | None = None,
        history: ConversationHistory | None = None,
    ) -> str:
        if name in ("get_history", "search_history"):
            if history is None:
                return "[tool error: no conversation history in this context]"
            return history.retrieve(name, args)
        # Vault mutations wait for any in-flight backup commit, so a commit
        # never snapshots a file mid-write.
        if self._backup is not None and name in _VAULT_MUTATING_TOOLS:
            async with self._backup.lock:
                return await self._dispatch_tool_unlocked(name, args, send_message_fn)
        return await self._dispatch_tool_unlocked(name, args, send_message_fn)

    async def _dispatch_tool_unlocked(
        self,
        name: str,
        args: dict[str, Any],
        send_message_fn: SendMessageFn | None = None,
    ) -> str:
        # File tools
        if name in (
            "read_file",
            "create_file",
            "rewrite_file",
            "edit_file",
            "append_file",
            "move_file",
            "list_files",
            "search",
            "check_vault",
        ):
            return self._vault.dispatch(name, args)
        # Skills (stored procedures; bodies live in the package or the vault)
        if name == "load_skill" and self._skills:
            return self._skills.dispatch(name, args)
        # Schedule tools
        if name in ("schedule", "list_scheduled", "cancel_scheduled") and self._schedule_dispatcher:
            return self._schedule_dispatcher(name, args)
        # Send message (for scheduled runs)
        send_fn = send_message_fn or self._send_message_fn
        if name == "send_message" and send_fn:
            await send_fn(args["text"])
            return "Message sent."
        # Web research (quarantined sub-agent)
        if name == "research" and self._research_fn:
            return await self._research_fn(args["question"])
        # Attachment content extraction (local parse, vision fallback)
        if name == "extract_attachment" and self._extract_fn:
            return await self._extract_fn(args["path"])
        # Fan-out bulk processing (concurrent quarantined workers)
        if name == "fan_out" and self._fan_out_fn:
            return await self._fan_out_fn(args["instruction"], args.get("items") or [])
        return f"[unknown tool: {name}]"

    async def run(
        self,
        chat_id: int,
        user_message: str,
        extra_context: str | None = None,
        image_data_urls: list[str] | None = None,
        transient_context: str | None = None,
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        response_format: dict[str, Any] | None = None,
        unwind_on_unavailable: bool = False,
        message_id: str | None = None,
        source: str = "telegram",
    ) -> str:
        """Run the agent loop for a user message. Returns the final text reply.

        ``transient_context`` rides the user message during this run only;
        stored history keeps the bare message (same treatment as images).

        Runs for the same conversation are serialized on a lock —
        interleaved appends into one history would produce orphaned tool
        messages the API rejects. Runs for different conversations (other
        chats, scheduled jobs) proceed in parallel; the lock queue is FIFO,
        so same-conversation messages are handled in arrival order.

        ``on_research`` is awaited once, best-effort, the first time this run
        dispatches the ``research`` tool (e.g. to react to the Telegram message).
        ``send_message_fn`` overrides the constructor-injected sender for this
        run only (used by ``run_job`` to observe deliveries).
        """
        chat_id = self.conversation_key(chat_id)  # Freeze identity before lock waits or network calls.
        if self.archive and chat_id != 0:
            message_id = self.archive.insert(self.conversation_space(chat_id), "user", user_message, "queued",
                                             message_id=message_id, source=source) if message_id is None else message_id
        lock = self._run_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if self.archive and message_id:
                row = self.archive.get(message_id)
                if row is None or row["status"] in ("dismissed", "deleted"):
                    return ""
                if row["space"] != self.conversation_space(chat_id):
                    raise ValueError("Message belongs to a different conversation")
                if row["status"] == "done":
                    return self.archive.reply(message_id) or ""
                self.archive.status(message_id, "running")
                self._get_history(chat_id).request_ids.add(message_id)
                self._get_history(chat_id).active_request_id = message_id
            try:
                reply, touched = await self._run_locked(
                    chat_id,
                    user_message,
                    extra_context=extra_context,
                    image_data_urls=image_data_urls,
                    transient_context=transient_context,
                    on_research=on_research,
                    send_message_fn=send_message_fn,
                    response_format=response_format,
                    unwind_on_unavailable=unwind_on_unavailable,
                )
            except BaseException as exc:
                if self.archive and message_id:
                    status = "unavailable" if isinstance(exc, copilot.CopilotUnavailableError) else "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
                    self.archive.status(message_id, status, "Run did not finish; some work may have completed.")
                raise
            if self.archive and message_id:
                if reply == MAX_ITERATIONS_REPLY:
                    self.archive.status(message_id, "failed", "Iteration limit reached; retry to continue.")
        # One commit per interaction, in the background: the reply is not
        # delayed by git, and the commit message carries the full exchange.
        if self._backup is not None and touched:
            self._backup.schedule_commit(touched, trigger=user_message, response=reply)
        return reply

    async def _run_locked(
        self,
        chat_id: int,
        user_message: str,
        extra_context: str | None = None,
        image_data_urls: list[str] | None = None,
        transient_context: str | None = None,
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        response_format: dict[str, Any] | None = None,
        unwind_on_unavailable: bool = False,
    ) -> tuple[str, set[str]]:
        """Returns the final text reply and the vault paths this run touched.

        With ``unwind_on_unavailable`` (outage-replay attempts), a
        CopilotUnavailableError removes the just-appended user message from
        history before propagating — the drain loop re-invokes the replay on
        every backoff cycle, and without the unwind each failed attempt would
        grow the pending request with a redundant note. Only the newest entry
        is ever removed: anything newer
        means the run made progress (completed tool calls) that the next
        resume must see. Completed exchanges are projected separately; this
        unwind touches only the unfinished work block.
        """
        t_start = time.monotonic()
        history = self._get_history(chat_id)
        history.begin_run()
        touched: set[str] = set()

        # Messages a scheduled run delivered to this conversation since its
        # last run enter history here, under its run lock, so the incoming
        # user message lands with the reminder it is replying to in context.
        for note in self._pending_notes.pop(self.conversation_key(chat_id), ()):
            history.append({"role": "assistant", "content": note})
        if history.archive:
            for note in history.archive.notes(history.space):
                if note["id"] not in history.note_ids:
                    history.append({"role": "assistant", "content": note["content"]})
                    history.note_ids.add(note["id"])

        system_prompt = self._load_system_prompt()
        if extra_context:
            system_prompt = f"{system_prompt}\n\n{extra_context}"

        # The send-time stamp is how the model knows the current time; it is
        # stored with the message so past turns never change retroactively.
        # It is already the user's local time: stamping UTC left the model doing
        # DST-aware arithmetic in its head, and UTC clock times leaked into vault
        # fields (routines "last done", reminder notes) that must be local.
        stamped_message = f"[{self._local_stamp()}] {user_message}"
        user_entry: dict[str, Any] = {"role": "user", "content": stamped_message}
        history.append(user_entry)

        # Images and transient context ride only this run's requests; stored
        # history keeps the bare text version so later turns don't re-pay
        # image tokens or stale state snapshots on every request.
        live_text = (
            stamped_message
            if transient_context is None
            else f"{stamped_message}\n\n{transient_context}"
        )
        if coverage := history.coverage():
            live_text = f"{live_text}\n\n{coverage}"
        live_entry: dict[str, Any] | None = None
        if image_data_urls:
            live_entry = {
                "role": "user",
                "content": [
                    {"type": "text", "text": live_text},
                    *(
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in image_data_urls
                    ),
                ],
            }
        elif transient_context is not None or coverage:
            live_entry = {"role": "user", "content": live_text}

        tools = self._all_tools()
        client = copilot.get_client()
        total_tool_calls = 0

        for iteration in range(_MAX_ITERATIONS):
            turn_messages = history.messages()
            if live_entry is not None:
                turn_messages = [
                    live_entry if m is user_entry else m for m in turn_messages
                ]
            messages = [{"role": "system", "content": system_prompt}] + turn_messages
            try:
                response = await client.chat(messages, tools, response_format=response_format)
            except copilot.CopilotUnavailableError:
                if unwind_on_unavailable:
                    history.pop_if_last(user_entry)
                raise
            except Exception:
                # A rejected request is terminal, not outage-pending work.
                # Keep it so the next run can compact oversized outputs;
                # retain the real tail (no success reply to supersede retries).
                # CancelledError is a BaseException and preserves pending work.
                history.finish_run(success=False)
                raise

            choice = response["choices"][0]
            msg = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # Log turn
            usage_dict = response.get("usage", {})
            usage.record(
                "agent",
                response.get("model", ""),
                usage_dict,
                chat_id=chat_id,
            )
            tool_calls_in_turn = extract_tool_calls(msg)
            tool_names = ",".join(tc["function"]["name"] for tc in tool_calls_in_turn)
            logger.info(
                "agent turn=%d finish=%s tools=%d%s tokens_in=%s tokens_out=%s duration=%.1fs",
                iteration,
                finish_reason,
                len(tool_calls_in_turn),
                f"({tool_names})" if tool_names else "",
                usage_dict.get("prompt_tokens", "?"),
                usage_dict.get("completion_tokens", "?"),
                time.monotonic() - t_start,
            )

            # Append assistant message to history
            history.append(msg)

            # The API may report finish_reason "stop" even when tool calls are
            # present — only the absence of tool calls ends the loop.
            if not tool_calls_in_turn:
                if finish_reason in ("tool_calls", "function_call"):
                    logger.warning(
                        "finish_reason=%s but no tool calls parsed; raw message: %s",
                        finish_reason,
                        json.dumps(msg, ensure_ascii=False)[:4000],
                    )
                history.finish_run(timestamp=self._local_stamp())
                return msg.get("content") or "", touched

            # Execute tool calls
            for tc in tool_calls_in_turn:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "tool %s: unparseable arguments, dispatching with {}: %s",
                        fn_name,
                        str(tc["function"].get("arguments", ""))[:300],
                    )
                    fn_args = {}
                total_tool_calls += 1
                if fn_name == "research" and on_research is not None:
                    try:
                        await on_research()
                    except Exception:
                        logger.warning("on_research callback failed", exc_info=True)
                    on_research = None  # notify at most once per run
                failure_tb = ""
                try:
                    if not isinstance(fn_args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    touched |= _paths_touched(fn_name, fn_args)
                    timeout = _TOOL_TIMEOUTS.get(fn_name, _TOOL_TIMEOUT)
                    result = await asyncio.wait_for(
                        self._dispatch_tool(fn_name, fn_args, send_message_fn, history),
                        timeout=timeout,
                    )
                except TimeoutError:
                    result = f"[tool {fn_name} timed out after {timeout}s]"
                except PermissionError as e:
                    result = f"[permission denied: {e}]"
                except Exception as e:
                    result = f"[tool error: {e}]"
                    failure_tb = traceback.format_exc()

                if _ERROR_RESULT_RX.match(str(result)):
                    logger.warning(
                        "tool %s failed: %s | args: %s%s",
                        fn_name,
                        str(result)[:300],
                        json.dumps(fn_args, ensure_ascii=False)[:300],
                        f"\n{failure_tb}" if failure_tb else "",
                    )

                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        # Hit iteration cap
        logger.warning("Agent hit max iterations (%d) for chat_id=%d", _MAX_ITERATIONS, chat_id)
        return MAX_ITERATIONS_REPLY, touched

    async def retry_message(
        self,
        chat_id: int,
        text: str,
        queued_at: str,
        hot: bool,
        send_message_fn: SendMessageFn | None = None,
        extra_context: str | None = None,
        message_id: str | None = None,
        image_data_urls: list[str] | None = None,
    ) -> str | None:
        """Replay a user message that failed during a Copilot outage.

        *Hot* items were queued in this process: the failed turn — the user
        message, possibly followed by completed tool calls — still sits in
        this conversation's history, so the replay appends only a resume note
        and lets the model pick the turn up in place. Re-appending the text
        would double it, and re-running a mid-run failure from scratch could
        redo vault writes the first attempt already made. Two hot cases stand
        down instead: an empty history (/clear during the outage — the
        conversation was deliberately forgotten) and a history whose last
        entry is a plain assistant reply (a later successful run saw the
        pending message in context and already covered it).

        *Cold* items were reloaded from disk after a restart: the history is
        gone, so the original text is replayed with a provenance note carrying
        when it was sent and that it may have been partially processed —
        the model re-reads pages before writing, per its normal recipe.

        Returns the reply to deliver, or None when the item was superseded.
        Raises CopilotUnavailableError while the outage lasts, so the retry
        queue keeps the item.
        """
        chat_id = self.conversation_key(chat_id)
        lock = self._run_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if self.archive and message_id:
                row = self.archive.get(message_id)
                if row is None or row["status"] in ("dismissed", "deleted", "done"):
                    return None
                if row["space"] != self.conversation_space(chat_id):
                    raise ValueError("Retry belongs to a different conversation")
                self._get_history(chat_id).request_ids.add(message_id)
                self._get_history(chat_id).active_request_id = message_id
            if hot:
                msgs = self._get_history(chat_id).messages()
                if not msgs:
                    return None  # /clear deliberately supersedes queued work
                last = msgs[-1]
                if last.get("role") == "assistant" and not extract_tool_calls(last):
                    return None  # a completed run saw all pending work
                note = (
                    "[Copilot went down mid-conversation and is back now — review the "
                    "messages above and finish handling anything still unanswered or "
                    "incomplete]"
                )
            else:
                note = (
                    f"[this message was originally sent {queued_at} and delayed by a Copilot "
                    f"outage; it may have been partially processed before the failure] {text}"
                )
            try:
                reply, touched = await self._run_locked(
                    chat_id, note, unwind_on_unavailable=True,
                    send_message_fn=send_message_fn, extra_context=extra_context,
                    # A cold replay shows the pictures again; a hot one's turn
                    # already carried them.
                    image_data_urls=None if hot else image_data_urls,
                )
            except BaseException as exc:
                if self.archive and message_id:
                    self.archive.status(message_id,
                                        "unavailable" if isinstance(exc, copilot.CopilotUnavailableError) else "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                                        "Retry did not finish; some work may have completed.")
                raise
            if self.archive and message_id and reply == MAX_ITERATIONS_REPLY:
                self.archive.status(message_id, "failed", "Iteration limit reached; retry to continue.")
        if self._backup is not None and touched:
            self._backup.schedule_commit(touched, trigger=note, response=reply)
        return reply

    async def run_job(self, prompt: str) -> str:
        """Run a scheduled-job prompt (chat_id 0).

        The prompt reaches the model tagged ``[scheduled run]``, and the run
        closes with a JSON object matching ``_JOB_CLOSE_SCHEMA`` (the contract
        in prompts/schedule.md): silent runs deliver nothing, otherwise the
        ``message`` field is delivered — unless the run already spoke via the
        send_message tool, whose messages must not be repeated. A reply that
        doesn't parse falls back to the legacy rules: a [silent] anywhere in
        it stands down (models misplace the sentinel), anything else is
        delivered raw so a reminder is never lost.

        Returns the run's raw final reply, so callers that must not mistake
        an abandoned run for a completed one can check it against
        ``MAX_ITERATIONS_REPLY`` (inbox ingestion does, before clearing).
        """
        base_send = self._send_message_fn
        delivered = 0

        async def counting_send(text: str) -> int | None:
            nonlocal delivered
            target_chat = None
            if base_send:
                target_chat = await base_send(text)
            delivered += 1
            # Mirror the delivery into the target conversation's history so a
            # user reply to it there arrives with context — this run is the
            # chat-0 job conversation, invisible to the one the message
            # landed in. A None chat id means the delivery was dropped.
            if target_chat is not None and self.archive is None:
                self._queue_sent_note(target_chat, text)
            return target_chat

        reply = await self.run(
            chat_id=0,
            user_message=f"[scheduled run] {prompt}",
            transient_context=self._job_state_snapshot(prompt),
            send_message_fn=counting_send,
            response_format=_JOB_CLOSE_RESPONSE_FORMAT,
        )
        close = _parse_job_close(reply)
        if close is not None:
            if close["silent"] or delivered or not close["message"] or base_send is None:
                return reply
            await counting_send(close["message"])
            return reply
        if _SILENT_SENTINEL in reply.lower():
            return reply
        if not delivered and reply and base_send:
            logger.warning("Scheduled run closed without job-close JSON; delivering raw reply")
            await counting_send(reply)
        return reply
