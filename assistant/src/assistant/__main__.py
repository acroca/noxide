"""Entrypoints: `assistant run` and `assistant auth`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Leave headroom after lifecycle's 270-second application drain.
_FINAL_DRAIN_BUDGET = 10.0


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # httpx logs full request URLs at INFO — for Telegram that includes the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run(config_path: Path | None) -> None:
    from . import copilot
    from .agent import Agent
    from .config import load_config
    from .lifecycle import Lifecycle, graceful_shutdown
    from .schedule import Scheduler
    from .telegram_bot import TelegramBot
    from .tools import VaultTools
    from .transcribe import Transcriber

    cfg = load_config(config_path)
    _setup_logging()

    # Fail fast with a readable error instead of crashing mid-startup
    cfg.validate_for_run()

    # Ensure directories exist
    cfg.vault_path.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # Init Copilot client with the configured default, then try the live model
    # catalog: it feeds the /model picker and, when default_family is set,
    # promotes the newest model of that family to default. Best-effort — a
    # failed fetch leaves the configured models in charge.
    from .models import ModelOption, merge_options, resolve_startup

    copilot.init(cfg.state_dir, cfg.models[cfg.default_model])
    model_options = merge_options(cfg.models, [])
    default_alias = cfg.default_model
    try:
        fetched = await copilot.get_client().list_models(cfg.model_vendors)
        model_options, default_alias, default_id = resolve_startup(
            cfg.models, cfg.default_model, fetched, cfg.default_family
        )
        if default_id is not None:
            copilot.get_client().set_model(default_id)
            logging.getLogger(__name__).info(
                "Default model resolved from family %r: %s", cfg.default_family, default_id
            )
    except Exception:
        logging.getLogger(__name__).warning(
            "Model catalog fetch failed; using configured models only", exc_info=True
        )

    async def refresh_models() -> dict[str, ModelOption]:
        return merge_options(
            cfg.models, await copilot.get_client().list_models(cfg.model_vendors)
        )

    # Init usage tracking (JSONL store + vault view, flushed off the critical path)
    from . import usage

    tracker = usage.init(cfg.state_dir, cfg.vault_path, cfg.timezone)

    # Built-in maintenance jobs (nightly compile, weekly lint): the scheduler
    # runs them, and check_vault holds the enabled ones to their cadence.
    from .maintenance import STATE_FILENAME, MaintenanceState, builtin_jobs

    maintenance_jobs = builtin_jobs(cfg.maintenance_compile, cfg.maintenance_lint)

    # Init vault tools
    vault = VaultTools(
        cfg.vault_path,
        tz_name=cfg.timezone,
        maintenance=tuple(job.id for job in maintenance_jobs),
    )

    # Vault backup (optional) — local-only git history of the vault, in a git
    # dir outside it. The startup sweep commits anything from before this boot.
    backup = None
    backup_task: asyncio.Task[None] | None = None
    if cfg.backup_enabled:
        from .backup import VaultBackup

        backup = VaultBackup(cfg.vault_path, cfg.resolved_backup_git_dir())
        await backup.init_repo()
        backup_task = asyncio.create_task(backup.run())
    else:
        logging.getLogger(__name__).info(
            "backup.enabled not set — vault git backup disabled"
        )

    # Voice transcription is optional — enabled when ELEVENLABS_API_KEY is set
    transcriber = (
        Transcriber(cfg.elevenlabs_api_key) if cfg.elevenlabs_api_key.strip() else None
    )
    if transcriber is None:
        logging.getLogger(__name__).info(
            "ELEVENLABS_API_KEY not set — voice messages disabled"
        )

    # Outage retry queue: messages and one-off jobs that failed because
    # Copilot was unreachable are persisted and replayed once it answers
    # again. The closures resolve `agent` and `bot` late — both are
    # constructed below.
    from .retry_queue import PendingItem, RetryQueue

    async def replay_message(
        chat_id: int, thread_id: int | None, text: str, queued_at: str, hot: bool
    ) -> None:
        reply = await agent.retry_message(chat_id, thread_id, text, queued_at, hot=hot)
        if reply is None:
            return  # superseded — correctly silent
        _require_completed(reply)
        try:
            await bot.send_message(reply or "(no reply)", thread_id, chat_id=chat_id)
        except Exception:
            # The run itself succeeded (vault writes happened); a Telegram
            # delivery hiccup — likely when several items drain back-to-back —
            # must not classify the item as poison and tell the user their
            # message failed.
            logging.getLogger(__name__).warning(
                "Could not deliver replayed reply for chat_id=%d", chat_id, exc_info=True
            )

    async def notify_drop(item: PendingItem, exc: Exception) -> None:
        if item.kind == "message" and item.chat_id is not None:
            await bot.send_message(
                f"Sorry — I couldn't process your message from {item.queued_at} "
                f"even after Copilot came back: {exc}",
                item.thread_id,
                chat_id=item.chat_id,
            )
        else:
            await bot.notify_lifecycle(
                f"A reminder queued during a Copilot outage failed and was "
                f"dropped: {item.text[:200]}"
            )

    async def run_job(prompt: str) -> None:
        _require_completed(await agent.run_job(prompt))

    retry_queue = RetryQueue(
        cfg.state_dir,
        replay_message_fn=replay_message,
        replay_job_fn=run_job,
        notify_drop_fn=notify_drop,
        tz_name=cfg.timezone,
    )

    # Init Telegram bot (we need send_message before building agent)
    bot = TelegramBot(
        token=cfg.telegram_bot_token,
        allowed_user_ids=cfg.allowed_user_ids,
        agent=None,  # type: ignore[arg-type]  — set below
        transcriber=transcriber,
        save_attachment_fn=vault.save_attachment,
        models=model_options,
        default_model=default_alias,
        set_model_fn=copilot.get_client().set_model,
        refresh_models_fn=refresh_models,
        state_dir=cfg.state_dir,
        default_chat_id=cfg.default_chat_id,
        queue_message_fn=retry_queue.enqueue_message,
    )

    # Web research is optional — enabled when a 4get URL is configured
    researcher = None
    if cfg.fourget_url.strip():
        from .web import Researcher, WebTools

        researcher = Researcher(WebTools(cfg.fourget_url))
    else:
        logging.getLogger(__name__).info(
            "web.fourget_url not set — web research disabled"
        )

    # Attachment content extraction (local parse, vision fallback via Copilot)
    from .extract import AttachmentExtractor

    extractor = AttachmentExtractor(vault)

    # Skills: stored procedures, shipped in the package and authored in the vault
    from .skills import SkillLibrary

    skills = SkillLibrary(cfg.vault_path)

    # Fan-out: bulk parallel processing via read-only worker sub-agents
    from .fanout import FanOut

    fan_out = FanOut(
        vault_tools=vault,
        skills=skills,
        research_fn=researcher.research if researcher else None,
    )

    # The built-ins ride the same scheduler as the table's rows; their
    # last-success bookkeeping lives in the state dir.
    scheduler = Scheduler(
        vault_tools=vault,
        run_job_fn=run_job,
        tz_name=cfg.timezone,
        queue_job_fn=retry_queue.enqueue_job,
        builtins=maintenance_jobs,
        maintenance_state=MaintenanceState(cfg.state_dir / STATE_FILENAME),
    )

    # Init agent
    agent = Agent(
        vault_tools=vault,
        schedule_dispatcher=scheduler.dispatch,
        schedule_schemas=scheduler.tool_schemas(),
        send_message_fn=bot.send_message,
        create_forum_topic_fn=bot.create_forum_topic,
        research_fn=researcher.research if researcher else None,
        extract_fn=extractor.extract,
        fan_out_fn=fan_out.run,
        skills=skills,
        backup=backup,
        history_size=cfg.history_size,
        tz_name=cfg.timezone,
    )
    bot._agent = agent  # wire back

    usage_task = asyncio.create_task(tracker.run())
    lifecycle = Lifecycle()
    lifecycle.install()
    background: list[asyncio.Task] = []
    try:
        await bot.start(abort=lifecycle.stop)
        if not lifecycle.stop.is_set():
            # Even overdue date jobs must wait for Telegram readiness, not
            # just recurring catch-up: a failed delivery consumes a one-off.
            scheduler.start()
            scheduler.reload()
            scheduler.catch_up()
            background.append(asyncio.create_task(_poll_schedule(scheduler)))
            background.append(asyncio.create_task(retry_queue.run()))

            from .inbox import ingest as ingest_inbox

            # The checkpoint records consumed captures without modifying the
            # externally edited inbox. Cancelled runs leave it unchanged.
            background.append(asyncio.create_task(ingest_inbox(
                cfg.vault_path, agent.run_job, backup=backup, state_dir=cfg.state_dir
            )))
        await lifecycle.wait()
    finally:
        try:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await graceful_shutdown(bot=bot, scheduler=scheduler, force=lifecycle.force)
        finally:
            try:
                await _drain_final(
                    [("usage telemetry", tracker, usage_task)]
                    + ([("vault backup", backup, backup_task)] if backup is not None else []),
                    lifecycle.force,
                )
            finally:
                # The second signal must retain its controlled force path
                # throughout the drain, rather than reverting to SIG_DFL.
                lifecycle.remove()


async def _drain_final(
    components: list[tuple[str, Any, asyncio.Task | None]], force: asyncio.Event
) -> None:
    """Flush independent final state without ignoring a second shutdown signal."""
    workers = [task for _, _, task in components if task is not None]
    for task in workers:
        task.cancel()

    async def finish(component: Any, worker: asyncio.Task | None) -> None:
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await component.drain()

    drains = [asyncio.create_task(finish(component, task)) for _, component, task in components]
    flushed = asyncio.gather(*drains, return_exceptions=True)
    forced = asyncio.create_task(force.wait())
    try:
        await asyncio.wait(
            {flushed, forced}, timeout=_FINAL_DRAIN_BUDGET,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        logger = logging.getLogger(__name__)
        for (name, _, _), task in zip(components, drains, strict=True):
            if not task.done():
                logger.warning(
                    "Dropping unfinished final %s flush (%s); pending data may not be persisted",
                    name, "forced shutdown" if force.is_set() else "deadline or cancellation",
                )
                task.cancel()
        forced.cancel()
        await asyncio.gather(flushed, forced, *workers, return_exceptions=True)
        for (name, _, _), task in zip(components, drains, strict=True):
            if not task.cancelled() and (exc := task.exception()) is not None:
                logger.error("Final %s flush failed; pending data may not be persisted", name, exc_info=exc)


def _require_completed(reply: str) -> None:
    from .agent import MAX_ITERATIONS_REPLY

    if reply == MAX_ITERATIONS_REPLY:
        raise RuntimeError("Agent reached its iteration limit before completing the run")


async def _poll_schedule(scheduler: Any) -> None:
    """Reload schedule.md every 60 seconds."""
    while True:
        await asyncio.sleep(60)
        try:
            scheduler.reload()
        except Exception:
            logging.getLogger(__name__).exception("Error reloading schedule")


async def _auth(config_path: Path | None) -> None:
    from . import copilot
    from .config import load_config

    cfg = load_config(config_path)
    _setup_logging()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    await copilot.run_device_flow(cfg.state_dir)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="assistant")
    parser.add_argument("command", choices=["run", "auth"], help="run: start the bot; auth: device flow")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.toml")
    args = parser.parse_args()

    from .config import ConfigError

    try:
        if args.command == "run":
            asyncio.run(_run(args.config))
        elif args.command == "auth":
            asyncio.run(_auth(args.config))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
