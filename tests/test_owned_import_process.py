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


def _darwin_process_observation(pid, query=None):
    """Read identity/state without spawning ps or signalling a numeric PID."""
    import ctypes
    import errno
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint32) for name in (
                'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid',
                'ruid', 'rgid', 'svuid', 'svgid', 'reserved')
        ] + [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
            (name, ctypes.c_uint32) for name in (
                'nfiles', 'pgid', 'pjobc', 'ttydev', 'tpgid')
        ] + [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
             ('start_usec', ctypes.c_uint64)]
    if query is None:
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                          ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
    info = ProcBsdInfo()
    ctypes.set_errno(0)
    count = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    code = ctypes.get_errno()
    if count == 0 and code == errno.ESRCH:
        return None
    if count != ctypes.sizeof(info):
        raise OSError(code, 'native process observation failed or was incomplete')
    if (info.pid != pid or info.status not in (1, 2, 3, 4, 5)
            or info.start_sec == 0 or info.start_usec >= 1_000_000):
        raise AssertionError('native process identity/state is invalid')
    return (info.start_sec, info.start_usec), info.status == 5


def _process_observation(pid):
    assert pid > 1
    if sys.platform == 'darwin':
        return _darwin_process_observation(pid)
    # Linux exposes a native birth tick and state without a subprocess. A
    # missing /proc mount is an observation failure, never proof of death.
    from pathlib import Path
    if not sys.platform.startswith('linux'):
        raise RuntimeError('native process observation is unsupported')
    Path('/proc/self/stat').read_bytes()
    try:
        stat = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return None
    opening, closing = stat.find(b'('), stat.rfind(b')')
    assert opening > 0 and closing > opening
    fields = stat[closing + 1:].split()
    assert int(stat[:opening]) == pid and len(fields) >= 20
    assert fields[0] in (b'R', b'S', b'D', b'Z', b'T', b't', b'X', b'x', b'K', b'W', b'P', b'I')
    birth = int(fields[19])
    assert birth > 0
    return birth, fields[0] in (b'Z', b'X', b'x')


@pytest.mark.parametrize('count,code,state,pid,birth', [
    (0, 1, 2, 2222, 1), (1, 0, 2, 2222, 1),
    (None, 0, 0, 2222, 1), (None, 0, 6, 2222, 1),
    (None, 0, 2, 2223, 1), (None, 0, 2, 2222, 0),
])
def test_native_observation_errors_never_mean_process_death(count, code, state, pid, birth):
    import ctypes
    def query(expected, flavor, arg, pointer, size):
        info = pointer._obj
        info.pid, info.status, info.start_sec = pid, state, birth
        ctypes.set_errno(code)
        return size if count is None else count
    with pytest.raises((OSError, AssertionError)):
        _darwin_process_observation(2222, query)


def test_native_observation_distinguishes_live_stopped_zombie_and_absent():
    import ctypes
    import errno
    for state in (1, 2, 3, 4, 5):
        def query(pid, flavor, arg, pointer, size):
            info = pointer._obj
            info.pid, info.status, info.start_sec, info.start_usec = pid, state, 10, 42
            return size
        assert _darwin_process_observation(2222, query) == ((10, 42), state == 5)
    def absent(*args):
        ctypes.set_errno(errno.ESRCH)
        return 0
    assert _darwin_process_observation(2222, absent) is None


def test_native_observation_of_real_owned_child_before_and_after_natural_exit():
    import subprocess
    child = subprocess.Popen([sys.executable, '-c',
        'import sys; print("ready",flush=True); sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline() == b'ready\n'
        before = _process_observation(child.pid)
        assert before is not None and not before[1]
    finally:
        child.stdin.close()
        assert child.wait(timeout=5) == 0
        child.stdout.close()
    after = _process_observation(child.pid)
    assert after is None or after[0] != before[0] or after[1]


@pytest.mark.parametrize('state,terminal', [(b'S', False), (b'T', False), (b'Z', True), (b'X', True)])
def test_linux_observation_parses_birth_after_parenthesized_command(monkeypatch, state, terminal):
    from pathlib import Path
    monkeypatch.setattr(sys, 'platform', 'linux')
    # starttime is stat field22: field3 (state), eighteen intervening fields,
    # then the native birth tick. Spaces and parentheses in comm are legal.
    fields = [state] + [b'0'] * 18 + [b'123456']
    stat = b'2222 (command (with) spaces) ' + b' '.join(fields)
    def read(path):
        assert str(path) in ('/proc/self/stat', '/proc/2222/stat')
        return stat
    monkeypatch.setattr(Path, 'read_bytes', read)
    assert _process_observation(2222) == (123456, terminal)


@pytest.mark.parametrize('failure', ['target-absent', 'self-absent', 'denied', 'malformed', 'wrong-pid', 'zero-birth', 'unknown-state'])
def test_linux_observation_requires_readable_kernel_evidence(monkeypatch, failure):
    from pathlib import Path
    monkeypatch.setattr(sys, 'platform', 'linux')
    def read(path):
        if str(path) == '/proc/self/stat':
            if failure == 'self-absent':
                raise FileNotFoundError('no proc mount')
            return b'present'
        assert str(path) == '/proc/2222/stat'
        if failure == 'target-absent':
            raise FileNotFoundError('original target gone')
        if failure == 'denied':
            raise PermissionError('process observation denied')
        if failure == 'malformed':
            return b'2222 (short) S'
        pid = b'2223' if failure == 'wrong-pid' else b'2222'
        state = b'?' if failure == 'unknown-state' else b'S'
        birth = b'0' if failure == 'zero-birth' else b'123456'
        return pid + b' (comm) ' + b' '.join([state] + [b'0'] * 18 + [birth])
    monkeypatch.setattr(Path, 'read_bytes', read)
    if failure == 'target-absent':
        assert _process_observation(2222) is None
    else:
        with pytest.raises((OSError, AssertionError)):
            _process_observation(2222)


def test_unknown_platform_cannot_claim_process_death(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'unsupported')
    with pytest.raises(RuntimeError, match='unsupported'):
        _process_observation(2222)


def test_parent_death_closes_private_pipe_and_stops_supervisor_descendants(tmp_path):
    import subprocess
    ready, group_file, gate, output, release_owner = (
        tmp_path / name for name in ('ready', 'group', 'gate', 'output', 'release-owner'))
    child = ('import os,pathlib,sys,time; ready,gate,out=map(pathlib.Path,sys.argv[1:]); '
             'ready.write_text(str(os.getpid()));\n'
             'while not gate.exists(): time.sleep(.01)\nout.write_text("late")')
    command = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c",'
               + repr(child) + ',*sys.argv[1:]]).wait()')
    owner = '''import asyncio,pathlib,sys,time
from services.owned_import_process import create_import_subprocess
async def main():
    process=await create_import_subprocess(sys.executable,'-c',sys.argv[1],*sys.argv[2:5])
    pathlib.Path(sys.argv[5]).write_text(str(process.pid))
    deadline=time.monotonic()+10
    while not pathlib.Path(sys.argv[6]).exists():
        if time.monotonic()>deadline: raise TimeoutError('owner exit handshake missing')
        await asyncio.sleep(.001)
    # Simulate an ungraceful application exit: no coroutine cleanup runs.
    import os
    os._exit(0)
asyncio.run(main())
'''
    parent = subprocess.Popen([sys.executable, '-c', owner, command,
        str(ready), str(gate), str(output), str(group_file), str(release_owner)],
        cwd=__import__('pathlib').Path(__file__).resolve().parents[1])
    try:
        deadline = time.monotonic() + 5
        while not (ready.exists() and group_file.exists()) and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists() and group_file.exists()
        pids = [int(ready.read_text()), int(group_file.read_text())]
        births = {}
        for pid in pids:
            observation = _process_observation(pid)
            assert observation is not None and not observation[1]
            births[pid] = observation[0]
        # Both exact births were observed alive before their owner is allowed
        # to exit. ESRCH, zombie state or replacement birth proves those original
        # processes stopped; denied/short/unknown reads always fail the test.
        release_owner.touch()
        assert parent.wait(timeout=10) == 0
        def alive(pid):
            observation = _process_observation(pid)
            return observation is not None and observation[0] == births[pid] and not observation[1]
        deadline = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not any(alive(pid) for pid in pids)
    finally:
        # Release only our finite fixture paths, including on a broken-cleanup
        # mutant; never signal an orphan's numeric PID or group for test cleanup.
        release_owner.touch()
        gate.touch()
        parent.wait(timeout=10)
    time.sleep(.05)
    assert not output.exists()
