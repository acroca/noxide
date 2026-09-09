"""Small automatic context backed by a retrievable, process-local transcript."""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any

_HISTORY_TOOL_RESULT_CAP = 2000
_HISTORY_TRIM_MARKER = (
    "\n[older tool output trimmed from history - call the tool again "
    "if you need the full content]"
)
_EXCHANGE_CHARS = 6000
_PAGE_CHARS = 12000


def history_tool_schemas() -> list[dict[str, Any]]:
    tools = []
    for name, description in (
        ("get_history", "Read completed conversation messages, newest page first, or page one message by ID."),
        ("search_history", "Search completed conversation messages by case-insensitive literal text."),
    ):
        properties: dict[str, Any] = {
            "before_id": {"type": "integer", "minimum": 1,
                          "description": "Exclusive cursor for older messages; omit for latest."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                      "description": "Maximum messages (default 10)."},
        }
        if name == "get_history":
            properties.update({
                "message_id": {"type": "integer", "minimum": 1,
                               "description": "Read this exact message; do not combine with before_id."},
                "offset": {"type": "integer", "minimum": 0,
                           "description": "Character offset within message_id (default 0)."},
            })
        else:
            properties["query"] = {"type": "string", "minLength": 1, "maxLength": 400}
        tools.append({"type": "function", "function": {
            "name": name,
            "description": description + (
                " Only this chat/topic, since restart or /clear. Returns text, IDs, timestamps "
                "and pagination; no tool traces or images. Pages cap at 12k content characters; "
                "list/search excerpts cap at 2k each. Read truncated text with message_id and "
                "offset in get_history. Not a source of current vault state."
            ),
            "parameters": {"type": "object", "properties": properties,
                           "required": ["query"] if name == "search_history" else [],
                           "additionalProperties": False},
        }})
    return tools


class ConversationHistory:
    """Archive completed text exchanges; never window unfinished protocol work."""

    def __init__(self, exchanges: int = 5) -> None:
        if exchanges < 1:
            raise ValueError("history_exchanges must be positive")
        self._window = exchanges
        self._unfinished = False
        self._history: deque[dict[str, Any]] = deque()
        self._exchanges: list[list[dict[str, Any]]] = []
        self._transcript: list[dict[str, Any]] = []

    def begin_run(self) -> None:
        if not self._unfinished:
            # Terminal rejections keep their real tail for retry supersession,
            # but their oversized outputs must not poison the next request.
            self.compact_tool_results()
        self._unfinished = True

    def finish_run(self, *, success: bool = True, timestamp: str = "") -> None:
        if success:
            exchange = []
            for message in self._history:
                if message.get("role") not in ("user", "assistant"):
                    continue
                if message.get("tool_calls") or message.get("function_call"):
                    continue
                content = message.get("content") or ""
                if not isinstance(content, str):
                    continue
                record = {"id": len(self._transcript) + 1, "role": message["role"],
                          "content": content, "completed_at": timestamp}
                self._transcript.append(record)
                exchange.append(record)
            if exchange:
                self._exchanges.append(exchange)
            self._history.clear()
        self._unfinished = False

    def append(self, msg: dict[str, Any]) -> None:
        self._history.append(msg)

    def messages(self) -> list[dict[str, Any]]:
        messages = []
        for exchange in self._exchanges[-self._window:]:
            allowance = max(1, _EXCHANGE_CHARS // len(exchange))
            for record in exchange:
                content = record["content"]
                if len(content) > allowance:
                    content = content[:allowance] + (
                        f"\n[message truncated; get_history message_id={record['id']} "
                        f"offset={allowance} for the rest]"
                    )
                messages.append({"role": record["role"], "content": content})
        # Keep the actual tail last: hot retries use it to distinguish pending
        # work from a completed reply, and transient images use object identity.
        return messages + list(self._history)

    def coverage(self) -> str | None:
        omitted = len(self._exchanges) - self._window
        if omitted <= 0:
            return None
        first = self._exchanges[-self._window][0]["id"]
        return (f"[conversation history: {omitted} older exchanges omitted; "
                f"get_history before_id={first} or search_history can retrieve them. "
                "History is available only since restart or /clear.]")

    def compact_tool_results(self) -> None:
        for i, msg in enumerate(self._history):
            content = msg.get("content")
            if msg.get("role") == "tool" and isinstance(content, str) and len(content) > _HISTORY_TOOL_RESULT_CAP:
                self._history[i] = {
                    **msg, "content": content[:_HISTORY_TOOL_RESULT_CAP] + _HISTORY_TRIM_MARKER,
                }

    def pop_if_last(self, msg: dict[str, Any]) -> bool:
        if self._history and self._history[-1] is msg:
            self._history.pop()
            return True
        return False

    def retrieve(self, name: str, args: dict[str, Any]) -> str:
        allowed = {"before_id", "limit", "query"} if name == "search_history" else {
            "before_id", "limit", "message_id", "offset",
        }
        if args.keys() - allowed:
            raise ValueError("unknown history arguments; history is scoped to this conversation")
        for key in ("before_id", "limit", "message_id", "offset"):
            if key in args and (type(args[key]) is not int or args[key] < (0 if key == "offset" else 1)):
                raise ValueError(f"invalid {key}")
        limit = args.get("limit", 10)
        if limit > 20:
            raise ValueError("limit must be at most 20")
        if "message_id" in args:
            if "before_id" in args:
                raise ValueError("message_id and before_id cannot be combined")
            message_id = args["message_id"]
            if message_id > len(self._transcript):
                return "[history message not found]"
            record = self._transcript[message_id - 1]
            offset = args.get("offset", 0)
            end = offset + _PAGE_CHARS
            return json.dumps({**record, "content": record["content"][offset:end],
                               "offset": offset,
                               "next_offset": end if end < len(record["content"]) else None},
                              ensure_ascii=False)
        if "offset" in args:
            raise ValueError("offset requires message_id")
        query = args.get("query", "")
        if name == "search_history" and (not isinstance(query, str) or not query.strip() or len(query) > 400):
            raise ValueError("query must contain 1-400 characters")
        records = []
        remaining = _PAGE_CHARS
        more = False
        end = min(args.get("before_id", len(self._transcript) + 1) - 1, len(self._transcript))
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for index in range(end - 1, -1, -1):
            record = self._transcript[index]
            content = record["content"]
            match = pattern.search(content)
            if match is None:
                continue
            if len(records) == limit or remaining <= 0:
                more = True
                break
            offset = max(0, match.start() - 200) if query else 0
            excerpt = content[offset:offset + min(2000, remaining)]
            records.append({**record, "content": excerpt, "offset": offset,
                            "truncated": offset > 0 or offset + len(excerpt) < len(content)})
            remaining -= len(excerpt)
        return json.dumps({"messages": list(reversed(records)),
                           "next_before_id": records[-1]["id"] if more else None,
                           "scope": "this conversation, since restart or /clear"}, ensure_ascii=False)
