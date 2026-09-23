"""Agent loop: OpenAI-style tool-calling against GitHub Copilot API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from . import copilot, usage
from .backup import VaultBackup
from .conversations import SPACE, ConversationArchive
from .history import ConversationHistory, history_tool_schemas
from .skills import SkillLibrary
from .tools import VaultTools

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 20
# Returned as the run's reply when the loop is abandoned at the iteration cap.
# Public so callers that must not mistake an abandoned run for a completed one
# (inbox ingestion clears processed entries) can recognize it.
MAX_ITERATIONS_REPLY = "[Reached maximum tool-call iterations. Please rephrase your request.]"
# Delivered instead of the sentinel when a scheduled run is abandoned at the
# cap: the sentinel is a protocol value for callers, not a message for the
# user (a catch-up compile once delivered it verbatim, 2026-09-22).
_JOB_CAP_NOTICE = (
    "A scheduled run stopped at its tool-call limit before finishing: \u201c{prompt}\u201d. "
    "What it did before that point is kept; the rest was not done."
)
_JOB_CAP_PROMPT_CHARS = 80
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

# The empty-write guard. A reply that reads as a confirmation ("Anotado",
# "Guardado en…", "Hecho, tarea cerrada") while the run changed nothing in the
# vault or the schedule and sent nothing is, more often than not, a write that
# never happened — the user found out days later on every occasion in the
# 2026-08/09 audit ("Ya te he dicho antes que me la he tomado", "no has
# actualizado el index", "juraría que ya te lo dije"). Such a draft is
# withdrawn and the model gets one more turn, told what it claimed; a second
# no-write reply is accepted and flagged on the reply row (``guard: unsaved``)
# so the app can say so. The lexicon is Spanish and English stems of the
# verbs the bot uses to confirm an action; generic closers ("hecho", "done")
# are left out because they follow read-only work too. A false positive
# costs one cached turn, a miss costs a lost fact.
_CLAIM_RX = re.compile(
    r"\b(anot|apunt|guardad|registr|cerrad|correg|program|cancel|actualiz|añad|anad|"
    r"quit|elimin|borrad|archiv|movid|"
    r"saved|noted|recorded|scheduled|updated|removed|closed|added|archived|deleted|moved)",
    re.IGNORECASE,
)
_GUARD_NOTE = (
    "[Your draft reply was: \u201c{draft}\u201d — but this turn made no change to the vault "
    "or the schedule and sent no message. If something should have been saved, updated, "
    "scheduled, closed or cancelled, do it now with the tools and then reply; if it was "
    "already in place, reply again saying so plainly.]"
)
_GUARD_DRAFT_CHARS = 300

# A scheduled run replies with this sentinel (prompts/schedule.md) when it
# finds its purpose already met; run_job then delivers nothing.
_SILENT_SENTINEL = "[silent]"

# A proactive sender: delivers text to the user.
SendMessageFn = Callable[[str], Coroutine[Any, Any, None]]

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
# Background for a thread's live turn: the newest threads of the past day.
_AMBIENT_THREADS = 5
_AMBIENT_WINDOW_SECONDS = 24 * 3600
_AMBIENT_CHARS = 600
_AMBIENT_HEADER = (
    "[Recent conversations, background only — the message above may refer to one of them "
    "without saying so. Deliveries marked as sent from a scheduled run were sent by you; do not "
    "repeat them. The vault, not this list, is the source of current state.]"
)


def _clip(text: str, limit: int = _AMBIENT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


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


@dataclass
class RunResult:
    """What one pass of the loop produced, beyond the reply text.

    ``touched`` is every path a write *may* have changed (for the backup
    commit, where over-reporting is harmless); ``wrote`` only the paths whose
    write did not fail (the receipt shown to the user). ``guard`` is
    ``"unsaved"`` when the empty-write guard re-prompted and the reply still
    changed nothing.
    """

    reply: str
    touched: set[str] = field(default_factory=set)
    wrote: set[str] = field(default_factory=set)
    guard: str | None = None

    def metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {"wrote": sorted(self.wrote)}
        if self.guard:
            data["guard"] = self.guard
        return data


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
        # Histories and locks are keyed by thread: a thread is one root message
        # and its replies, and each runs under its own lock — concurrent runs
        # on one history would interleave appends into tool orderings the API
        # rejects, while different threads proceed in parallel. Key None is
        # the one unthreaded history: scheduled jobs (never archived, so no
        # thread), and every run when there is no archive at all (tests). A
        # settled thread history is dropped after its run; the archive
        # rebuilds it for the next reply.
        self._histories: dict[str | None, ConversationHistory] = {}
        self._run_locks: dict[str | None, asyncio.Lock] = {}
        self._history_exchanges = history_exchanges
        self.archive = archive
        self._tz = ZoneInfo(tz_name)

    def _get_history(self, thread: str | None = None) -> ConversationHistory:
        if thread not in self._histories:
            self._histories[thread] = ConversationHistory(
                self._history_exchanges, archive=self.archive if thread is not None else None,
                space=SPACE, thread=thread,
            )
        return self._histories[thread]

    async def reset_conversation(self) -> None:
        """Forget the in-memory histories and start a new archive generation.

        Threads already running keep their own history object and finish;
        the archive refuses to complete a message from the old generation, so
        callers wait for in-flight work first (the web app refuses a reset
        while a message is queued or running).
        """
        self.clear_history()

    def _local_stamp(self) -> str:
        return datetime.now(tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M local")

    def _ambient_context(self, thread: str) -> str | None:
        """The last few threads, as background for the live turn only.

        A thread's own context is just its messages, so a new root like
        "done" or "pastilla tomada" would otherwise arrive cold. The newest
        threads of the past day — root plus latest reply, scheduled-run
        deliveries included — ride the live turn like a job's state snapshot:
        never stored, so replies do not re-pay them.
        """
        assert self.archive is not None
        threads = self.archive.recent_threads(
            SPACE, generation=self.archive.generation(SPACE),
            since=time.time() - _AMBIENT_WINDOW_SECONDS, limit=_AMBIENT_THREADS, exclude=thread,
        )
        if not threads:
            return None
        blocks = [_AMBIENT_HEADER]
        for item in reversed(threads):  # oldest first, like a transcript
            stamp = datetime.fromtimestamp(item["root_created"], tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
            who = "Assistant (sent from a scheduled run)" if item["root_role"] == "assistant" else "User"
            lines = [f"--- {stamp} ---", f"{who}: {_clip(item['root_text'])}"]
            if item["reply_text"]:
                lines.append(f"Assistant: {_clip(item['reply_text'])}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _job_state_snapshot(self, prompt: str) -> str | None:
        """Current content of the vault pages a job prompt names.

        Injected into the run's live turn only (never stored in history —
        the jobs history would otherwise re-pay every old snapshot on every
        job run). A missing page inlines the not-found sentinel: a broken
        premise is itself something the run should see rather than guess
        around.
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

    def clear_history(self) -> None:
        """Forget every history in memory and open a new archive generation."""
        self._histories.clear()
        if self.archive:
            self.archive.reset(SPACE)

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
                        "Send a message to the user. "
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
        user_message: str,
        *,
        image_data_urls: list[str] | None = None,
        transient_context: str | None = None,
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        message_id: str | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Run the agent loop for a user message. Returns the final text reply.

        ``transient_context`` rides the user message during this run only;
        stored history keeps the bare message (same treatment as images).

        With an archive, the message is a thread: a reply (``reply_to`` an
        archived message, or a pre-inserted ``message_id`` that already
        replies to one) continues its parent's thread and sees that thread's
        exchanges; anything else starts a new thread and sees only the
        ambient background of recent threads. Runs on one thread are
        serialized on a lock — interleaved appends into one history would
        produce orphaned tool messages the API rejects — while other threads
        and scheduled jobs proceed in parallel; the lock queue is FIFO, so
        replies within a thread are handled in arrival order.

        ``on_research`` is awaited once, best-effort, the first time this run
        dispatches the ``research`` tool (the web app shows it on the message).
        ``send_message_fn`` overrides the constructor-injected sender for this
        run only (used by ``run_job`` to observe deliveries).
        """
        thread = None
        if self.archive:
            if message_id is None:
                message_id = self.archive.insert(SPACE, "user", user_message, "queued", reply_to=reply_to)
            thread = self.archive.thread_of(message_id)
        return await self._run(
            thread, user_message, message_id=message_id, image_data_urls=image_data_urls,
            transient_context=transient_context, on_research=on_research,
            send_message_fn=send_message_fn,
        )

    async def _run(
        self,
        thread: str | None,
        user_message: str,
        *,
        message_id: str | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Run under the thread's lock, keeping the archived row's status in step."""
        lock = self._run_locks.setdefault(thread, asyncio.Lock())
        async with lock:
            if self.archive and message_id:
                row = self.archive.get(message_id)
                if row is None or row["status"] in ("dismissed", "deleted"):
                    return ""
                if row["status"] == "done":
                    return self.archive.reply(message_id) or ""
                self.archive.status(message_id, "running")
                history = self._get_history(thread)
                history.request_ids.add(message_id)
                history.active_request_id = message_id
            try:
                result = await self._run_locked(
                    thread, user_message, response_format=response_format, **kwargs,
                )
            except BaseException as exc:
                if self.archive and message_id:
                    self._mark_unfinished(message_id, _failure_status(exc),
                                          "Run did not finish; some work may have completed.")
                raise
            reply = result.reply
            if self.archive and message_id:
                if reply == MAX_ITERATIONS_REPLY:
                    self.archive.status(message_id, "failed", "Iteration limit reached; retry to continue.")
                else:
                    # The receipt: what this run changed, for the timeline.
                    self.archive.merge_metadata(f"reply:{message_id}", result.metadata())
            self._release_thread(thread)
        # One commit per interaction, in the background: the reply is not
        # delayed by git, and the commit message carries the full exchange.
        if self._backup is not None and result.touched:
            self._backup.schedule_commit(result.touched, trigger=user_message, response=reply)
        return reply

    def _mark_unfinished(self, message_id: str, status: str, error: str) -> None:
        """Record why a run stopped, unless the user already dismissed the message.

        A reset landing while a thread runs tombstones its rows; the run then
        fails on completion, and that failure must not revive the row as
        retryable work.
        """
        assert self.archive is not None
        row = self.archive.get(message_id)
        if row is not None and row["status"] not in ("dismissed", "deleted"):
            self.archive.status(message_id, status, error)

    def _release_thread(self, thread: str | None) -> None:
        """Drop a thread's history once nothing unfinished remains in it.

        Its completed exchanges are in the archive, and a later reply
        restores them; keeping every thread ever run would grow without
        bound. Unfinished work (a failed or iteration-capped run) stays for
        the resume that picks it up.
        """
        if thread is not None and thread in self._histories and self._histories[thread].is_settled():
            del self._histories[thread]

    async def _run_locked(
        self,
        thread: str | None,
        user_message: str,
        image_data_urls: list[str] | None = None,
        transient_context: str | None = None,
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: SendMessageFn | None = None,
        response_format: dict[str, Any] | None = None,
        unwind_on_unavailable: bool = False,
    ) -> RunResult:
        """Returns the final text reply and the vault paths this run touched.

        With ``unwind_on_unavailable`` (outage-resume attempts), a
        CopilotUnavailableError removes the just-appended user message from
        history before propagating — a repeated resume would otherwise grow
        the pending request with a redundant note on every attempt. Only the
        newest entry is ever removed: anything newer means the run made
        progress (completed tool calls) that the next resume must see.
        Completed exchanges are projected separately; this unwind touches
        only the unfinished work block.
        """
        t_start = time.monotonic()
        history = self._get_history(thread)
        history.begin_run()
        result = RunResult("")
        # Live-only note the guard appends after withdrawing a no-write
        # confirmation; never stored, so later turns don't carry it.
        guard_note: dict[str, Any] | None = None
        acted = False  # a message sent counts as an action the guard accepts

        if thread is not None:
            ambient = self._ambient_context(thread)
            if ambient:
                transient_context = f"{transient_context}\n\n{ambient}" if transient_context else ambient

        system_prompt = self._load_system_prompt()

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
            if guard_note is not None:
                messages.append(guard_note)
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
            usage.record("agent", response.get("model", ""), usage_dict)
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
                reply = msg.get("content") or ""
                if not result.wrote and not acted and _CLAIM_RX.search(reply):
                    if guard_note is None:
                        logger.info("Empty-write guard: re-prompting a confirmation that wrote nothing")
                        history.pop_if_last(msg)
                        guard_note = {"role": "user", "content": _GUARD_NOTE.format(
                            draft=_clip(reply, _GUARD_DRAFT_CHARS))}
                        continue
                    result.guard = "unsaved"
                history.finish_run(timestamp=self._local_stamp())
                result.reply = reply
                return result

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
                may_write: set[str] = set()
                try:
                    if not isinstance(fn_args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    may_write = _paths_touched(fn_name, fn_args)
                    result.touched |= may_write
                    timeout = _TOOL_TIMEOUTS.get(fn_name, _TOOL_TIMEOUT)
                    tool_result = await asyncio.wait_for(
                        self._dispatch_tool(fn_name, fn_args, send_message_fn, history),
                        timeout=timeout,
                    )
                except TimeoutError:
                    tool_result = f"[tool {fn_name} timed out after {timeout}s]"
                except PermissionError as e:
                    tool_result = f"[permission denied: {e}]"
                except Exception as e:
                    tool_result = f"[tool error: {e}]"
                    failure_tb = traceback.format_exc()

                if _ERROR_RESULT_RX.match(str(tool_result)):
                    logger.warning(
                        "tool %s failed: %s | args: %s%s",
                        fn_name,
                        str(tool_result)[:300],
                        json.dumps(fn_args, ensure_ascii=False)[:300],
                        f"\n{failure_tb}" if failure_tb else "",
                    )
                else:
                    result.wrote |= may_write
                    acted |= fn_name == "send_message"

                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(tool_result),
                })

        # Hit iteration cap
        logger.warning("Agent hit max iterations (%d) for thread=%s", _MAX_ITERATIONS, thread)
        result.reply = MAX_ITERATIONS_REPLY
        return result

    async def resume(
        self,
        message_id: str | None = None,
        *,
        send_message_fn: SendMessageFn | None = None,
    ) -> str | None:
        """Resume a run that failed during a Copilot outage, in place.

        The failed turn — the user message, possibly followed by completed
        tool calls — still sits in the thread's history, so the resume appends
        only a note and lets the model pick the turn up where it stopped.
        Re-appending the text would double it, and re-running a mid-run
        failure from scratch could redo vault writes the first attempt already
        made. Two cases stand down instead: an empty history (a context reset
        during the outage — the conversation was deliberately forgotten) and
        a history whose last entry is a plain assistant reply (a later
        successful run saw the pending message in context and already covered
        it).

        Returns the reply to deliver, or None when the work was superseded.
        Raises CopilotUnavailableError while the outage lasts.
        """
        thread = self.archive.thread_of(message_id) if self.archive and message_id else None
        lock = self._run_locks.setdefault(thread, asyncio.Lock())
        async with lock:
            if self.archive and message_id:
                row = self.archive.get(message_id)
                if row is None or row["status"] in ("dismissed", "deleted", "done"):
                    return None
                history = self._get_history(thread)
                history.request_ids.add(message_id)
                history.active_request_id = message_id
            msgs = self._get_history(thread).messages()
            if not msgs:
                return None  # a reset deliberately supersedes pending work
            last = msgs[-1]
            if last.get("role") == "assistant" and not extract_tool_calls(last):
                return None  # a completed run saw all pending work
            note = (
                "[Copilot went down mid-conversation and is back now — review the "
                "messages above and finish handling anything still unanswered or "
                "incomplete]"
            )
            try:
                result = await self._run_locked(
                    thread, note, unwind_on_unavailable=True, send_message_fn=send_message_fn,
                )
            except BaseException as exc:
                if self.archive and message_id:
                    self._mark_unfinished(message_id, _failure_status(exc),
                                          "Retry did not finish; some work may have completed.")
                raise
            reply = result.reply
            if self.archive and message_id:
                if reply == MAX_ITERATIONS_REPLY:
                    self.archive.status(message_id, "failed", "Iteration limit reached; retry to continue.")
                else:
                    self.archive.merge_metadata(f"reply:{message_id}", result.metadata())
            self._release_thread(thread)
        if self._backup is not None and result.touched:
            self._backup.schedule_commit(result.touched, trigger=note, response=reply)
        return reply

    async def run_job(self, prompt: str) -> str:
        """Run a scheduled-job prompt on the unthreaded jobs history.

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

        async def counting_send(text: str) -> None:
            nonlocal delivered
            if base_send:
                await base_send(text)
            delivered += 1

        reply = await self._run(
            None,
            f"[scheduled run] {prompt}",
            transient_context=self._job_state_snapshot(prompt),
            send_message_fn=counting_send,
            response_format=_JOB_CLOSE_RESPONSE_FORMAT,
        )
        if reply == MAX_ITERATIONS_REPLY:
            if base_send:
                await counting_send(_JOB_CAP_NOTICE.format(prompt=_clip(prompt, _JOB_CAP_PROMPT_CHARS)))
            return reply
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


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, copilot.CopilotUnavailableError):
        return "unavailable"
    if isinstance(exc, asyncio.CancelledError):
        return "interrupted"
    return "failed"
