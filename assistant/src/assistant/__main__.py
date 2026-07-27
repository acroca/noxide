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

    # Init Copilot client
    copilot.init(cfg.state_dir, cfg.models[cfg.default_model])

    # Init usage tracking (JSONL store + vault view, flushed off the critical path)
    from . import usage

    tracker = usage.init(cfg.state_dir, cfg.vault_path, cfg.timezone)

    # Init vault tools
    vault = VaultTools(cfg.vault_path)

    # Voice transcription is optional — enabled when GITHUB_TOKEN is set
    transcriber = Transcriber(cfg.github_token) if cfg.github_token.strip() else None
    if transcriber is None:
        logging.getLogger(__name__).info(
            "GITHUB_TOKEN not set — voice messages disabled"
        )

    # Init Telegram bot (we need send_message before building agent)
    bot = TelegramBot(
        token=cfg.telegram_bot_token,
        allowed_user_ids=cfg.allowed_user_ids,
        agent=None,  # type: ignore[arg-type]  — set below
        transcriber=transcriber,
        save_attachment_fn=vault.save_attachment,
        models=cfg.models,
        default_model=cfg.default_model,
        set_model_fn=copilot.get_client().set_model,
        state_dir=cfg.state_dir,
        default_chat_id=cfg.default_chat_id,
    )

    # Web research is optional — enabled when a SearXNG URL is configured
    researcher = None
    if cfg.searxng_url.strip():
        from .web import Researcher, WebTools

        researcher = Researcher(WebTools(cfg.searxng_url))
    else:
        logging.getLogger(__name__).info(
            "web.searxng_url not set — web research disabled"
        )

    # Attachment content extraction (local parse, vision fallback via Copilot)
    from .extract import AttachmentExtractor

    extractor = AttachmentExtractor(vault)

    # Skills: stored procedures, shipped in the package and authored in the vault
    from .skills import SkillLibrary

    skills = SkillLibrary(cfg.vault_path)

    # Init scheduler (needs agent for job firing; closure resolves the
    # agent name late — it is constructed below)
    async def run_job(prompt: str) -> None:
        await agent.run_job(prompt)

    scheduler = Scheduler(vault_tools=vault, run_job_fn=run_job, tz_name=cfg.timezone)

    # Init agent
    agent = Agent(
        vault_tools=vault,
        schedule_dispatcher=scheduler.dispatch,
        schedule_schemas=scheduler.tool_schemas(),
        send_message_fn=bot.send_message,
        create_forum_topic_fn=bot.create_forum_topic,
        research_fn=researcher.research if researcher else None,
        extract_fn=extractor.extract,
        skills=skills,
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
    await bot.start()
    try:
        await lifecycle.wait()
    finally:
        lifecycle.remove()
        # Stop reloading schedule.md so it cannot register jobs mid-shutdown
        poll_task.cancel()
        await graceful_shutdown(bot=bot, scheduler=scheduler, force=lifecycle.force)
        usage_task.cancel()
        await tracker.drain()


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
