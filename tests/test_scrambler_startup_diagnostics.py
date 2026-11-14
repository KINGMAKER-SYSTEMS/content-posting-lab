"""Startup observations never hold readiness or leave a slow reader behind."""
import asyncio
import logging
import sys
import threading

import pytest

from services import scrambler_preflight as preflight


@pytest.mark.asyncio
async def test_diagnostic_deadline_reaps_real_owned_reader(monkeypatch, caplog):
    from services import owned_import_process as owned

    create = owned.create_import_subprocess
    processes = []

    async def slow_reader(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(owned, "create_import_subprocess", slow_reader)
    monkeypatch.setattr(preflight, "STARTUP_DIAGNOSTIC_TIMEOUT_S", 0.12)
    with caplog.at_level(logging.INFO, logger="scrambler.preflight"):
        await asyncio.wait_for(preflight.run_startup_preflight(), 3)
    assert len(processes) == 1
    assert processes[0]._process.returncode is not None
    assert "readiness unknown (diagnostic deadline exceeded)" in caplog.text
    assert "preflight: ready" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_cancellation_joins_owned_reader(monkeypatch):
    from services import owned_import_process as owned

    create = owned.create_import_subprocess
    started = asyncio.Event()
    processes = []

    async def slow_reader(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(owned, "create_import_subprocess", slow_reader)
    task = asyncio.create_task(preflight.run_startup_preflight())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert processes[0]._process.returncode is not None


@pytest.mark.asyncio
async def test_child_off_observation_is_forwarded_without_activating_pilot(monkeypatch, caplog):
    monkeypatch.delenv("SCRAMBLER_ENABLED", raising=False)
    monkeypatch.delenv("SCRAMBLER_PAGES", raising=False)
    with caplog.at_level(logging.INFO, logger="scrambler.preflight"):
        await asyncio.wait_for(preflight.run_startup_preflight(), 3)
    assert "scrambler preflight: OFF" in caplog.text


@pytest.mark.asyncio
async def test_not_ready_observation_retains_warning_and_child_failure_is_unknown(monkeypatch, caplog):
    from services import owned_import_process as owned

    create = owned.create_import_subprocess

    async def refused(*args, **kwargs):
        return await create(sys.executable, "-c",
                            "print('scrambler preflight: NOT READY page=acct:fixture')",
                            **kwargs)

    monkeypatch.setattr(owned, "create_import_subprocess", refused)
    with caplog.at_level(logging.INFO, logger="scrambler.preflight"):
        await preflight.run_startup_preflight()
    assert any(record.levelno == logging.WARNING and "NOT READY" in record.message
               for record in caplog.records)

    async def failed(*args, **kwargs):
        return await create(sys.executable, "-c", "raise SystemExit(3)", **kwargs)

    caplog.clear()
    monkeypatch.setattr(owned, "create_import_subprocess", failed)
    with caplog.at_level(logging.INFO, logger="scrambler.preflight"):
        await preflight.run_startup_preflight()
    assert "diagnostic process failed (exit=3)" in caplog.text
    assert "preflight: ready" not in caplog.text


@pytest.fixture
def isolated_lifespan(monkeypatch):
    import app as app_module
    from services import source_runtime_reconciliation as reconcile
    import services.telegram as telegram
    import telegram_bot
    import routers.control_plane as cp
    import scraper.frame_extractor as extractor
    import services.abn_assets as assets
    import services.abn_factory as factory
    import scripts.migrate_abn_assets as migration
    import services.scrambler_feed_pull as feed

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(reconcile, "reconcile_railway_startup", lambda *args: {"status": "fixture"})
    monkeypatch.setattr(extractor, "sweep_private_cookie_jars", lambda: None)
    monkeypatch.setattr(assets, "assert_migration_complete", lambda: None)
    monkeypatch.setattr(migration, "verify_voice_symlinks", lambda: [])
    monkeypatch.setattr(app_module.agenticnews_db, "init_db", nothing)
    monkeypatch.setattr(factory, "start_factory", nothing)
    monkeypatch.setattr(app_module, "_refresh_tool_status", nothing)
    monkeypatch.setattr(app_module, "_log_startup_validation", lambda *args: None)
    monkeypatch.setattr(telegram, "get_bot_token", lambda: None)
    for name in ("start_sounds_bot", "stop_sounds_bot", "stop_bot"):
        monkeypatch.setattr(telegram_bot, name, nothing)
    monkeypatch.setattr(cp, "start_compaction_scheduler", lambda: None)
    monkeypatch.setattr(cp, "stop_compaction_scheduler", lambda: None)
    monkeypatch.setattr(cp, "shutdown_dossier_generation", nothing)
    monkeypatch.setattr(app_module, "start_post_render_workers", lambda: None)
    monkeypatch.setattr(feed, "start_feed_pull_scheduler", lambda: None)
    monkeypatch.setattr(feed, "stop_feed_pull_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "ensure_default_project", lambda: None)
    return app_module


def test_actual_app_startup_serves_while_diagnostics_are_blocked(monkeypatch, isolated_lifespan):
    from fastapi.testclient import TestClient

    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    async def blocked_diagnostics():
        started.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.005)
        finally:
            completed.set()

    def blocking_legacy_diagnostics():
        started.set()
        release.wait(2)

    # A finite emergency release makes a synchronous-await mutant terminate;
    # passing depends on startup finishing BEFORE that release.
    timer = threading.Timer(1.0, release.set)
    monkeypatch.setattr(preflight, "run_startup_preflight", blocked_diagnostics)
    monkeypatch.setattr(preflight, "log_startup_preflight", blocking_legacy_diagnostics)
    timer.start()
    try:
        with TestClient(isolated_lifespan.app) as client:
            assert started.wait(0.5)
            assert not release.is_set(), "startup waited for the diagnostic reader"
            assert client.get("/api/health").status_code == 200
        assert completed.is_set(), "shutdown did not join diagnostics"
    finally:
        release.set()
        timer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["exception", "cancelled-body", "normal"])
async def test_lifespan_exit_drains_before_preserving_original_failure(
    monkeypatch, isolated_lifespan, exit_kind,
):
    import routers.control_plane as cp

    started, leave, cleaning, release, completed = (asyncio.Event() for _ in range(5))
    teardown = []

    async def diagnostic():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            completed.set()

    async def shutdown():
        assert completed.is_set(), "existing shutdown ran before diagnostic cleanup"
        teardown.append("generation")

    async def owner():
        async with isolated_lifespan.lifespan(isolated_lifespan.app):
            await started.wait()
            await leave.wait()
            if exit_kind == "exception":
                raise RuntimeError("original-body-failure")

    monkeypatch.setattr(preflight, "run_startup_preflight", diagnostic)
    monkeypatch.setattr(cp, "shutdown_dossier_generation", shutdown)
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(started.wait(), 1)
        if exit_kind == "cancelled-body":
            task.cancel("original-owner-stop")
        else:
            leave.set()
        await asyncio.wait_for(cleaning.wait(), 1)
        for tag in ("first-shutdown-cancel", "second-shutdown-cancel"):
            task.cancel(tag)
            await asyncio.sleep(0)
        assert not task.done(), "lifespan released its diagnostic before cleanup completed"
        release.set()
        if exit_kind == "exception":
            with pytest.raises(RuntimeError, match="original-body-failure"):
                await asyncio.wait_for(task, 1)
        else:
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(task, 1)
            assert error.value.args == (("original-owner-stop" if exit_kind == "cancelled-body"
                                         else "first-shutdown-cancel"),)
        assert completed.is_set()
        assert teardown == ["generation"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["exception", "cancelled-body"])
async def test_exceptional_lifespan_reaps_real_diagnostic_process(
    monkeypatch, isolated_lifespan, exit_kind,
):
    from services import owned_import_process as owned

    create = owned.create_import_subprocess
    started, leave = asyncio.Event(), asyncio.Event()
    processes = []

    async def reader(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        started.set()
        return process

    async def owner():
        async with isolated_lifespan.lifespan(isolated_lifespan.app):
            await leave.wait()
            raise RuntimeError("original-body-failure")

    monkeypatch.setattr(owned, "create_import_subprocess", reader)
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(started.wait(), 1)
        if exit_kind == "cancelled-body":
            task.cancel("original-owner-stop")
            with pytest.raises(asyncio.CancelledError) as error:
                await asyncio.wait_for(task, 2)
            assert error.value.args == ("original-owner-stop",)
        else:
            leave.set()
            with pytest.raises(RuntimeError, match="original-body-failure"):
                await asyncio.wait_for(task, 2)
        assert processes[0]._process.returncode is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            await process.terminate()
