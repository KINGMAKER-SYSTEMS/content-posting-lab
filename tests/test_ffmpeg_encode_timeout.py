"""A hung ffmpeg encode must not own the global treatment lane forever."""

import asyncio
import signal

import pytest

from services import ffmpeg


class HungProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.waited = False

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self):
        self.waited = True
        self.returncode = -signal.SIGKILL
        return self.returncode


@pytest.mark.asyncio
async def test_hung_ffmpeg_is_killed_reaped_and_releases_the_lane(monkeypatch, tmp_path):
    proc = HungProc()
    kill_calls = []

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg, "_ENCODE_TIMEOUT_FLOOR_SECONDS", 0.05)
    monkeypatch.setattr(ffmpeg.os, "killpg", lambda pgid, sig: kill_calls.append((pgid, sig)))

    out = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="ffmpeg_encode_timeout"):
        await asyncio.wait_for(
            ffmpeg.run_color_correct(str(tmp_path / "in.mp4"), str(out), None),
            timeout=5,
        )

    assert kill_calls == [(proc.pid, signal.SIGKILL)]
    assert proc.waited is True
    # The semaphore permit must be released so a later render can start.
    assert ffmpeg._COLOR_CORRECT_GATE.locked() is False
