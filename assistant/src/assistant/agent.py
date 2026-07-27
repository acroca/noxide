"""Agent loop: OpenAI-style tool-calling against GitHub Copilot API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

from . import copilot, usage
from .skills import SkillLibrary
from .tools import VaultTools, slug_from_name

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 20
_TOOL_TIMEOUT = 60.0
# These tools make their own model calls (research sub-agent, vision extraction)
_SLOW_TOOLS = {"research", "extract_attachment"}
_SLOW_TOOL_TIMEOUT = 300.0

# A scheduled run replies with this sentinel (prompts/schedule.md) when it
# finds its purpose already met; run_job then delivers nothing.
_SILENT_SENTINEL = "[silent]"

_TOPIC_INDEX_FILE = "system/topics/index.md"
_TOPIC_INDEX_HEADER = "| topic_id | slug | name |"
_TOPIC_INDEX_SEP = "|----------|------|------|"


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
        send_message_fn: Callable[[str, int | None], Coroutine[Any, Any, None]] | None = None,
        create_forum_topic_fn: Callable[[str], Coroutine[Any, Any, dict[str, Any]]] | None = None,
        research_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        extract_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        skills: SkillLibrary | None = None,
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
        self._skills = skills
        self._histories: dict[tuple[int, int | None], ConversationHistory] = {}
        self._history_size = history_size
        self._tz = ZoneInfo(tz_name)

    def _get_history(self, chat_id: int, thread_id: int | None = None) -> ConversationHistory:
        key = (chat_id, thread_id)
        if key not in self._histories:
            self._histories[key] = ConversationHistory(self._history_size)
        return self._histories[key]

    def clear_history(self, chat_id: int, thread_id: int | None = None) -> None:
        """Forget one chat/topic's conversation; the next run starts fresh."""
        self._histories.pop((chat_id, thread_id), None)

    def _base_prompt(self) -> str:
        """Embedded capability prompt: ships with the code, sections gated by enabled features."""
        sections = [_read_prompt("base.md"), _read_prompt("wiki.md")]
        if self._schedule_dispatcher:
            sections.append(_read_prompt("schedule.md"))
        if self._research_fn:
            sections.append(_read_prompt("research.md"))
        if self._extract_fn:
            sections.append(_read_prompt("extract.md"))
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
            parts = [p.strip() for p in stripped.strip("|").split("|")]
            if len(parts) >= 3:
                try:
                    tid = int(parts[0])
                except ValueError:
                    continue
                if tid == thread_id:
                    return parts[1], parts[2]
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
                f"| {topic_id} | {slug} | {name} |",
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
                parts = [p.strip() for p in stripped.strip("|").split("|")]
                if parts and parts[0].isdigit() and int(parts[0]) == topic_id:
                    found = True
                    new_rows.append(f"| {topic_id} | {slug} | {name} |")
                    continue
            new_rows.append(line)

        if not found:
            # Find last data row and append after it
            new_rows.append(f"| {topic_id} | {slug} | {name} |")

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
            tools.append({
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
            })
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
                                "description": "Display name for the new topic, e.g. 'Health & Fitness'.",
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
        send_message_fn: Callable[[str, int | None], Coroutine[Any, Any, None]] | None = None,
    ) -> str:
        # File tools
        if name in ("read_file", "write_file", "edit_file", "append_file", "list_files", "search"):
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
        # Create forum topic
        if name == "create_forum_topic" and self._create_forum_topic_fn:
            topic_name: str = args["name"]
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
        on_research: Callable[[], Coroutine[Any, Any, None]] | None = None,
        send_message_fn: Callable[[str, int | None], Coroutine[Any, Any, None]] | None = None,
    ) -> str:
        """Run the agent loop for a user message. Returns the final text reply.

        ``on_research`` is awaited once, best-effort, the first time this run
        dispatches the ``research`` tool (e.g. to react to the Telegram message).
        ``send_message_fn`` overrides the constructor-injected sender for this
        run only (used by ``run_job`` to observe deliveries).
        """
        t_start = time.monotonic()
        history = self._get_history(chat_id, thread_id)

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
        stamp = datetime.now(tz=UTC).astimezone(self._tz).strftime("%Y-%m-%d %H:%M local")
        stamped_message = f"[{stamp}] {user_message}"
        user_entry: dict[str, Any] = {"role": "user", "content": stamped_message}
        history.append(user_entry)

        # Images are sent only during this run; stored history keeps the text
        # version so later turns don't re-send image tokens on every request.
        multimodal_entry: dict[str, Any] | None = None
        if image_data_url:
            multimodal_entry = {
                "role": "user",
                "content": [
                    {"type": "text", "text": stamped_message},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }

        tools = self._all_tools()
        client = copilot.get_client()
        total_tool_calls = 0

        for iteration in range(_MAX_ITERATIONS):
            turn_messages = history.messages()
            if multimodal_entry is not None:
                turn_messages = [
                    multimodal_entry if m is user_entry else m for m in turn_messages
                ]
            messages = [{"role": "system", "content": system_prompt}] + turn_messages
            response = await client.chat(messages, tools)

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
            logger.info(
                "agent turn=%d finish=%s tools=%d tokens_in=%s tokens_out=%s duration=%.1fs",
                iteration,
                finish_reason,
                len(tool_calls_in_turn),
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
                return msg.get("content") or ""

            # Execute tool calls
            for tc in tool_calls_in_turn:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    fn_args = {}
                total_tool_calls += 1
                if fn_name == "research" and on_research is not None:
                    try:
                        await on_research()
                    except Exception:
                        logger.warning("on_research callback failed", exc_info=True)
                    on_research = None  # notify at most once per run
                try:
                    timeout = _SLOW_TOOL_TIMEOUT if fn_name in _SLOW_TOOLS else _TOOL_TIMEOUT
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

                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        # Hit iteration cap
        logger.warning("Agent hit max iterations (%d) for chat_id=%d", _MAX_ITERATIONS, chat_id)
        return "[Reached maximum tool-call iterations. Please rephrase your request.]"

    async def run_job(self, prompt: str) -> None:
        """Run a scheduled-job prompt (chat_id 0, no thread).

        The model normally delivers the result itself via the send_message
        tool and then closes its turn with a short confirmation; forwarding
        that closing text would duplicate the message. The final reply is
        sent only when the run delivered nothing — unless it starts with the
        [silent] sentinel, which is how a job whose purpose turned out to be
        already met stands down without messaging the user.
        """
        base_send = self._send_message_fn
        delivered = 0

        async def counting_send(text: str, thread_id: int | None = None) -> None:
            nonlocal delivered
            if base_send:
                await base_send(text, thread_id)
            delivered += 1

        reply = await self.run(chat_id=0, user_message=prompt, send_message_fn=counting_send)
        if reply.strip().lower().startswith(_SILENT_SENTINEL):
            return
        if not delivered and reply and base_send:
            await base_send(reply, None)
