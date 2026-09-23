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
    # httpx logs full request URLs at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run(config_path: Path | None) -> None:
    from . import copilot
    from .agent import Agent
    from .config import load_config
    from .lifecycle import Lifecycle, graceful_shutdown
    from .schedule import Scheduler
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
    # catalog: it feeds the model picker and, when default_family is set,
    # promotes the newest model of that family to default. Best-effort — a
    # failed fetch leaves the configured models in charge.
    from .models import ModelOption, ModelPicker, merge_options, resolve_startup

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

    models = ModelPicker(model_options, default_alias,
                         set_model_fn=copilot.get_client().set_model, refresh_fn=refresh_models)

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
    from .conversations import ConversationArchive

    archive = ConversationArchive(cfg.state_dir)

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

    # Outage retry queue: one-off jobs whose run failed because Copilot was
    # unreachable are persisted and replayed once it answers again. The
    # closures resolve `agent` and `companion` late — both are constructed
    # below.
    from .retry_queue import PendingItem, RetryQueue

    async def notify_drop(item: PendingItem, exc: Exception) -> None:
        companion.notify_lifecycle(
            f"A reminder queued during a Copilot outage failed and was dropped: {item.text[:200]}",
            important=True,
        )

    async def run_job(prompt: str) -> None:
        _require_completed(await agent.run_job(prompt))

    retry_queue = RetryQueue(
        cfg.state_dir,
        replay_job_fn=run_job,
        notify_drop_fn=notify_drop,
        tz_name=cfg.timezone,
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
    # A routine check-in ([routine: …] rows) pushes through the app, which is
    # built after the scheduler, hence the closure.
    async def remind(text: str, thread: str | None = None) -> str | None:
        return await companion.remind(text, thread)

    scheduler = Scheduler(
        vault_tools=vault,
        run_job_fn=run_job,
        tz_name=cfg.timezone,
        queue_job_fn=retry_queue.enqueue_job,
        builtins=maintenance_jobs,
        maintenance_state=MaintenanceState(cfg.state_dir / STATE_FILENAME),
        remind_fn=remind,
    )

    # A proactive delivery (a scheduled run's reminder) is the web app's to
    # archive and push; the app is built after the agent, which needs the
    # sender at construction, hence the closure.
    async def send_message(text: str) -> None:
        await companion.deliver(text)

    # Init agent
    agent = Agent(
        vault_tools=vault,
        schedule_dispatcher=scheduler.dispatch,
        schedule_schemas=scheduler.tool_schemas(),
        send_message_fn=send_message,
        research_fn=researcher.research if researcher else None,
        extract_fn=extractor.extract,
        fan_out_fn=fan_out.run,
        skills=skills,
        backup=backup,
        history_exchanges=cfg.history_exchanges,
        tz_name=cfg.timezone,
        archive=archive,
        agent_name=cfg.agent_name,
    )
    from .companion import Companion

    companion = Companion(cfg, agent, vault, archive=archive, transcriber=transcriber, models=models)

    usage_task = asyncio.create_task(tracker.run())
    lifecycle = Lifecycle()
    lifecycle.install()
    background: list[asyncio.Task] = []
    try:
        await companion.start()
        companion.notify_lifecycle(companion.startup_message())
        # Even overdue date jobs wait for the app to be up: a failed
        # delivery consumes a one-off.
        scheduler.start()
        scheduler.reload()
        scheduler.catch_up()
        background.append(asyncio.create_task(_poll_schedule(scheduler)))
        background.append(asyncio.create_task(retry_queue.run()))
        # Web messages that failed on an outage replay from their archived
        # rows; reminders still unseen after a while are pushed once more.
        background.append(asyncio.create_task(companion.replay_outages()))
        background.append(asyncio.create_task(companion.nudge_loop()))

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
            await graceful_shutdown(companion=companion, scheduler=scheduler, force=lifecycle.force)
        finally:
            try:
                await companion.close()
                await _drain_final(
                    [("usage telemetry", tracker, usage_task)]
                    + ([("vault backup", backup, backup_task)] if backup is not None else []),
                    lifecycle.force,
                )
            finally:
                # The second signal must retain its controlled force path
                # throughout the drain, rather than reverting to SIG_DFL.
                lifecycle.remove()
                archive.close()


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


def _require_completed(result: Any) -> None:
    """Reject a run abandoned at the cap with no closing summary (a capped run
    that summarised what remains counts as completed)."""
    from .agent import MAX_ITERATIONS_REPLY

    if result.reply == MAX_ITERATIONS_REPLY:
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
    parser.add_argument("command", choices=["run", "auth"], help="run: start the assistant; auth: device flow")
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
