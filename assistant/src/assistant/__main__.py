"""Entrypoints: `assistant run` and `assistant auth`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any


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

    async def replay_job(prompt: str) -> None:
        await agent.run_job(prompt)

    retry_queue = RetryQueue(
        cfg.state_dir,
        replay_message_fn=replay_message,
        replay_job_fn=replay_job,
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

    # Init scheduler (needs agent for job firing; closure resolves the
    # agent name late — it is constructed below)
    async def run_job(prompt: str) -> None:
        await agent.run_job(prompt)

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

    # Start scheduler
    scheduler.start()
    scheduler.reload()

    # Start watchfiles poll for schedule.md
    poll_task = asyncio.create_task(_poll_schedule(scheduler))

    # Start usage flush loop
    usage_task = asyncio.create_task(tracker.run())

    # SIGTERM/SIGINT start a drain that lets in-flight work finish; a second
    # signal abandons it. See lifecycle.py for why the ordering matters.
    lifecycle = Lifecycle()
    lifecycle.install()
    # The stop event doubles as the abort switch for startup's network
    # retries, so a SIGTERM during an outage still shuts down promptly.
    await bot.start(abort=lifecycle.stop)

    # Drain the outage retry queue (items reloaded from a previous run, plus
    # anything queued live). After bot.start() so replayed replies can deliver.
    retry_task = asyncio.create_task(retry_queue.run())

    # Fire recurring jobs missed while the service was down. After bot.start()
    # so the late runs can deliver; reload() above already backfilled `next`
    # baselines for rows that had none. Skipped when a signal already aborted
    # startup — the runs would only stall the shutdown drain and, failing,
    # burn nothing but time; `next` stays past, so the next boot catches up.
    if not lifecycle.stop.is_set():
        scheduler.catch_up()

    # Ingest offline captures from inbox.md. Ordering matters: the backup's
    # startup sweep (init_repo above) has already committed the pre-boot file,
    # the scheduler is up so entries can create reminders, and the bot is up
    # so the summary can be delivered. Cancelled mid-run, the file is left
    # untouched and the next startup retries.
    from .inbox import ingest as ingest_inbox

    inbox_task = asyncio.create_task(
        ingest_inbox(cfg.vault_path, agent.run_job, backup=backup)
    )

    try:
        await lifecycle.wait()
    finally:
        lifecycle.remove()
        inbox_task.cancel()
        # Stop replaying queued work mid-shutdown; unfinished items are on
        # disk and the next startup reprocesses them.
        retry_task.cancel()
        # Stop reloading schedule.md so it cannot register jobs mid-shutdown
        poll_task.cancel()
        await graceful_shutdown(bot=bot, scheduler=scheduler, force=lifecycle.force)
        usage_task.cancel()
        await tracker.drain()
        # After the drain: every finished run has scheduled its commit by now.
        if backup is not None:
            if backup_task is not None:
                backup_task.cancel()
            await backup.drain()


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
