"""Fan-out: apply one instruction to many items via concurrent worker sub-agents.

The main agent processes work turn by turn, so a batch of 50 independent items
costs 50 sequential think-cycles. The `fan_out` tool trades that for wall-clock
parallelism: each item gets its own worker — a fresh-context sub-agent in the
mould of `web.Researcher` — and workers run concurrently under a semaphore.

Workers are deliberately read-only: vault read/list/search, `load_skill`, and
`research` (when web research is enabled) — never write/edit/append, schedule,
or messaging tools. Concurrent writers would race on vault files (lost updates
across their model turns), and keeping integration with the main agent means
one mind owns what actually lands in the vault. Workers see nothing of the
conversation; the instruction string is their entire task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from . import copilot, usage
from .agent import RESEARCH_TOOL_SCHEMA, extract_tool_calls
from .skills import SkillLibrary
from .tools import VaultTools

logger = logging.getLogger(__name__)

_MAX_ITEMS = 50
_MAX_CONCURRENCY = 4
_MAX_INSTRUCTION_CHARS = 2_000
_MAX_ITEM_CHARS = 1_000
# Per-item result cap: with _MAX_ITEMS items the aggregate stays at or below
# read_file's 100k cap, so one fan_out cannot displace the whole conversation.
_MAX_RESULT_CHARS = 2_000
_MAX_WORKER_ITERATIONS = 10
# A worker that uses research waits on a whole sub-agent, so its budget must
# exceed the research tool's own 300s.
_WORKER_TIMEOUT = 600.0
_TOOL_TIMEOUT = 60.0
_RESEARCH_TIMEOUT = 300.0

_READONLY_VAULT_TOOLS = ("read_file", "list_files", "search")

_WORKER_PROMPT = """\
You are a fan-out worker. You receive one instruction and one item, and must
apply the instruction to the item, then reply with the result.

Rules:
- The instruction and item are your entire task; you see nothing of the
  conversation that produced them. Perform no other task.
- Your vault access is read-only. You cannot write files, schedule jobs, or
  message the user — report what you found or decided and let the caller act.
- Reply with only the result: concise, self-contained, no preamble. Long
  replies are truncated.
- If you cannot complete the instruction for this item, say so plainly.
"""


class FanOut:
    """Quarantined bulk processing: one read-only worker per item, run concurrently."""

    def __init__(
        self,
        vault_tools: VaultTools,
        skills: SkillLibrary | None = None,
        research_fn: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        concurrency: int = _MAX_CONCURRENCY,
    ) -> None:
        self._vault = vault_tools
        self._skills = skills
        self._research_fn = research_fn
        self._concurrency = concurrency

    def _worker_prompt(self) -> str:
        parts = [_WORKER_PROMPT.strip()]
        if self._skills:
            menu = self._skills.menu()
            if menu:
                parts.append(menu)
        return "\n\n".join(parts)

    def _worker_tools(self) -> list[dict[str, Any]]:
        tools = [
            s for s in self._vault.tool_schemas()
            if s["function"]["name"] in _READONLY_VAULT_TOOLS
        ]
        if self._skills:
            tools.extend(self._skills.tool_schemas())
        if self._research_fn:
            tools.append(RESEARCH_TOOL_SCHEMA)
        return tools

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name in _READONLY_VAULT_TOOLS:
            return self._vault.dispatch(name, args)
        if name == "load_skill" and self._skills:
            return self._skills.dispatch(name, args)
        if name == "research" and self._research_fn:
            return await self._research_fn(args["question"])
        return f"[unknown tool: {name}]"

    async def run(self, instruction: str, items: list[Any]) -> str:
        """Process *items* concurrently; returns one aggregated result string."""
        if not instruction or not instruction.strip():
            return "[tool error: fan_out requires a non-empty instruction]"
        if len(instruction) > _MAX_INSTRUCTION_CHARS:
            return (
                f"[tool error: instruction too long "
                f"(max {_MAX_INSTRUCTION_CHARS} chars)]"
            )
        if not items:
            return "[tool error: fan_out requires at least one item]"
        if len(items) > _MAX_ITEMS:
            return f"[tool error: too many items ({len(items)}; max {_MAX_ITEMS})]"
        texts = [str(item) for item in items]
        for i, text in enumerate(texts, 1):
            if len(text) > _MAX_ITEM_CHARS:
                return f"[tool error: item {i} too long (max {_MAX_ITEM_CHARS} chars)]"

        logger.info("fan_out items=%d instruction=%r", len(texts), instruction[:200])
        system_prompt = self._worker_prompt()
        tools = self._worker_tools()
        semaphore = asyncio.Semaphore(self._concurrency)

        async def guarded(text: str) -> str:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self._work(system_prompt, tools, instruction, text),
                        timeout=_WORKER_TIMEOUT,
                    )
                except TimeoutError:
                    return f"[item error: worker timed out after {_WORKER_TIMEOUT:.0f}s]"
                except Exception as e:
                    logger.warning("fan_out worker failed for %r", text, exc_info=True)
                    return f"[item error: {e}]"

        results = await asyncio.gather(*(guarded(text) for text in texts))
        blocks = [
            f"### Item {i}: {text}\n{result}"
            for i, (text, result) in enumerate(zip(texts, results, strict=True), 1)
        ]
        errors = sum(1 for r in results if r.startswith("[item error:"))
        header = f"fan_out processed {len(texts)} items ({errors} failed):"
        return header + "\n\n" + "\n\n".join(blocks)

    async def _work(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        instruction: str,
        item: str,
    ) -> str:
        """One worker: fresh context, tool loop until a text reply or the cap."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Instruction:\n{instruction}\n\nItem:\n{item}"},
        ]
        client = copilot.get_client()

        for _ in range(_MAX_WORKER_ITERATIONS):
            response = await client.chat(list(messages), tools, initiator="agent")
            usage.record("fanout", response.get("model", ""), response.get("usage", {}))
            msg = response["choices"][0]["message"]
            messages.append(msg)

            tool_calls = extract_tool_calls(msg)
            if not tool_calls:
                result = msg.get("content") or "[worker returned no result]"
                if len(result) > _MAX_RESULT_CHARS:
                    result = result[:_MAX_RESULT_CHARS] + "\n[truncated]"
                return result

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    fn_args = {}
                try:
                    timeout = _RESEARCH_TIMEOUT if fn_name == "research" else _TOOL_TIMEOUT
                    result = await asyncio.wait_for(
                        self._dispatch(fn_name, fn_args), timeout=timeout
                    )
                except TimeoutError:
                    result = f"[tool {fn_name} timed out after {timeout}s]"
                except PermissionError as e:
                    result = f"[permission denied: {e}]"
                except Exception as e:
                    result = f"[tool error: {e}]"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        return "[item error: worker hit the iteration limit without a result]"
