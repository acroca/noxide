"""Entrypoint wiring, with no network clients or process signal handlers."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from assistant import __main__ as main
from assistant import (
    agent,
    backup,
    companion,
    config,
    copilot,
    inbox,
    lifecycle,
    retry_queue,
    schedule,
    usage,
)
from assistant.agent import MAX_ITERATIONS_REPLY, JobResult
from assistant.maintenance import COMPILE_ID, COMPILE_PROMPT, STATE_FILENAME, MaintenanceState
from assistant.models import ModelPicker
from assistant.retry_queue import PendingItem
from assistant.schedule import Scheduler


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    cfg = config.Config.model_construct(
        vault_path=tmp_path / "vault",
        state_dir=tmp_path / "private-state",
        backup_enabled=True,
        models={"sonnet": "claude-sonnet-5"},
        default_model="sonnet",
        default_family="",
        model_vendors=[],
        timezone="UTC",
        agent_name="Noxide",
        history_exchanges=5,
        elevenlabs_api_key="",
        fourget_url="",
        maintenance_compile="0 3 * * *",
        maintenance_lint="0 4 * * SUN",
    )
    monkeypatch.setattr(config, "load_config", MagicMock(return_value=cfg))
    monkeypatch.setattr(config.Config, "validate_for_run", MagicMock())
    monkeypatch.setattr(main, "_setup_logging", lambda: None)
    client = MagicMock(list_models=AsyncMock(return_value=[]))
    monkeypatch.setattr(copilot, "init", MagicMock())
    monkeypatch.setattr(copilot, "get_client", lambda: client)

    events: list[str] = []
    workers: dict[str, asyncio.Task] = {}
    started = {name: asyncio.Event() for name in ("usage", "backup", "poll", "retry", "inbox")}
    lc = lifecycle.Lifecycle()
    installed = False

    def install() -> None:
        nonlocal installed
        installed = True
        events.append("install")

    def remove() -> None:
        nonlocal installed
        assert installed
        installed = False
        events.append("remove")

    async def worker(name: str) -> None:
        workers[name] = asyncio.current_task()
        events.append(f"{name}.run")
        started[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{name}.cancel")

    async def wait() -> None:
        if not lc.stop.is_set():
            await asyncio.gather(*(event.wait() for event in started.values()))
            lc._on_signal(signal.SIGTERM)
        await lc.stop.wait()

    monkeypatch.setattr(lc, "install", MagicMock(side_effect=install))
    monkeypatch.setattr(lc, "remove", MagicMock(side_effect=remove))
    monkeypatch.setattr(lc, "wait", AsyncMock(side_effect=wait))
    monkeypatch.setattr(lifecycle, "Lifecycle", lambda: lc)

    async def drain(name: str) -> None:
        assert installed, f"Signal handlers removed before {name} drain"
        events.append(f"{name}.drain")

    async def run_usage() -> None:
        await worker("usage")

    async def drain_usage() -> None:
        await drain("usage")

    tracker = MagicMock(
        run=AsyncMock(side_effect=run_usage),
        drain=AsyncMock(side_effect=drain_usage),
    )
    monkeypatch.setattr(usage, "init", MagicMock(return_value=tracker))

    async def run_backup() -> None:
        await worker("backup")

    async def drain_backup() -> None:
        await drain("backup")

    vault_backup = MagicMock(
        init_repo=AsyncMock(side_effect=lambda: events.append("backup.init")),
        run=AsyncMock(side_effect=run_backup),
        drain=AsyncMock(side_effect=drain_backup),
    )
    monkeypatch.setattr(backup, "VaultBackup", MagicMock(return_value=vault_backup))

    async def start() -> None:
        assert installed
        events.append("app.start")

    async def drain_app() -> None:
        await drain("app")

    app = MagicMock(
        start=AsyncMock(side_effect=start),
        pending=MagicMock(return_value=0),
        notify_lifecycle=MagicMock(return_value=None),
        startup_message=MagicMock(return_value="Started"),
        deliver=AsyncMock(),
        replay_outages=AsyncMock(),
        nudge_loop=AsyncMock(),
        drain=AsyncMock(side_effect=drain_app),
        close=AsyncMock(side_effect=lambda: events.append("app.close")),
    )
    app_factory = MagicMock(return_value=app)
    monkeypatch.setattr(companion, "Companion", app_factory)
    runner = MagicMock(run_job=AsyncMock(return_value=JobResult("completed")))
    monkeypatch.setattr(agent, "Agent", MagicMock(return_value=runner))

    async def drain_scheduler(*, timeout: float) -> int:
        await drain("scheduler")
        return 0

    scheduler = MagicMock(
        start=MagicMock(side_effect=lambda: events.append("scheduler.start")),
        reload=MagicMock(side_effect=lambda: events.append("scheduler.reload")),
        catch_up=MagicMock(side_effect=lambda: events.append("scheduler.catch_up")),
        drain=AsyncMock(side_effect=drain_scheduler),
        tool_schemas=MagicMock(return_value=[]),
    )
    scheduler_factory = MagicMock(return_value=scheduler)
    monkeypatch.setattr(schedule, "Scheduler", scheduler_factory)

    async def poll(sched: object) -> None:
        assert sched is scheduler
        await worker("poll")

    monkeypatch.setattr(main, "_poll_schedule", AsyncMock(side_effect=poll))

    async def run_retry() -> None:
        await worker("retry")

    queue = MagicMock(run=AsyncMock(side_effect=run_retry))
    queue_factory = MagicMock(return_value=queue)
    monkeypatch.setattr(retry_queue, "RetryQueue", queue_factory)

    async def ingest(*args: object, **kwargs: object) -> None:
        await worker("inbox")

    ingest_mock = AsyncMock(side_effect=ingest)
    monkeypatch.setattr(inbox, "ingest", ingest_mock)

    return SimpleNamespace(
        cfg=cfg, events=events, workers=workers, started=started, lc=lc, client=client,
        tracker=tracker, backup=vault_backup, app=app, app_factory=app_factory, agent=runner,
        scheduler=scheduler, scheduler_factory=scheduler_factory,
        queue=queue, queue_factory=queue_factory, ingest=ingest_mock,
    )


async def test_run_starts_the_app_before_jobs_and_tears_down_in_order(runtime) -> None:
    await asyncio.wait_for(main._run(None), timeout=2)

    events = runtime.events
    ordered = ["backup.init", "install", "app.start",
               "scheduler.start", "scheduler.reload", "scheduler.catch_up"]
    assert [events.index(name) for name in ordered] == sorted(events.index(name) for name in ordered)
    for name in ("poll", "retry", "inbox"):
        assert events.index("scheduler.catch_up") < events.index(f"{name}.run")
        assert events.index(f"{name}.cancel") < events.index("app.drain")
    assert events.index("app.drain") < events.index("app.close") < events.index("remove")
    assert all(task.cancelled() for task in runtime.workers.values())
    runtime.ingest.assert_awaited_once_with(
        runtime.cfg.vault_path, runtime.agent.run_job,
        backup=runtime.backup, state_dir=runtime.cfg.state_dir,
    )
    assert runtime.cfg.state_dir != runtime.cfg.vault_path
    assert events[-1] == "remove"
    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    runtime.app.notify_lifecycle.assert_any_call("Started")
    assert runtime.app.accepting is False


async def test_app_is_wired_with_the_shared_archive_and_the_model_picker(runtime, monkeypatch):
    from assistant import conversations

    class OpenArchive(conversations.ConversationArchive):
        """Keeps the database open after _run's teardown so the rows can be inspected."""

        def close(self):
            self.closed = True

    monkeypatch.setattr(conversations, "ConversationArchive", OpenArchive)
    await main._run(None)
    archive = agent.Agent.call_args.kwargs["archive"]
    try:
        assert isinstance(archive, OpenArchive) and archive.closed
        runtime.app_factory.assert_called_once_with(
            runtime.cfg, runtime.agent, ANY, archive=archive, transcriber=None, models=ANY)
        models = runtime.app_factory.call_args.kwargs["models"]
        assert isinstance(models, ModelPicker)
        assert models.choices()["current"] == "sonnet"
        models.select("sonnet")
        runtime.client.set_model.assert_called_once_with("claude-sonnet-5")
        runtime.app.start.assert_awaited_once()
        runtime.app.drain.assert_awaited_once()
        runtime.app.close.assert_awaited_once()
    finally:
        archive.db.close()


async def test_proactive_sends_are_the_apps_to_deliver(runtime) -> None:
    await asyncio.wait_for(main._run(None), timeout=2)
    sender = agent.Agent.call_args.kwargs["send_message_fn"]

    assert await sender("A reminder") is None

    runtime.app.deliver.assert_awaited_once_with("A reminder")


async def test_dropped_queued_job_pushes_an_important_notice(runtime) -> None:
    await asyncio.wait_for(main._run(None), timeout=2)
    notify_drop = runtime.queue_factory.call_args.kwargs["notify_drop_fn"]
    runtime.app.notify_lifecycle.reset_mock()

    await notify_drop(PendingItem(text="Remind me about the dentist", queued_at="earlier"), RuntimeError("boom"))

    text, kwargs = runtime.app.notify_lifecycle.call_args.args[0], runtime.app.notify_lifecycle.call_args.kwargs
    assert "dentist" in text and "dropped" in text and kwargs == {"important": True}


async def test_second_signal_can_force_the_actual_graceful_drain(runtime) -> None:
    final_tasks = []

    async def blocked_flush() -> None:
        final_tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    async def blocked_drain() -> None:
        runtime.lc.remove.assert_not_called()
        assert runtime.lc.stop.is_set()
        runtime.lc._on_signal(signal.SIGTERM)
        await asyncio.Event().wait()

    runtime.app.drain.side_effect = blocked_drain
    runtime.tracker.drain.side_effect = blocked_flush
    runtime.backup.drain.side_effect = blocked_flush
    await asyncio.wait_for(main._run(None), timeout=2)

    assert runtime.lc.force.is_set()
    runtime.app.close.assert_awaited_once()
    runtime.scheduler.drain.assert_awaited_once()
    runtime.lc.remove.assert_called_once()
    assert runtime.events[-1] == "remove"
    assert len(final_tasks) == 2
    assert all(task.cancelled() for task in final_tasks)


@pytest.mark.parametrize("component", ["tracker", "backup"])
async def test_second_signal_during_final_flush_cancels_and_joins(runtime, component, caplog) -> None:
    entered = asyncio.Event()
    cleanup_tasks = []
    cancelled = asyncio.Event()

    async def blocked_flush() -> None:
        cleanup_tasks.append(asyncio.current_task())
        runtime.lc.remove.assert_not_called()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Joining must include asynchronous cancellation cleanup.
            await asyncio.sleep(0)
            runtime.lc.remove.assert_not_called()
            cancelled.set()

    getattr(runtime, component).drain.side_effect = blocked_flush
    task = asyncio.create_task(main._run(None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert runtime.lc.stop.is_set()
        runtime.lc._on_signal(signal.SIGTERM)
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert cancelled.is_set()
    assert all(task.cancelled() for task in cleanup_tasks)
    assert all(task.cancelled() for task in runtime.workers.values())
    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    runtime.lc.remove.assert_called_once()
    name = "usage telemetry" if component == "tracker" else "vault backup"
    assert f"Dropping unfinished final {name} flush (forced shutdown)" in caplog.text


async def test_final_flush_deadline_cancels_and_joins_both_drains(runtime, monkeypatch, caplog) -> None:
    cleanup_tasks = []

    async def blocked_flush() -> None:
        cleanup_tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "_FINAL_DRAIN_BUDGET", 0.01)
    runtime.tracker.drain.side_effect = blocked_flush
    runtime.backup.drain.side_effect = blocked_flush
    await asyncio.wait_for(main._run(None), timeout=2)

    assert not runtime.lc.force.is_set()
    assert len(cleanup_tasks) == 2
    assert all(task.cancelled() for task in cleanup_tasks)
    assert all(task.cancelled() for task in runtime.workers.values())
    assert "final usage telemetry flush (deadline or cancellation)" in caplog.text
    assert "final vault backup flush (deadline or cancellation)" in caplog.text
    runtime.lc.remove.assert_called_once()


@pytest.mark.parametrize("component", ["tracker", "backup"])
async def test_final_flush_failure_does_not_skip_other_cleanup(runtime, component, caplog) -> None:
    getattr(runtime, component).drain.side_effect = RuntimeError("flush broke")
    await asyncio.wait_for(main._run(None), timeout=2)

    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    assert all(task.cancelled() for task in runtime.workers.values())
    runtime.lc.remove.assert_called_once()
    name = "usage telemetry" if component == "tracker" else "vault backup"
    assert f"Final {name} flush failed" in caplog.text
    assert "flush broke" in caplog.text


async def test_final_flush_failure_does_not_mask_startup_error(runtime, caplog) -> None:
    runtime.app.start.side_effect = RuntimeError("startup broke")
    runtime.tracker.drain.side_effect = RuntimeError("flush broke")

    with pytest.raises(RuntimeError, match="startup broke"):
        await asyncio.wait_for(main._run(None), timeout=2)

    runtime.backup.drain.assert_awaited_once()
    runtime.lc.remove.assert_called_once()
    assert "Final usage telemetry flush failed" in caplog.text


async def test_cancelling_final_flush_joins_cleanup_before_removing_handlers(runtime) -> None:
    entered = asyncio.Event()
    cleanup_tasks = []

    async def blocked_flush() -> None:
        cleanup_tasks.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            runtime.lc.remove.assert_not_called()

    runtime.tracker.drain.side_effect = blocked_flush
    task = asyncio.create_task(main._run(None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert all(task.cancelled() for task in cleanup_tasks)
    assert all(task.cancelled() for task in runtime.workers.values())
    runtime.backup.drain.assert_awaited_once()
    runtime.lc.remove.assert_called_once()


async def test_startup_failure_skips_background_and_cleans_up(runtime) -> None:
    error = RuntimeError("port in use")

    async def start() -> None:
        runtime.lc.remove.assert_not_called()
        await asyncio.gather(runtime.started["usage"].wait(), runtime.started["backup"].wait())
        raise error

    runtime.app.start.side_effect = start
    with pytest.raises(RuntimeError) as caught:
        await asyncio.wait_for(main._run(None), timeout=2)
    assert caught.value is error

    runtime.scheduler.start.assert_not_called()
    runtime.scheduler.reload.assert_not_called()
    runtime.scheduler.catch_up.assert_not_called()
    runtime.queue.run.assert_not_called()
    runtime.ingest.assert_not_called()
    assert not runtime.started["poll"].is_set()
    runtime.app.drain.assert_awaited_once()
    runtime.scheduler.drain.assert_awaited_once()
    runtime.app.close.assert_awaited_once()
    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    assert all(task.cancelled() for task in runtime.workers.values())
    assert runtime.events[-1] == "remove"


async def test_shutdown_failure_still_flushes_and_removes_handlers(runtime, monkeypatch) -> None:
    async def fail_shutdown(**kwargs: object) -> None:
        runtime.lc.remove.assert_not_called()
        assert kwargs["force"] is runtime.lc.force
        assert kwargs["companion"] is runtime.app
        raise RuntimeError("shutdown failed")

    monkeypatch.setattr(lifecycle, "graceful_shutdown", fail_shutdown)
    with pytest.raises(RuntimeError, match="shutdown failed"):
        await asyncio.wait_for(main._run(None), timeout=2)

    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    runtime.app.close.assert_awaited_once()
    assert all(task.cancelled() for task in runtime.workers.values())
    runtime.lc.remove.assert_called_once()


@pytest.mark.parametrize("step", ["start", "reload", "catch_up"])
async def test_scheduler_startup_failure_still_cleans_up(runtime, step) -> None:
    getattr(runtime.scheduler, step).side_effect = RuntimeError(f"{step} failed")

    with pytest.raises(RuntimeError, match=f"{step} failed"):
        await asyncio.wait_for(main._run(None), timeout=2)

    runtime.queue.run.assert_not_called()
    runtime.ingest.assert_not_called()
    assert not runtime.started["poll"].is_set()
    runtime.app.close.assert_awaited_once()
    runtime.scheduler.drain.assert_awaited_once()
    runtime.tracker.drain.assert_awaited_once()
    runtime.backup.drain.assert_awaited_once()
    assert all(task.cancelled() for task in runtime.workers.values())
    assert runtime.events[-1] == "remove"


@pytest.mark.parametrize("source", ["scheduler", "retry"])
@pytest.mark.parametrize("reply", [JobResult(MAX_ITERATIONS_REPLY, capped=True), JobResult("completed"),
                                   JobResult(""), JobResult("did half; the rest remains", capped=True)])
async def test_job_callbacks_reject_only_abandoned_outcomes(runtime, source, reply) -> None:
    """Only a run that ended on the raw sentinel (no closing summary) is rejected;
    a capped run that summarised what remains counts as completed."""
    await asyncio.wait_for(main._run(None), timeout=2)
    callback = (
        runtime.scheduler_factory.call_args.kwargs["run_job_fn"]
        if source == "scheduler"
        else runtime.queue_factory.call_args.kwargs["replay_job_fn"]
    )
    runtime.agent.run_job.return_value = reply

    if reply.reply == MAX_ITERATIONS_REPLY:
        with pytest.raises(RuntimeError, match="iteration limit"):
            await callback("check the vault")
    else:
        assert await callback("check the vault") is None
    runtime.agent.run_job.assert_awaited_once_with("check the vault")


@pytest.mark.parametrize("capped", [True, False])
async def test_real_scheduler_records_success_only_for_completed_callback(runtime, capped) -> None:
    await asyncio.wait_for(main._run(None), timeout=2)
    kwargs = runtime.scheduler_factory.call_args.kwargs
    scheduler = Scheduler(**kwargs)
    state = kwargs["maintenance_state"]
    baseline = datetime(2020, 1, 1, tzinfo=UTC)
    state.mark_success(COMPILE_ID, baseline)
    scheduler.schedule("0 8 * * *", "recurring check", recurring=True)
    entries, preserved = scheduler._read_table()
    entry = entries[0]
    entry.next = baseline.isoformat()
    scheduler._write_entries(entries, preserved)
    runtime.agent.run_job.return_value = JobResult(MAX_ITERATIONS_REPLY, capped=True) if capped else JobResult("completed")

    await scheduler._fire(COMPILE_ID, COMPILE_PROMPT, recurring=True)
    await scheduler._fire(entry.id, entry.prompt, recurring=True)

    persisted = MaintenanceState(runtime.cfg.state_dir / STATE_FILENAME)
    next_run = datetime.fromisoformat(scheduler._read_entries()[0].next)
    if capped:
        assert state.last_success(COMPILE_ID) == baseline
        assert persisted.last_success(COMPILE_ID) == baseline
        assert next_run == baseline
    else:
        assert state.last_success(COMPILE_ID) > baseline
        assert persisted.last_success(COMPILE_ID) == state.last_success(COMPILE_ID)
        assert next_run > baseline
    assert runtime.agent.run_job.await_count == 2


async def test_scheduler_gets_the_app_remind_fn(runtime) -> None:
    await asyncio.wait_for(main._run(None), timeout=2)
    remind = runtime.scheduler_factory.call_args.kwargs["remind_fn"]
    runtime.app.remind = AsyncMock(return_value="t1")
    assert await remind("Take the pills", None) == "t1"
    runtime.app.remind.assert_awaited_once_with("Take the pills", None)
