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
    def slow(result):
        time.sleep(0.1)
        return result

    monkeypatch.setattr(app_module, "_check_ffmpeg", lambda: slow(False))
    monkeypatch.setattr(app_module, "_check_ytdlp", lambda: slow(True))
    start = time.monotonic()
    await app_module._refresh_tool_status()
    elapsed = time.monotonic() - start
    assert elapsed < 0.19
    assert app_module._tool_status == (False, True)
    assert app_module._tool_checked_at >= start
