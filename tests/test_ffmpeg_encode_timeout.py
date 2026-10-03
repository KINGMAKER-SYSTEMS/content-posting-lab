"""Behavioral bounds for the shared color-correction ffmpeg process."""

import asyncio
import signal

import pytest

from services import ffmpeg


class FakeStderr:
    def __init__(self, chunks=()):
        self._chunks = iter(chunks)
        self.read_sizes = []

    async def read(self, size=-1):
        self.read_sizes.append(size)
        return next(self._chunks, b"")


class FakeProc:
    def __init__(self, *, pid=4242, returncode=0, stderr_chunks=()):
        self.pid = pid
        self.returncode = returncode
        self.stderr = FakeStderr(stderr_chunks)
        self.waited = False
        self.communicated = False

    async def communicate(self):
        self.communicated = True
        return b"", b"".join(self.stderr._chunks)

    async def wait(self):
        self.waited = True
        return self.returncode


class HungProc(FakeProc):
    def __init__(self, pid=4242):
        super().__init__(pid=pid, returncode=None)

    async def communicate(self):
        self.communicated = True
        await asyncio.Event().wait()

    async def wait(self):
        self.waited = True
        if self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode


def _stub_probe(monkeypatch, duration_seconds):
    # Intercept the standard async offload boundary, not a newly added private
    # probe symbol. b8033bd can therefore execute these public-function tests;
    # it fails their timeout behavior rather than their imports/setup.
    # Keyed on the probed function: run_color_correct offloads BOTH its probes
    # through asyncio.to_thread (colour metadata + duration), and only the
    # duration result drives the encode timeout under test. The colour probe
    # gets a plain SDR stub so no HDR tags enter the asserted argv.
    real_to_thread = asyncio.to_thread

    async def fake_to_thread(function, *args, **kwargs):
        if function is ffmpeg._probe_input_duration_seconds:
            return duration_seconds
        if function is ffmpeg._probe_input_color:
            return {"color_space": "bt709", "color_transfer": "bt709",
                    "color_primaries": "bt709", "color_range": "tv"}
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(ffmpeg.asyncio, "to_thread", fake_to_thread)

def _short_timeout_constants(monkeypatch):
    for name, value in {
        "_ENCODE_TIMEOUT_FLOOR_SECONDS": 0.02,
        "_ENCODE_TIMEOUT_GRACE_SECONDS": 0.0,
        "_ENCODE_TIMEOUT_MULTIPLIER": 1.0,
        "_ENCODE_TIMEOUT_CEILING_SECONDS": 0.02,
    }.items():
        monkeypatch.setattr(ffmpeg, name, value, raising=False)


@pytest.mark.asyncio
async def test_long_unwindowed_encode_uses_probed_input_duration_budget(monkeypatch, tmp_path):
    proc = FakeProc()
    timeouts = []
    kill_calls = []

    async def fake_exec(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return proc

    async def budget_gate(awaitable, timeout):
        timeouts.append(timeout)
        if timeout < 700:
            awaitable.close()
            raise asyncio.TimeoutError
        return await awaitable

    _stub_probe(monkeypatch, 200.0)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg.asyncio, "wait_for", budget_gate)
    monkeypatch.setattr(ffmpeg.os, "killpg", lambda pgid, sig: kill_calls.append((pgid, sig)))

    await ffmpeg.run_color_correct(str(tmp_path / "long-input.mp4"), str(tmp_path / "out.mp4"), None)

    assert timeouts == [pytest.approx(920.0)]
    assert kill_calls == []


@pytest.mark.asyncio
async def test_windowed_encode_budget_uses_input_window_duration(monkeypatch, tmp_path):
    proc = FakeProc()
    timeouts = []

    async def fake_exec(*args, **kwargs):
        return proc

    async def capture_timeout(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    _stub_probe(monkeypatch, pytest.fail)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg.asyncio, "wait_for", capture_timeout)

    await ffmpeg.run_color_correct(
        str(tmp_path / "in.mp4"),
        str(tmp_path / "out.mp4"),
        None,
        clip_start_ms=1_000,
        clip_duration_ms=300_000,
    )

    assert timeouts == [pytest.approx(1_320.0)]


@pytest.mark.asyncio
async def test_unknown_input_duration_uses_timeout_ceiling(monkeypatch, tmp_path):
    proc = FakeProc()
    timeouts = []

    async def fake_exec(*args, **kwargs):
        return proc

    async def capture_timeout(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    _stub_probe(monkeypatch, None)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg.asyncio, "wait_for", capture_timeout)

    await ffmpeg.run_color_correct(str(tmp_path / "unknown.mp4"), str(tmp_path / "out.mp4"), None)

    assert timeouts == [pytest.approx(10_800.0)]


@pytest.mark.asyncio
async def test_hung_ffmpeg_is_killed_reaped_and_partial_output_removed(monkeypatch, tmp_path):
    proc = HungProc()
    kill_calls = []
    spawn_options = []

    async def fake_exec(*args, **kwargs):
        spawn_options.append(kwargs)
        return proc

    def kill_group(pgid, sig):
        kill_calls.append((pgid, sig))
        proc.returncode = -sig

    _stub_probe(monkeypatch, None)
    _short_timeout_constants(monkeypatch)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg.os, "killpg", kill_group)

    out = tmp_path / "out.mp4"
    out.write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="ffmpeg_encode_timeout"):
        await asyncio.wait_for(
            ffmpeg.run_color_correct(str(tmp_path / "in.mp4"), str(out), None),
            timeout=1,
        )

    assert spawn_options == [{
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": True,
    }]
    assert kill_calls == [(proc.pid, signal.SIGKILL)]
    assert proc.waited is True
    assert not out.exists()
    assert ffmpeg._COLOR_CORRECT_GATE.locked() is False


@pytest.mark.asyncio
async def test_ffmpeg_wait_that_never_finishes_cannot_stall_timeout_cleanup(monkeypatch, tmp_path):
    class NeverReapedProc(FakeProc):
        def __init__(self):
            super().__init__(returncode=None)
            self.wait_started = asyncio.Event()

        async def wait(self):
            self.wait_started.set()
            await asyncio.Event().wait()

    proc = NeverReapedProc()
    kill_calls = []

    async def fake_exec(*args, **kwargs):
        return proc

    def kill_group(pgid, sig):
        kill_calls.append((pgid, sig))

    _stub_probe(monkeypatch, None)
    _short_timeout_constants(monkeypatch)
    monkeypatch.setattr(ffmpeg, "_ENCODE_REAP_TIMEOUT_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg.os, "killpg", kill_group)

    out = tmp_path / "never-reaped.mp4"
    out.write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="ffmpeg_encode_timeout"):
        await asyncio.wait_for(
            ffmpeg.run_color_correct(str(tmp_path / "in.mp4"), str(out), None),
            timeout=0.5,
        )

    assert proc.wait_started.is_set()
    assert kill_calls == [(proc.pid, signal.SIGKILL)]
    assert not out.exists()
    assert ffmpeg._COLOR_CORRECT_GATE.locked() is False


@pytest.mark.asyncio
async def test_ffmpeg_stderr_is_drained_into_a_bounded_tail(monkeypatch, tmp_path):
    marker = b"tail-marker"
    proc = FakeProc(returncode=1, stderr_chunks=[b"A" * 65_536] * 4 + [marker])

    async def fake_exec(*args, **kwargs):
        return proc

    _stub_probe(monkeypatch, 1.0)
    monkeypatch.setattr(ffmpeg, "_ENCODE_STDERR_TAIL_BYTES", 32, raising=False)
    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError) as caught:
        await ffmpeg.run_color_correct(str(tmp_path / "in.mp4"), str(tmp_path / "out.mp4"), None)

    assert proc.communicated is False
    assert marker.decode() in str(caught.value)
    assert "A" * 33 not in str(caught.value)
    assert proc.stderr.read_sizes and set(proc.stderr.read_sizes) == {8_192}
