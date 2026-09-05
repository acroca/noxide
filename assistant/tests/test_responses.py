"""Tests for the chat-completions ⇄ Responses API translation layer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.responses import (
    OUTPUT_KEY,
    StreamError,
    build_payload,
    parse_response,
    read_sse,
)

_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a vault file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


# ------------------------------------------------------------------
# build_payload: chat-shaped history → /responses request
# ------------------------------------------------------------------


def test_build_payload_moves_system_prompt_to_instructions() -> None:
    payload = build_payload(
        "gpt-6-astra",
        [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}],
        tools=None,
        response_format=None,
    )

    assert payload["model"] == "gpt-6-astra"
    assert payload["instructions"] == "Be terse."
    assert payload["input"] == [{"role": "user", "content": "hi"}]


def test_build_payload_is_stateless_and_streams() -> None:
    payload = build_payload("m", [{"role": "user", "content": "hi"}], None, None)

    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "messages" not in payload


def test_build_payload_replays_a_responses_turn_verbatim() -> None:
    """An assistant turn that came from /responses carries its raw output
    array; it is replayed as-is (ids, reasoning, interleaving intact) and the
    tool result becomes a function_call_output."""
    output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path": "a.md"}',
            "status": "completed",
        },
    ]
    messages = [
        {"role": "user", "content": "read a.md"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.md"}'},
                }
            ],
            OUTPUT_KEY: output,
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "# A\n"},
    ]

    payload = build_payload("m", messages, None, None)

    assert payload["input"] == [
        {"role": "user", "content": "read a.md"},
        *output,
        {"type": "function_call_output", "call_id": "call_1", "output": "# A\n"},
    ]


def test_build_payload_keeps_interleaved_output_order() -> None:
    """The API requires each reasoning item to be immediately followed by the
    item it produced — a preamble message then a call must not be regrouped."""
    output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "A"},
        {"type": "message", "id": "msg_1", "role": "assistant",
         "content": [{"type": "output_text", "text": "Checking."}]},
        {"type": "reasoning", "id": "rs_2", "encrypted_content": "B"},
        {"type": "function_call", "id": "fc_1", "call_id": "c", "name": "f", "arguments": "{}"},
    ]
    messages = [{
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        OUTPUT_KEY: output,
    }]

    payload = build_payload("m", messages, None, None)

    assert payload["input"] == output


def test_build_payload_reconstructs_chat_origin_turns() -> None:
    """A turn produced on the chat endpoint has no stored output; its text
    and tool calls are rebuilt as a message item and function_call items."""
    messages = [
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
    ]

    payload = build_payload("m", messages, None, None)

    assert payload["input"] == [
        {"role": "assistant", "content": "Looking."},
        {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
    ]


def test_build_payload_drops_chat_only_reasoning_fields() -> None:
    """reasoning_text/reasoning_opaque come from the chat endpoint (another model
    or another endpoint); /responses cannot use them and must not see them."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "hello",
            "reasoning_text": "thinking",
            "reasoning_opaque": "OPAQUE",
        },
    ]

    payload = build_payload("m", messages, None, None)

    assert payload["input"][1] == {"role": "assistant", "content": "hello"}


def test_build_payload_flattens_tool_schemas() -> None:
    payload = build_payload("m", [{"role": "user", "content": "hi"}], [_TOOL], None)

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a vault file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert payload["tool_choice"] == "auto"


def test_build_payload_omits_tools_when_none() -> None:
    payload = build_payload("m", [{"role": "user", "content": "hi"}], None, None)

    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_build_payload_maps_response_format_to_text_format() -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "job_close", "strict": True, "schema": {"type": "object"}},
    }

    payload = build_payload("m", [{"role": "user", "content": "hi"}], None, response_format)

    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "job_close",
            "strict": True,
            "schema": {"type": "object"},
        }
    }


def test_build_payload_omits_text_format_when_no_response_format() -> None:
    payload = build_payload("m", [{"role": "user", "content": "hi"}], None, None)

    assert "text" not in payload


def test_build_payload_converts_multimodal_user_content() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        }
    ]

    payload = build_payload("m", messages, None, None)

    assert payload["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ],
        }
    ]


# ------------------------------------------------------------------
# parse_response: completed /responses object → chat-shaped response
# ------------------------------------------------------------------

_USAGE = {
    "input_tokens": 150,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 40},
    "output_tokens": 179,
    "output_tokens_details": {"reasoning_tokens": 131},
    "total_tokens": 329,
}


def _completed(output: list[dict], status: str = "completed") -> dict:
    return {"status": status, "output": output, "usage": _USAGE, "incomplete_details": None}


def test_parse_response_turns_function_calls_into_tool_calls() -> None:
    reasoning = {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC", "summary": []}
    response = _completed([
        reasoning,
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path": "a.md"}',
            "status": "completed",
        },
    ])

    result = parse_response(response)

    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.md"}'},
            }
        ],
        OUTPUT_KEY: response["output"],
    }


def test_parse_response_collects_message_text() -> None:
    response = _completed([
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "Hel", "annotations": []},
                {"type": "output_text", "text": "lo", "annotations": []},
            ],
        }
    ])

    result = parse_response(response)

    choice = result["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {
        "role": "assistant",
        "content": "Hello",
        OUTPUT_KEY: response["output"],
    }


def test_parse_response_separates_message_items() -> None:
    """Parts inside one message item are fragments of one text; distinct
    message items (a preamble, then the answer) are distinct paragraphs."""
    response = _completed([
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Checking."}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "22°C."}]},
    ])

    result = parse_response(response)

    assert result["choices"][0]["message"]["content"] == "Checking.\n\n22°C."


def test_parse_response_omits_output_key_for_empty_output() -> None:
    result = parse_response(_completed([]))

    assert OUTPUT_KEY not in result["choices"][0]["message"]


def test_parse_response_maps_usage_to_chat_field_names() -> None:
    result = parse_response(_completed([]))

    assert result["usage"] == {
        "prompt_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens": 179,
    }


def test_parse_response_incomplete_status_is_length() -> None:
    result = parse_response(_completed([], status="incomplete"))

    assert result["choices"][0]["finish_reason"] == "length"


def test_parse_response_normalizes_empty_arguments() -> None:
    response = _completed([
        {"type": "function_call", "call_id": "c", "name": "list_files", "arguments": ""}
    ])

    result = parse_response(response)

    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_parse_response_rejects_function_call_without_call_id() -> None:
    response = _completed([{"type": "function_call", "name": "f", "arguments": "{}"}])

    with pytest.raises(StreamError, match="call_id"):
        parse_response(response)


def test_parse_response_rejects_malformed_arguments() -> None:
    response = _completed([
        {"type": "function_call", "call_id": "c", "name": "f", "arguments": '{"path": '}
    ])

    with pytest.raises(StreamError):
        parse_response(response)


# ------------------------------------------------------------------
# read_sse: event stream → chat-shaped response
# ------------------------------------------------------------------


class _Stream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _event(type_: str, **fields) -> list[str]:
    return [f"event: {type_}", f"data: {json.dumps({'type': type_, **fields})}", ""]


async def test_read_sse_uses_the_completed_event_and_ignores_deltas() -> None:
    message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }
    lines = [
        *_event("response.created", response={"status": "in_progress"}),
        *_event("response.output_text.delta", delta="do"),
        *_event("response.output_text.delta", delta="ne"),
        *_event("response.completed", response=_completed([message])),
    ]

    result = await read_sse(_Stream(lines))

    assert result["choices"][0]["message"]["content"] == "done"
    assert result["usage"]["prompt_tokens"] == 150


async def test_read_sse_without_completed_event_is_truncated() -> None:
    lines = [
        *_event("response.created", response={"status": "in_progress"}),
        *_event("response.output_text.delta", delta="do"),
    ]

    with pytest.raises(StreamError, match="truncated"):
        await read_sse(_Stream(lines))


async def test_read_sse_incomplete_response_is_a_length_finish() -> None:
    """response.incomplete is the terminal event for a capped or filtered
    reply — it carries the whole response and must not look like an outage."""
    message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "partial"}],
    }
    lines = _event("response.incomplete", response=_completed([message], status="incomplete"))

    result = await read_sse(_Stream(lines))

    assert result["choices"][0]["finish_reason"] == "length"
    assert result["choices"][0]["message"]["content"] == "partial"


async def test_read_sse_surfaces_failed_response() -> None:
    lines = _event(
        "response.failed",
        response={"status": "failed", "error": {"code": "server_error", "message": "boom"}},
    )

    with pytest.raises(StreamError, match="boom"):
        await read_sse(_Stream(lines))


async def test_read_sse_surfaces_error_event() -> None:
    lines = _event("error", code="rate_limit", message="slow down")

    with pytest.raises(StreamError, match="slow down"):
        await read_sse(_Stream(lines))


async def test_read_sse_skips_unparseable_lines() -> None:
    lines = [
        "data: {not json",
        *_event("response.completed", response=_completed([])),
    ]

    result = await read_sse(_Stream(lines))

    assert result["choices"][0]["finish_reason"] == "stop"
