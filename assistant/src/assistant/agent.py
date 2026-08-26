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
from .skills import SkillLibrary
from .tools import VaultTools, slug_from_name

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

# Tool results from *finished* runs are trimmed to this many characters at the
# start of the next run: heavy outputs (check_vault reports, full-page reads)
# otherwise sit in the deque for up to history_size messages and get re-paid,
# cold, on every later request. The marker carries no lengths so re-trimming
# is byte-identical — a shifting history would defeat the provider's prompt
# cache. Trimming drops read_file's trailing version token, which is a
# feature: a rewrite in a later run must re-read anyway.
_HISTORY_TOOL_RESULT_CAP = 2000
_HISTORY_TRIM_MARKER = (
    "\n[older tool output trimmed from history — call the tool again "
    "if you need the full content]"
)

# A proactive sender: delivers text (optionally into a forum topic) and
# returns the chat id it delivered to, or None when the message was dropped.
# The chat id is how run_job learns the real conversation key when mirroring
# a delivery into that conversation's history.
SendMessageFn = Callable[[str, int | None], Coroutine[Any, Any, int | None]]

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

_TOPIC_INDEX_FILE = "system/topics/index.md"
_TOPIC_INDEX_HEADER = "| topic_id | slug | name |"
_TOPIC_INDEX_SEP = "|----------|------|------|"
_TOPIC_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _escape_topic_cell(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("topic index cells must be single-line")
    return value.replace("|", "\\|")


def _unescape_topic_cell(value: str) -> str:
    return value.replace("\\|", "|")


def _topic_row(topic_id: int, slug: str, name: str) -> str:
    cells = (str(topic_id), slug, name)
    return "| " + " | ".join(_escape_topic_cell(cell) for cell in cells) + " |"


def _parse_topic_row(row: str) -> tuple[int, str, str] | None:
    cols = [col.strip() for col in _TOPIC_CELL_SPLIT_RE.split(row.strip())]
    if cols and not cols[0]:
        cols.pop(0)
    if cols and not cols[-1]:
        cols.pop()
    if len(cols) < 3:
        return None
    topic_id, slug, name = (_unescape_topic_cell(col) for col in cols[:3])
    try:
        return int(topic_id), slug, name
    except ValueError:
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
    "create_forum_topic",
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
    if name == "create_forum_topic":
        slug = slug_from_name(args.get("name") or "")
        if slug:
            return {f"system/topics/{slug}/AGENTS.md", _TOPIC_INDEX_FILE}
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


class ConversationHistory:
    """Keeps the last N messages per chat."""

    def __init__(self, max_size: int = 40) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=max_size)

    def append(self, msg: dict[str, Any]) -> None:
        self._history.append(msg)

    def messages(self) -> list[dict[str, Any]]:
        msgs = list(self._history)
        # Eviction can cut between an assistant tool_calls message and its
        # tool results; leading orphaned tool messages are rejected by the API.
        while msgs and msgs[0].get("role") == "tool":
            msgs.pop(0)
        return msgs

    def compact_tool_results(self) -> None:
        """Trim stored tool results beyond ``_HISTORY_TOOL_RESULT_CAP`` chars.

        Called at run start, under the conversation's run lock, so every
        message in the deque belongs to a finished run — the live run keeps
        its own tool outputs intact. Trimming is idempotent (the marker holds
        no lengths), keeping compacted history byte-stable across runs for
        the provider's prompt cache. Entries are replaced, not mutated: the
        original dicts may still be referenced by an in-flight backup commit
        or a retry-queue peek.
        """
        for i, msg in enumerate(self._history):
            content = msg.get("content")
            if (
                msg.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > _HISTORY_TOOL_RESULT_CAP
            ):
                self._history[i] = {
                    **msg,
                    "content": content[:_HISTORY_TOOL_RESULT_CAP] + _HISTORY_TRIM_MARKER,
                }

    def pop_if_last(self, msg: dict[str, Any]) -> bool:
        """Remove ``msg`` if it is (by identity) the newest entry.

        Lets a failed outage-replay attempt unwind the note it just appended:
        anything newer than the note means the run made progress, which must
        be kept.
        """
        if self._history and self._history[-1] is msg:
            self._history.pop()
            return True
        return False

    def clear(self) -> None:
        self._history.clear()


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
        create_forum_topic_fn: Callable[[str], Coroutine[Any, Any, dict[str, Any]]] | None = None,
        research_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        extract_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        fan_out_fn: Callable[[str, list[str]], Coroutine[Any, Any, str]] | None = None,
        skills: SkillLibrary | None = None,
        backup: VaultBackup | None = None,
        history_size: int = 40,
        tz_name: str = "UTC",
    ) -> None:
        self._vault = vault_tools
        self._schedule_dispatcher = schedule_dispatcher
        self._schedule_schemas = schedule_schemas or []
        self._send_message_fn = send_message_fn
        self._create_forum_topic_fn = create_forum_topic_fn
        self._research_fn = research_fn
        self._extract_fn = extract_fn
        self._fan_out_fn = fan_out_fn
        self._skills = skills
        self._backup = backup
        self._histories: dict[tuple[int, int | None], ConversationHistory] = {}
        # Scheduled-run deliveries queued for mirroring into the target
        # conversation's history, keyed like _histories. Queued at send time,
        # drained by that conversation's next run — appending directly from
        # the job run could interleave into an in-flight run's tool sequence,
        # and waiting for the target's run lock inside a send_message dispatch
        # could outlive the tool timeout and bait the model into re-sending.
        self._pending_notes: dict[tuple[int, int | None], deque[str]] = {}
        # One lock per conversation: concurrent runs for the same
        # (chat_id, thread_id) would interleave appends into one history and
        # produce tool messages the API rejects. Different conversations
        # (other topics, scheduled jobs on chat 0) run in parallel.
        self._run_locks: dict[tuple[int, int | None], asyncio.Lock] = {}
        self._history_size = history_size
        self._tz = ZoneInfo(tz_name)

    def _get_history(self, chat_id: int, thread_id: int | None = None) -> ConversationHistory:
        key = (chat_id, thread_id)
        if key not in self._histories:
            self._histories[key] = ConversationHistory(self._history_size)
        return self._histories[key]

    def _local_stamp(self) -> str:
        return datetime.now(tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M local")

    def _queue_sent_note(self, chat_id: int, thread_id: int | None, text: str) -> None:
        """Queue a scheduled-run delivery for the target conversation's history.

        The stamp is frozen now, like user-message stamps; the provenance
        prefix tells the model this is a message it already sent, not one to
        send again.
        """
        notes = self._pending_notes.setdefault(
            (chat_id, thread_id), deque(maxlen=self._history_size)
        )
        notes.append(f"[{self._local_stamp()}, sent from a scheduled run] {text}")

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

    def clear_history(self, chat_id: int, thread_id: int | None = None) -> None:
        """Forget one chat/topic's conversation; the next run starts fresh."""
        self._histories.pop((chat_id, thread_id), None)
        self._pending_notes.pop((chat_id, thread_id), None)

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
        if self._create_forum_topic_fn:
            sections.append(_read_prompt("topics.md"))
        if self._skills:
            sections.append(_read_prompt("skills.md"))
        return "\n\n".join(sections)

    def _load_system_prompt(self, topic_slug: str | None = None) -> str:
        """Assemble: embedded capabilities → vault AGENTS.md → topic AGENTS.md → skills menu.

        The embedded part documents what the bot can do and updates with the
        code; the vault parts carry user- and deployment-specific conventions
        and take precedence on conflict (they come later in the prompt).

        The result must be stable across runs — the current time rides on the
        newest user message instead, so the provider's prompt cache keeps
        covering the system prompt and older history.
        """
        parts = [self._base_prompt()]
        vault_prompt = self._vault.read_file("AGENTS.md")
        if not vault_prompt.startswith("[file not found"):
            parts.append(vault_prompt)
        if topic_slug:
            topic_prompt = self._vault.read_file(f"system/topics/{topic_slug}/AGENTS.md")
            if not topic_prompt.startswith("[file not found"):
                parts.append(topic_prompt)
        # The menu is the one volatile part of the prompt, so it goes last:
        # a change invalidates only the tail of the provider's prompt cache.
        # It is built from triggers only, so refining a skill body changes nothing.
        if self._skills:
            menu = self._skills.menu()
            if menu:
                parts.append(menu)
        return "\n\n".join(parts)

    def _resolve_topic_slug(self, thread_id: int) -> tuple[str | None, str | None]:
        """Look up slug and name for a thread_id from system/topics/index.md.

        Returns ``(slug, name)`` or ``(None, None)`` if not found.
        """
        text = self._vault.read_file(_TOPIC_INDEX_FILE)
        if text.startswith("[file not found"):
            return None, None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("| topic_id") or stripped.startswith("|---"):
                continue
            parsed = _parse_topic_row(stripped)
            if parsed and parsed[0] == thread_id:
                return parsed[1], parsed[2]
        return None, None

    def _register_topic(self, topic_id: int, slug: str, name: str) -> None:
        """Add or update an entry in system/topics/index.md."""
        text = self._vault.read_file(_TOPIC_INDEX_FILE)
        if text.startswith("[file not found"):
            # Create fresh index
            lines = [
                "# Topic Index\n",
                _TOPIC_INDEX_HEADER,
                _TOPIC_INDEX_SEP,
                _topic_row(topic_id, slug, name),
                "",
            ]
            self._vault.write_file(_TOPIC_INDEX_FILE, "\n".join(lines))
            return

        # Rebuild, skipping any existing row with same topic_id
        new_rows: list[str] = []
        found = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and not stripped.startswith("| topic_id") and not stripped.startswith("|---"):
                parsed = _parse_topic_row(stripped)
                if parsed and parsed[0] == topic_id:
                    found = True
                    new_rows.append(_topic_row(topic_id, slug, name))
                    continue
            new_rows.append(line)

        if not found:
            # Find last data row and append after it
            new_rows.append(_topic_row(topic_id, slug, name))

        self._vault.write_file(_TOPIC_INDEX_FILE, "\n".join(new_rows) + "\n")

    def _all_tools(self) -> list[dict[str, Any]]:
        tools = list(self._vault.tool_schemas())
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
                        "Use this when scheduled jobs need to deliver output. "
                        "Optionally include message_thread_id to send to a specific forum topic."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "message_thread_id": {
                                "type": "integer",
                                "description": "Forum topic thread ID; omit for general chat.",
                            },
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
        if self._create_forum_topic_fn:
            tools.append({
                "type": "function",
                "function": {
                    "name": "create_forum_topic",
                    "description": (
                        "Create a new forum topic (room) in the Telegram supergroup. "
                        "Call this when the user asks to create a topic or room. "
                        "Automatically registers the topic in the vault index and creates vault directories."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "Single-line display name for the new topic, "
                                    "e.g. 'Health & Fitness'."
                                ),
                            },
                        },
                        "required": ["name"],
                    },
                },
            })
        return tools

    async def _dispatch_tool(
        self,
        name: str,
        args: dict[str, Any],
        send_message_fn: SendMessageFn | None = None,
    ) -> str:
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
            thread_id: int | None = args.get("message_thread_id")
            await send_fn(args["text"], thread_id)
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
        # Create forum topic
        if name == "create_forum_topic" and self._create_forum_topic_fn:
            topic_name: str = args["name"]
            if "\r" in topic_name or "\n" in topic_name:
                return "[tool error: topic name must be a single line]"
            forum_topic = await self._create_forum_topic_fn(topic_name)
            thread_id = forum_topic["message_thread_id"]
            slug = slug_from_name(topic_name)
            # Create the topic prompt placeholder
            self._vault.write_file(
                f"system/topics/{slug}/AGENTS.md",
                f"# Topic: {topic_name}\n\n<!-- Add topic-specific instructions here -->\n",
            )
            # Register in index
            self._register_topic(thread_id, slug, topic_name)
            return (
                f"Topic '{topic_name}' created successfully. "
                f"thread_id={thread_id}, slug='{slug}'."
            )
        return f"[unknown tool: {name}]"

    async def run(
        self,
        chat_id: int,
        user_message: str,
        thread_id: int | None = None,
        extra_context: str | None = None,
        image_data_url: str | None = None,
        transient_context: str | None = None,
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        response_format: dict[str, Any] | None = None,
        unwind_on_unavailable: bool = False,
    ) -> str:
        """Run the agent loop for a user message. Returns the final text reply.

        ``transient_context`` rides the user message during this run only;
        stored history keeps the bare message (same treatment as images).

        Runs for the same ``(chat_id, thread_id)`` are serialized on a lock —
        interleaved appends into one history would produce orphaned tool
        messages the API rejects. Runs for different conversations (other
        topics, scheduled jobs) proceed in parallel; the lock queue is FIFO,
        so same-conversation messages are handled in arrival order.

        ``on_research`` is awaited once, best-effort, the first time this run
        dispatches the ``research`` tool (e.g. to react to the Telegram message).
        ``send_message_fn`` overrides the constructor-injected sender for this
        run only (used by ``run_job`` to observe deliveries).
        """
        lock = self._run_locks.setdefault((chat_id, thread_id), asyncio.Lock())
        async with lock:
            reply, touched = await self._run_locked(
                chat_id,
                user_message,
                thread_id=thread_id,
                extra_context=extra_context,
                image_data_url=image_data_url,
                transient_context=transient_context,
                on_research=on_research,
                send_message_fn=send_message_fn,
                response_format=response_format,
                unwind_on_unavailable=unwind_on_unavailable,
            )
        # One commit per interaction, in the background: the reply is not
        # delayed by git, and the commit message carries the full exchange.
        if self._backup is not None and touched:
            self._backup.schedule_commit(touched, trigger=user_message, response=reply)
        return reply

    async def _run_locked(
        self,
        chat_id: int,
        user_message: str,
        thread_id: int | None = None,
        extra_context: str | None = None,
        image_data_url: str | None = None,
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
        leave its note behind, eventually evicting the original failed turn
        from the deque. Only the newest entry is ever removed: anything newer
        means the run made progress (completed tool calls) that the next
        resume must see.
        """
        t_start = time.monotonic()
        history = self._get_history(chat_id, thread_id)
        # Everything already in history belongs to finished runs (this run
        # holds the conversation lock): shed the heavy tool outputs before
        # they ride every request of this run, cold.
        history.compact_tool_results()
        touched: set[str] = set()

        # Messages a scheduled run delivered to this conversation since its
        # last run enter history here, under its run lock, so the incoming
        # user message lands with the reminder it is replying to in context.
        for note in self._pending_notes.pop((chat_id, thread_id), ()):
            history.append({"role": "assistant", "content": note})

        # Resolve topic slug for thread-specific prompt (if in a forum topic)
        topic_slug: str | None = None
        if thread_id is not None:
            topic_slug, _ = self._resolve_topic_slug(thread_id)

        system_prompt = self._load_system_prompt(topic_slug)
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
        live_entry: dict[str, Any] | None = None
        if image_data_url:
            live_entry = {
                "role": "user",
                "content": [
                    {"type": "text", "text": live_text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        elif transient_context is not None:
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
                thread_id=thread_id,
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
                return msg.get("content") or "", touched

            # Execute tool calls
            for tc in tool_calls_in_turn:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    logger.warning(
                        "tool %s: unparseable arguments, dispatching with {}: %s",
                        fn_name,
                        str(tc["function"].get("arguments", ""))[:300],
                    )
                    fn_args = {}
                total_tool_calls += 1
                touched |= _paths_touched(fn_name, fn_args)
                if fn_name == "research" and on_research is not None:
                    try:
                        await on_research()
                    except Exception:
                        logger.warning("on_research callback failed", exc_info=True)
                    on_research = None  # notify at most once per run
                failure_tb = ""
                try:
                    timeout = _TOOL_TIMEOUTS.get(fn_name, _TOOL_TIMEOUT)
                    result = await asyncio.wait_for(
                        self._dispatch_tool(fn_name, fn_args, send_message_fn),
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
        thread_id: int | None,
        text: str,
        queued_at: str,
        hot: bool,
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
        if hot:
            # Peeked outside the run lock: a concurrent run can only leave a
            # user/tool/assistant-with-tools tail (no false drop); worst case
            # is a redundant "everything's handled" reply after racing one.
            msgs = self._get_history(chat_id, thread_id).messages()
            if not msgs:
                logger.info(
                    "Dropping queued message for chat_id=%d thread_id=%s: history cleared",
                    chat_id, thread_id,
                )
                return None
            last = msgs[-1]
            if last.get("role") == "assistant" and not extract_tool_calls(last):
                logger.info(
                    "Dropping queued message for chat_id=%d thread_id=%s: already covered "
                    "by a later run", chat_id, thread_id,
                )
                return None
            note = (
                "[Copilot went down mid-conversation and is back now — review the "
                "messages above and finish handling anything still unanswered or "
                "incomplete]"
            )
            return await self.run(
                chat_id, note, thread_id=thread_id, unwind_on_unavailable=True
            )
        note = (
            f"[this message was originally sent {queued_at} and delayed by a Copilot "
            f"outage; it may have been partially processed before the failure] {text}"
        )
        return await self.run(
            chat_id, note, thread_id=thread_id, unwind_on_unavailable=True
        )

    async def run_job(self, prompt: str) -> str:
        """Run a scheduled-job prompt (chat_id 0, no thread).

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

        async def counting_send(text: str, thread_id: int | None = None) -> int | None:
            nonlocal delivered
            target_chat = None
            if base_send:
                target_chat = await base_send(text, thread_id)
            delivered += 1
            # Mirror the delivery into the target conversation's history so a
            # user reply to it there arrives with context — this run is the
            # chat-0 job conversation, invisible to the one the message
            # landed in. A None chat id means the delivery was dropped.
            if target_chat is not None:
                self._queue_sent_note(target_chat, thread_id, text)
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
            await counting_send(close["message"], None)
            return reply
        if _SILENT_SENTINEL in reply.lower():
            return reply
        if not delivered and reply and base_send:
            logger.warning("Scheduled run closed without job-close JSON; delivering raw reply")
            await counting_send(reply, None)
        return reply
