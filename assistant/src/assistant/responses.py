"""Translate between the bot's chat-completions-shaped history and the
Responses API dialect Copilot serves newer OpenAI models on.

Everything above the client (history, compaction, retry queue, sub-agents)
speaks chat completions; this module converts at the client boundary in both
directions so nothing else has to know which endpoint a model lives on.
Pure functions, no I/O.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Private key on an assistant history message holding the raw output array a
# /responses turn produced. It is replayed verbatim — item ids, encrypted
# reasoning, and the interleaving the API requires (each reasoning item
# immediately followed by the item it produced) — instead of being rebuilt
# from the chat-shaped fields. The chat endpoint never sees the key, and this
# endpoint never sees chat's reasoning_text/reasoning_opaque.
OUTPUT_KEY = "responses_output"


class StreamError(RuntimeError):
    """The event stream ended without a usable response (truncated, failed,
    or malformed). The client maps it to its transient error for retry."""


def build_payload(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
) -> dict[str, Any]:
    """A /responses request body equivalent to a chat-completions one."""
    instructions = [m["content"] for m in messages if m.get("role") == "system"]
    payload: dict[str, Any] = {
        "model": model,
        "input": _input_items(m for m in messages if m.get("role") != "system"),
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if tools:
        payload["tools"] = [_flatten_tool(t) for t in tools]
        payload["tool_choice"] = "auto"
    if response_format:
        payload["text"] = {"format": _text_format(response_format)}
    return payload


def _input_items(messages) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m["tool_call_id"],
                "output": m.get("content") or "",
            })
        elif role == "assistant":
            items.extend(_assistant_items(m))
        else:
            items.append({"role": role, "content": _content(m.get("content"))})
    return items


def _assistant_items(m: dict[str, Any]) -> list[dict[str, Any]]:
    """A turn from this endpoint replays verbatim; a chat-origin turn (no
    stored output) is rebuilt from its text and tool calls."""
    if m.get(OUTPUT_KEY):
        return list(m[OUTPUT_KEY])
    items: list[dict[str, Any]] = []
    if m.get("content"):
        items.append({"role": "assistant", "content": m["content"]})
    for tc in m.get("tool_calls") or []:
        fn = tc["function"]
        items.append({
            "type": "function_call",
            "call_id": tc["id"],
            "name": fn["name"],
            "arguments": fn["arguments"],
        })
    return items


def _content(content: Any) -> Any:
    """Chat content (string or parts list) → Responses content; text passes through."""
    if not isinstance(content, list):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") == "image_url":
            parts.append({"type": "input_image", "image_url": part["image_url"]["url"]})
        elif part.get("type") == "text":
            parts.append({"type": "input_text", "text": part["text"]})
        else:
            parts.append(part)
    return parts


def _flatten_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", **(tool.get("function") or {})}


def _text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    schema = response_format.get("json_schema") or {}
    return {"type": response_format.get("type", "json_schema"), **schema}


# ------------------------------------------------------------------
# Incoming: /responses stream → chat-shaped response
# ------------------------------------------------------------------


async def read_sse(r: Any) -> dict[str, Any]:
    """Aggregate a /responses event stream into a chat-completions-shaped dict.

    Only the terminal event matters — ``response.completed``, or
    ``response.incomplete`` for a capped or filtered reply — since it carries
    the whole output array; deltas are skipped rather than merged. A stream
    that ends without one was cut off; a partial message would poison history.
    """
    async for line in r.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable SSE event: %.200s", data)
            continue
        kind = event.get("type")
        if kind in ("response.completed", "response.incomplete"):
            return parse_response(event.get("response") or {})
        if kind == "error":
            raise StreamError(f"error event: {event.get('message') or event}")
        if kind == "response.failed":
            error = (event.get("response") or {}).get("error") or {}
            raise StreamError(f"{kind}: {error.get('message') or error}")
    raise StreamError("stream truncated (no response.completed)")


def parse_response(response: dict[str, Any]) -> dict[str, Any]:
    """A terminal /responses object as a chat-completions response dict.

    Text parts inside one message item are fragments of one text; distinct
    message items (a preamble, then the answer) become paragraphs.
    """
    output = list(response.get("output") or [])
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        kind = item.get("type")
        if kind == "message":
            texts.append("".join(
                p.get("text") or "" for p in item.get("content") or []
                if p.get("type") == "output_text"
            ))
        elif kind == "function_call":
            tool_calls.append(_tool_call(item))

    message: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if output:
        message[OUTPUT_KEY] = output

    if response.get("status") == "incomplete":
        finish_reason = "length"
    elif tool_calls:
        finish_reason = "tool_calls"
    else:
        finish_reason = "stop"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": _usage(response.get("usage") or {}),
    }


def _tool_call(item: dict[str, Any]) -> dict[str, Any]:
    args = (item.get("arguments") or "").strip() or "{}"
    try:
        json.loads(args)
        return {
            "id": item["call_id"],
            "type": "function",
            "function": {"name": item["name"], "arguments": args},
        }
    except json.JSONDecodeError:
        raise StreamError(
            f"malformed tool call arguments for {item.get('name')!r}"
        ) from None
    except KeyError as exc:
        raise StreamError(f"function_call item missing {exc}") from None


def _usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Responses token counts under the chat field names usage tracking reads."""
    details = usage.get("input_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": details.get("cached_tokens", 0)},
        "completion_tokens": usage.get("output_tokens", 0),
    }
