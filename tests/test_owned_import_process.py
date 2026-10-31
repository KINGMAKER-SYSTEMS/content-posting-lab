"""The owned leader anchors its group until one cleanup, even after exit."""
import asyncio
import os
import sys
import time

import pytest

from services import owned_import_process as owned


@pytest.mark.asyncio
async def test_success_drains_output_and_reaps_once(monkeypatch):
    process = await owned.create_import_subprocess(sys.executable, '-c', 'print("hello")')
    real_killpg = os.killpg
    signals = []
    def signal(group, sig):
        owned._check_waitable(group)  # Still our child at signal time, not reaped.
        signals.append(group)
        return real_killpg(group, sig)
    monkeypatch.setattr(owned.os, 'killpg', signal)
    output, error = await asyncio.wait_for(process.communicate(), 5)
    await process.terminate()
    assert output == b'hello\n' and error == b''
    assert signals == [process.pid]
    assert process.returncode == 0
    with pytest.raises(ChildProcessError):
        owned._check_waitable(process.pid)


@pytest.mark.asyncio
async def test_reaped_anchor_never_signals_numeric_group(monkeypatch):
    process = await owned.create_import_subprocess(sys.executable, '-c', 'pass')
    process._process.kill()
    process._process.wait()  # Model a foreign reaper/incorrect lifecycle.
    attempted = []
    monkeypatch.setattr(owned.os, 'killpg', lambda *args: attempted.append(args))
    with pytest.raises(RuntimeError, match='reaped outside'):
        await process.terminate()
    assert attempted == []
    for pipe in (process._process.stdout, process._process.stderr):
        pipe.close()


@pytest.mark.asyncio
async def test_exited_leader_with_descendant_holding_pipes_is_still_owned(tmp_path):
    ready, gate, output = (tmp_path / name for name in ('ready', 'gate', 'output'))
    child = ('import pathlib,sys,time; r,g,o=map(pathlib.Path,sys.argv[1:]); '
             'r.touch();\nwhile not g.exists(): time.sleep(.01)\no.write_text("late")')
    leader = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
              + repr(child) + ',*sys.argv[1:]])')
    process = await owned.create_import_subprocess(sys.executable, '-c', leader,
                                                   str(ready), str(gate), str(output))
    for _ in range(500):
        if ready.exists():
            break
        await asyncio.sleep(.01)
    assert ready.exists()
    # The leader exits before its descendant closes the inherited pipe.
    await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0
    gate.touch()
    await asyncio.sleep(.05)
    assert not output.exists()


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_preserves_first_tag(monkeypatch):
    process = await owned.create_import_subprocess(sys.executable, '-c', 'pass')
    real_cleanup = process._terminate_and_reap
    import threading
    entered, release = threading.Event(), threading.Event()
    def cleanup():
        entered.set()
        assert release.wait(5)
        real_cleanup()
    monkeypatch.setattr(process, '_terminate_and_reap', cleanup)
    task = asyncio.create_task(process.communicate())
    while not entered.is_set():
        await asyncio.sleep(.001)
    task.cancel('source_import_runtime_restarted')
    await asyncio.sleep(0)
    task.cancel('second')
    release.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert error.value.args == ('source_import_runtime_restarted',)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_continuous_stderr_cannot_starve_timeout_or_cleanup(tmp_path):
    ready = tmp_path / 'ready'
    script = ('import os,pathlib,sys; pathlib.Path(sys.argv[1]).touch(); '
              'chunk=b"x"*65536\nwhile True: os.write(2,chunk)')
    process = await owned.create_import_subprocess(sys.executable, '-c', script, str(ready))
    for _ in range(500):
        if ready.exists():
            break
        await asyncio.sleep(.001)
    assert ready.exists()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(process.communicate(), .05)
    assert time.monotonic() - started < 3
    assert process.returncode is not None


def test_parent_death_closes_private_pipe_and_stops_supervisor_descendants(tmp_path):
    import subprocess
    import json
    ready, group_file, gate, output = (tmp_path / name for name in ('ready', 'group', 'gate', 'output'))
    child = ('import os,pathlib,sys,time,json; ready,gate,out=map(pathlib.Path,sys.argv[1:]); '
             'ready.write_text(str(os.getpid()));\n'
             'while not gate.exists(): time.sleep(.01)\nout.write_text("late")')
    command = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
               + repr(child) + ',*sys.argv[1:]]).wait()')
    owner = '''import asyncio,pathlib,sys
from services.owned_import_process import create_import_subprocess
async def main():
    process=await create_import_subprocess(sys.executable,'-c',sys.argv[1],*sys.argv[2:5])
    pathlib.Path(sys.argv[5]).write_text(str(process.pid))
    while not pathlib.Path(sys.argv[2]).exists(): await asyncio.sleep(.001)
    # Simulate an ungraceful application exit: no coroutine cleanup runs.
    import os
    os._exit(0)
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, '-c', owner, command, str(ready), str(gate), str(output), str(group_file)],
                            cwd=__import__('pathlib').Path(__file__).resolve().parents[1], timeout=10)
    assert result.returncode == 0
    pids = [int(ready.read_text()), int(group_file.read_text())]
    def alive(pid):
        # Observational only: no signal is sent to an orphan's numeric PID.
        check = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
        return check.returncode == 0 and check.stdout.strip() and not check.stdout.strip().startswith('Z')
    deadline = time.monotonic() + 5
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not any(alive(pid) for pid in pids)
    gate.touch()
    time.sleep(.05)
    assert not output.exists()
