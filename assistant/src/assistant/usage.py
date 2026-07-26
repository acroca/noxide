"""Token usage tracking: append-only JSONL store + rendered vault view.

``record()`` is synchronous and in-memory only (queue put) so call sites add
zero latency; a single background task (``run``) drains the queue, appends
events to ``state_dir/usage/usage-YYYY-MM.jsonl``, and regenerates the
rolling ``system/usage.md`` view in the vault.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Coalesce a burst of events (one agent run = several API calls) into one flush
_DEBOUNCE_SECONDS = 5.0

_VIEW_DAYS = 7


class UsageTracker:
    """Queues usage events in memory; a background task persists them."""

    def __init__(self, state_dir: Path, vault_path: Path, tz_name: str) -> None:
        self._usage_dir = state_dir / "usage"
        self._view_path = vault_path / "system" / "usage.md"
        self._tz = ZoneInfo(tz_name)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    def record(
        self,
        feature: str,
        model: str,
        usage: dict[str, Any],
        chat_id: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        """Queue one usage event. Never touches disk, never raises to callers."""
        details = usage.get("prompt_tokens_details") or {}
        event = {
            "ts": self._now().isoformat(timespec="seconds"),
            "feature": feature,
            "model": model,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_tokens": int(details.get("cached_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }
        self._queue.put_nowait(event)

    async def run(self) -> None:
        """Background loop: wait for events, debounce, flush to disk."""
        while True:
            events = [await self._queue.get()]
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            while not self._queue.empty():
                events.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self._flush, events)
            except Exception:
                logger.exception("usage flush failed")

    async def drain(self) -> None:
        """Flush anything still queued (used at shutdown and in tests)."""
        events: list[dict[str, Any]] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        if not events:
            return
        try:
            await asyncio.to_thread(self._flush, events)
        except Exception:
            logger.exception("usage flush failed")

    # ------------------------------------------------------------------
    # Sync internals — called via asyncio.to_thread
    # ------------------------------------------------------------------

    def _flush(self, events: list[dict[str, Any]]) -> None:
        self._append(events)
        self._render()

    def _append(self, events: list[dict[str, Any]]) -> None:
        self._usage_dir.mkdir(parents=True, exist_ok=True)
        by_file: dict[Path, list[str]] = defaultdict(list)
        for event in events:
            month = event["ts"][:7]  # YYYY-MM
            by_file[self._usage_dir / f"usage-{month}.jsonl"].append(json.dumps(event))
        for path, lines in by_file.items():
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    def _render(self) -> None:
        """Regenerate the rolling vault view from recent JSONL files."""
        today = self._now().date()
        cutoff = (today - timedelta(days=_VIEW_DAYS - 1)).isoformat()
        # (day, feature, model) -> [requests, tokens_in, cached, tokens_out]
        totals: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        months = sorted({cutoff[:7], today.isoformat()[:7]})
        for month in months:
            path = self._usage_dir / f"usage-{month}.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    day = event["ts"][:10]
                    if day < cutoff:
                        continue
                    agg = totals[(day, event["feature"], event["model"])]
                    agg[0] += 1
                    agg[1] += int(event["prompt_tokens"])
                    # Events predate the cached column — treat missing as 0
                    agg[2] += int(event.get("cached_tokens") or 0)
                    agg[3] += int(event["completion_tokens"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # malformed line — skip, never fatal
        lines = [
            f"# Usage (last {_VIEW_DAYS} days)",
            "",
            "| day | feature | model | requests | tokens in | cached | tokens out |",
            "|-----|---------|-------|----------|-----------|--------|------------|",
        ]
        rows = sorted(totals.items(), key=lambda kv: (kv[0][1], kv[0][2]))
        rows.sort(key=lambda kv: kv[0][0], reverse=True)
        for (day, feature, model), (requests, tokens_in, cached, tokens_out) in rows:
            lines.append(
                f"| {day} | {feature} | {model} | {requests} "
                f"| {tokens_in:,} | {cached:,} | {tokens_out:,} |"
            )
        self._view_path.parent.mkdir(parents=True, exist_ok=True)
        self._view_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------
# Module-level singleton helpers
# ------------------------------------------------------------------

_tracker: UsageTracker | None = None


def init(state_dir: Path, vault_path: Path, tz_name: str) -> UsageTracker:
    global _tracker
    _tracker = UsageTracker(state_dir, vault_path, tz_name)
    return _tracker


def get_tracker() -> UsageTracker | None:
    return _tracker


def record(
    feature: str,
    model: str,
    usage: dict[str, Any],
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    """Record a usage event; no-op when tracking is not initialized (tests)."""
    if _tracker is not None:
        _tracker.record(feature, model, usage, chat_id=chat_id, thread_id=thread_id)
