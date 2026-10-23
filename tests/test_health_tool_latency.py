"""Health probes must never wait on tool subprocesses or monopolize the event loop."""
import asyncio
import threading
import time

import pytest

import app as app_module


@pytest.mark.asyncio
async def test_health_tool_refresh_is_single_flight_and_nonblocking(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_tool(name, result):
        calls.append(name)
        entered.set()
        assert release.wait(2), "test refresh was not released"
        return result

    monkeypatch.setattr(app_module, "_check_ffmpeg", lambda: slow_tool("ffmpeg", True))
    monkeypatch.setattr(app_module, "_check_ytdlp", lambda: slow_tool("ytdlp", False))
    monkeypatch.setattr(app_module, "generation_budget_status", lambda: {"corrupt": False})
    monkeypatch.setattr(app_module, "_tool_status", (True, True))
    monkeypatch.setattr(app_module, "_tool_checked_at", 0.0)
    monkeypatch.setattr(app_module, "_tool_refresh_task", None)

    try:
        start = time.monotonic()
        results = await asyncio.wait_for(
            asyncio.gather(*(app_module.health_check() for _ in range(20))),
            timeout=0.15,
        )
        assert time.monotonic() - start < 0.15
        assert all(result["status"] == "ok" for result in results)
        assert all(result["ffmpeg"] and result["ytdlp"] for result in results)
        assert await asyncio.to_thread(entered.wait, 0.5)
        assert app_module._tool_refresh_task is not None
        release.set()
        await asyncio.wait_for(app_module._tool_refresh_task, timeout=1)
        assert sorted(calls) == ["ffmpeg", "ytdlp"]
        refreshed = await app_module.health_check()
        assert refreshed["status"] == "degraded"
        assert refreshed["ffmpeg"] is True
        assert refreshed["ytdlp"] is False
        assert sorted(calls) == ["ffmpeg", "ytdlp"]
    finally:
        release.set()
        if app_module._tool_refresh_task is not None:
            await asyncio.wait_for(app_module._tool_refresh_task, timeout=2)


@pytest.mark.asyncio
async def test_startup_tool_check_runs_concurrently_and_records_missing(monkeypatch):
    # The guarantee is that the two tool checks overlap (run concurrently),
    # not that they finish within some wall-clock budget — a loaded host
    # pushes two 0.1s thread-pool sleeps past any tight deadline. Peak
    # overlap measures exactly what matters: both checkers in-flight at once.
    active = 0
    peak = 0
    lock = threading.Lock()

    def tracked(result):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return result

    monkeypatch.setattr(app_module, "_check_ffmpeg", lambda: tracked(False))
    monkeypatch.setattr(app_module, "_check_ytdlp", lambda: tracked(True))
    start = time.monotonic()
    await app_module._refresh_tool_status()
    assert app_module._tool_status == (False, True)
    assert app_module._tool_checked_at >= start
    assert peak == 2, "tool checks must run concurrently (peak overlap)"


@pytest.mark.asyncio
async def test_permission_error_degrades_and_refresh_is_rate_limited(monkeypatch):
    calls = []

    def denied(*args, **kwargs):
        calls.append(args[0])
        raise PermissionError("executable denied")

    monkeypatch.setattr(app_module.subprocess, "run", denied)
    monkeypatch.setattr(app_module, "generation_budget_status", lambda: {"corrupt": False})
    monkeypatch.setattr(app_module, "_tool_status", (True, True))
    monkeypatch.setattr(app_module, "_tool_checked_at", 0.0)
    monkeypatch.setattr(app_module, "_tool_refresh_task", None)

    first = await app_module.health_check()
    assert first["status"] == "ok"  # Last completed status until refresh settles.
    await asyncio.wait_for(app_module._tool_refresh_task, timeout=1)
    assert app_module._tool_status == (False, False)
    assert len(calls) == 2
    for _ in range(20):
        response = await app_module.health_check()
        assert response["status"] == "degraded"
    assert len(calls) == 2
    assert app_module._tool_checked_at > 0


@pytest.mark.asyncio
async def test_unexpected_checker_failure_degrades_without_probe_loop(monkeypatch):
    calls = []

    def broken():
        calls.append("ffmpeg")
        raise RuntimeError("checker failed")

    monkeypatch.setattr(app_module, "_check_ffmpeg", broken)
    monkeypatch.setattr(app_module, "_check_ytdlp", lambda: True)
    monkeypatch.setattr(app_module, "_tool_status", (True, True))
    monkeypatch.setattr(app_module, "_tool_checked_at", 0.0)
    monkeypatch.setattr(app_module, "_tool_refresh_task", None)
    monkeypatch.setattr(app_module, "generation_budget_status", lambda: {"corrupt": False})

    await app_module.health_check()
    await asyncio.wait_for(app_module._tool_refresh_task, timeout=1)
    assert app_module._tool_status == (False, True)
    for _ in range(10):
        assert (await app_module.health_check())["status"] == "degraded"
    assert calls == ["ffmpeg"]
