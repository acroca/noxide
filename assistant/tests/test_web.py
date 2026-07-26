"""Tests for web research: SearXNG search, guarded page fetch, quarantined researcher."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.agent import Agent
from assistant.tools import VaultTools
from assistant.web import Researcher, WebTools

_SEARXNG_URL = "http://searxng:8080"


def _make_text_response(content: str) -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _make_tool_call_response(tool_name: str, tool_args: dict, call_id: str = "tc1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }


@pytest.fixture
def web() -> WebTools:
    return WebTools(_SEARXNG_URL)


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every hostname resolves to a public address."""

    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr("assistant.web._resolve_host", fake_resolve)


# ------------------------------------------------------------------
# web_search
# ------------------------------------------------------------------

@respx.mock
async def test_web_search_formats_results(web: WebTools) -> None:
    respx.get(f"{_SEARXNG_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Best e-bike locks",
                        "url": "https://example.com/locks",
                        "content": "A review of the toughest locks.",
                    },
                    {
                        "title": "Lock test 2026",
                        "url": "https://example.org/test",
                        "content": "Independent lab results.",
                    },
                ]
            },
        )
    )

    out = await web.web_search("best e-bike locks")

    assert "Best e-bike locks" in out
    assert "https://example.com/locks" in out
    assert "A review of the toughest locks." in out
    assert "Lock test 2026" in out


@respx.mock
async def test_web_search_limits_result_count(web: WebTools) -> None:
    many = [
        {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": "x"}
        for i in range(30)
    ]
    respx.get(f"{_SEARXNG_URL}/search").mock(
        return_value=httpx.Response(200, json={"results": many})
    )

    out = await web.web_search("anything")

    assert "Result 0" in out
    assert "Result 29" not in out


@respx.mock
async def test_web_search_no_results(web: WebTools) -> None:
    respx.get(f"{_SEARXNG_URL}/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    out = await web.web_search("gibberish qzxv")

    assert out == "[no results]"


@respx.mock
async def test_web_search_backend_error_returns_error_string(web: WebTools) -> None:
    respx.get(f"{_SEARXNG_URL}/search").mock(return_value=httpx.Response(500))

    out = await web.web_search("anything")

    assert out.startswith("[tool error:")


@respx.mock
async def test_web_search_backend_unreachable_returns_error_string(web: WebTools) -> None:
    respx.get(f"{_SEARXNG_URL}/search").mock(side_effect=httpx.ConnectError("refused"))

    out = await web.web_search("anything")

    assert out.startswith("[tool error:")


# ------------------------------------------------------------------
# fetch_page: SSRF guards
# ------------------------------------------------------------------

async def test_fetch_page_blocks_non_http_schemes(web: WebTools) -> None:
    assert "[fetch blocked" in await web.fetch_page("file:///etc/passwd")
    assert "[fetch blocked" in await web.fetch_page("ftp://example.com/x")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://192.168.1.10/router",
        "http://10.0.0.5/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
    ],
)
async def test_fetch_page_blocks_private_literal_ips(web: WebTools, url: str) -> None:
    out = await web.fetch_page(url)

    assert "[fetch blocked" in out


async def test_fetch_page_blocks_hostname_resolving_to_private(
    web: WebTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["10.0.0.5"]

    monkeypatch.setattr("assistant.web._resolve_host", fake_resolve)

    out = await web.fetch_page("http://internal.example.com/secrets")

    assert "[fetch blocked" in out


@respx.mock
async def test_fetch_page_blocks_redirect_to_private(web: WebTools, public_dns: None) -> None:
    respx.get("https://93.184.216.34/page").mock(
        return_value=httpx.Response(302, headers={"location": "http://192.168.1.1/router"})
    )

    out = await web.fetch_page("https://example.com/page")

    assert "[fetch blocked" in out


# ------------------------------------------------------------------
# fetch_page: extraction
# ------------------------------------------------------------------

_HTML = """
<html><head><title>Locks</title><script>var x = 1;</script></head>
<body>
<article>
<h1>The great lock review</h1>
<p>The Kryptonite New-U resisted the angle grinder for ninety seconds.</p>
<p>The cheaper cable locks failed in under five seconds.</p>
</article>
</body></html>
"""


@respx.mock
async def test_fetch_page_extracts_readable_text(web: WebTools, public_dns: None) -> None:
    route = respx.get("https://93.184.216.34/locks").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_HTML)
    )

    out = await web.fetch_page("https://example.com/locks")

    assert "resisted the angle grinder" in out
    assert "<p>" not in out
    assert "var x = 1" not in out
    # The connection must be pinned to the vetted IP (DNS-rebinding defence)
    # while the Host header still names the original site.
    assert route.calls[0].request.headers["host"] == "example.com"


@respx.mock
async def test_fetch_page_connects_to_vetted_ip_not_hostname(
    web: WebTools, public_dns: None
) -> None:
    """No request may go out by hostname: httpx re-resolving it independently
    would reopen the DNS-rebinding window the guard closed."""
    respx.get("https://93.184.216.34/locks").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_HTML)
    )
    hostname_route = respx.get(url__regex=r"https://example\.com/.*").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=_HTML)
    )

    await web.fetch_page("https://example.com/locks")

    assert not hostname_route.called


@respx.mock
async def test_fetch_page_returns_plain_text_directly(web: WebTools, public_dns: None) -> None:
    respx.get("https://93.184.216.34/notes.txt").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/plain"}, text="just plain text"
        )
    )

    out = await web.fetch_page("https://example.com/notes.txt")

    assert "just plain text" in out


@respx.mock
async def test_fetch_page_survives_bogus_charset(web: WebTools, public_dns: None) -> None:
    respx.get("https://93.184.216.34/weird").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html; charset=bogus-charset"},
            content=_HTML.encode(),
        )
    )

    out = await web.fetch_page("https://example.com/weird")

    assert "resisted the angle grinder" in out


@respx.mock
async def test_fetch_page_truncates_long_pages(web: WebTools, public_dns: None) -> None:
    body = "<html><body><p>" + ("lorem ipsum " * 5000) + "</p></body></html>"
    respx.get("https://93.184.216.34/long").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=body)
    )

    out = await web.fetch_page("https://example.com/long")

    assert len(out) <= 16_000
    assert "truncated" in out


@respx.mock
async def test_fetch_page_http_error_returns_error_string(web: WebTools, public_dns: None) -> None:
    respx.get("https://93.184.216.34/gone").mock(return_value=httpx.Response(404))

    out = await web.fetch_page("https://example.com/gone")

    assert out.startswith("[tool error:")


# ------------------------------------------------------------------
# Researcher: quarantined sub-agent
# ------------------------------------------------------------------

@respx.mock
async def test_researcher_searches_then_summarizes(web: WebTools) -> None:
    respx.get(f"{_SEARXNG_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "T", "url": "https://e.com", "content": "snippet"}]},
        )
    )
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("web_search", {"query": "e-bike locks"}),
        _make_text_response("Summary: buy the New-U. Source: https://e.com"),
    ])
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await researcher.research("What is the best e-bike lock?")

    assert "Summary: buy the New-U" in out
    # The search result must have been fed back to the model
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("snippet" in m["content"] for m in tool_msgs)


async def test_researcher_only_exposes_web_tools(web: WebTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await researcher.research("anything")

    tools = mock_client.chat.call_args[0][1]
    names = {t["function"]["name"] for t in tools}
    assert names == {"web_search", "fetch_page"}


async def test_researcher_requests_are_agent_initiated(web: WebTools) -> None:
    """Research runs inside an existing user turn — its calls must not be
    billed as user-initiated premium requests."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await researcher.research("anything")

    assert mock_client.chat.call_args.kwargs["initiator"] == "agent"


async def test_researcher_uses_own_system_prompt(web: WebTools) -> None:
    """The researcher's context must contain only its fixed prompt and the question."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("done"))
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        await researcher.research("What year is it?")

    messages = mock_client.chat.call_args[0][0]
    assert messages[0]["role"] == "system"
    assert "research" in messages[0]["content"].lower()
    assert [m["role"] for m in messages[1:]] == ["user"]
    assert messages[1]["content"] == "What year is it?"


async def test_researcher_rejects_overlong_question(web: WebTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock()
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await researcher.research("x" * 401)

    assert out.startswith("[tool error:")
    mock_client.chat.assert_not_called()


async def test_researcher_iteration_cap(web: WebTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(
        return_value=_make_tool_call_response("fetch_page", {"url": "file:///x"})
    )
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await researcher.research("loop forever")

    assert out.startswith("[tool error:")
    assert mock_client.chat.call_count == 8


async def test_researcher_truncates_summary(web: WebTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=_make_text_response("y" * 50_000))
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await researcher.research("long answer please")

    assert len(out) <= 7_000


async def test_researcher_survives_model_error(web: WebTools) -> None:
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=RuntimeError("copilot down"))
    researcher = Researcher(web)

    with patch("assistant.copilot.get_client", return_value=mock_client):
        out = await researcher.research("anything")

    assert out.startswith("[tool error:")


# ------------------------------------------------------------------
# Agent integration: the `research` tool
# ------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path: Path) -> VaultTools:
    return VaultTools(tmp_path)


def test_agent_registers_research_tool_when_fn_present(vault: VaultTools) -> None:
    async def fake_research(question: str) -> str:
        return "ok"

    agent = Agent(vault_tools=vault, research_fn=fake_research)

    names = {t["function"]["name"] for t in agent._all_tools()}
    assert "research" in names


def test_agent_omits_research_tool_without_fn(vault: VaultTools) -> None:
    agent = Agent(vault_tools=vault)

    names = {t["function"]["name"] for t in agent._all_tools()}
    assert "research" not in names


async def test_agent_dispatches_research_tool(vault: VaultTools) -> None:
    questions: list[str] = []

    async def fake_research(question: str) -> str:
        questions.append(question)
        return "The answer is 42. Source: https://example.com"

    agent = Agent(vault_tools=vault, research_fn=fake_research, history_size=10)
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=[
        _make_tool_call_response("research", {"question": "answer to everything?"}),
        _make_text_response("It's 42."),
    ])

    with patch("assistant.copilot.get_client", return_value=mock_client):
        reply = await agent.run(chat_id=1, user_message="what is the answer?")

    assert questions == ["answer to everything?"]
    assert reply == "It's 42."
    second_call_messages = mock_client.chat.call_args_list[1][0][0]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("The answer is 42" in m["content"] for m in tool_msgs)


# ------------------------------------------------------------------
# Usage recording
# ------------------------------------------------------------------

async def test_research_records_usage_event() -> None:
    researcher = Researcher(WebTools(_SEARXNG_URL))
    mock_client = MagicMock()
    response = _make_text_response("answer")
    response["model"] = "claude-sonnet-4.6"
    mock_client.chat = AsyncMock(return_value=response)

    with patch("assistant.copilot.get_client", return_value=mock_client), \
         patch("assistant.web.usage") as mock_usage:
        await researcher.research("what is love?")

    mock_usage.record.assert_called_once_with(
        "research", "claude-sonnet-4.6", {"prompt_tokens": 10, "completion_tokens": 5}
    )
