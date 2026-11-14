"""Private process sessions with a retained, unreaped leader until cleanup.

Only this object waits for its Popen session leader; asyncio never registers
a child watcher for it. The leader reports command exit over a private pipe
and stays alive until cleanup. Its unreaped identity reserves the PID/PGID.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import signal
import subprocess
import sys

_LIMIT = 8 * 1024 * 1024


def _check_waitable(pid: int):
    """Validate the owned child without reaping, including an exited leader."""
    if hasattr(os, "waitid"):
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    # CPython omits waitid on macOS, whose libc provides the POSIX interface.
    # The opaque, aligned buffer is deliberately not decoded.
    libc = ctypes.CDLL(None, use_errno=True)
    result = (ctypes.c_long * 64)()
    if libc.waitid(os.P_PID, pid, ctypes.byref(result), os.WEXITED | os.WNOHANG | os.WNOWAIT):
        code = ctypes.get_errno()
        if code == errno.ECHILD:
            raise ChildProcessError(code, os.strerror(code))
        raise OSError(code, os.strerror(code))
    return None


# The session leader remains live after the command exits. Its private status
# pipe reports the command result; stdin is an owner-held cleanup handshake.
# This is also necessary on macOS, where signalling an all-zombie group yields
# EPERM even though its leader remains a waitable child.
_SUPERVISOR = """import os,signal,subprocess,sys,threading
status=int(sys.argv[1])
def owner_pipe_closed():
    os.read(0,1)
    os.killpg(os.getpgrp(),signal.SIGKILL)
watcher=threading.Thread(target=owner_pipe_closed,daemon=True)
watcher.start()
try:
    command=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL)
    result=command.wait()
except BaseException:
    result=127
os.write(status,(str(result)+"\\n").encode())
os.close(status)
watcher.join()
"""


class OwnedImportProcess:
    def __init__(self, args, *, stdout, stderr):
        read_status, write_status = os.pipe()
        self._status = read_status
        self._status_buffer = bytearray()
        self._command_returncode = None
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-c", _SUPERVISOR, str(write_status), *args],
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                pass_fds=(write_status,), start_new_session=True)
        except BaseException:
            os.close(read_status)
            raise
        finally:
            os.close(write_status)
        self.pid = self._process.pid
        self._cleanup = None
        os.set_blocking(read_status, False)
        for pipe in (self._process.stdout, self._process.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)

    @property
    def returncode(self):
        # Do not call Popen.poll(): it reaps the ownership anchor.
        if self._process.returncode is None:
            return None
        return self._command_returncode if self._command_returncode is not None else self._process.returncode

    def _exited(self):
        _check_waitable(self.pid)
        try:
            chunk = os.read(self._status, 64)
        except BlockingIOError:
            return False
        self._status_buffer.extend(chunk)
        if b"\n" in self._status_buffer:
            self._command_returncode = int(self._status_buffer.strip())
            return True
        if not chunk:
            raise RuntimeError("source import supervisor exited without command result")
        return False

    def _terminate_and_reap(self):
        try:
            if self._process.returncode is not None:
                raise RuntimeError("source import child was reaped outside its owner")
            try:
                _check_waitable(self.pid)
            except ChildProcessError as error:
                # Never signal a numeric group after its owned anchor was lost.
                self._process.returncode = 255
                raise RuntimeError("source import child ownership lost") from error
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait()
        finally:
            os.close(self._status)
            self._process.stdin.close()

    async def terminate(self):
        if self._cleanup is None:
            self._cleanup = asyncio.get_running_loop().run_in_executor(None, self._terminate_and_reap)
        cancellation = None
        while True:
            try:
                await asyncio.shield(self._cleanup)
                return cancellation
            except asyncio.CancelledError as error:
                # Retain the first cancellation and the claim until cleanup ends.
                cancellation = cancellation or error

    async def communicate(self):
        buffers = [bytearray(), bytearray()]
        pipes = [self._process.stdout, self._process.stderr]
        def drain(*, max_reads=16):
            for index, pipe in enumerate(pipes):
                if pipe is None:
                    continue
                for _ in range(max_reads):
                    try:
                        chunk = os.read(pipe.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    buffers[index].extend(chunk)
                    if len(buffers[index]) > _LIMIT:
                        if index == 0:
                            raise RuntimeError("source import subprocess output exceeds limit")
                        del buffers[index][:-_LIMIT]
        failure = None
        try:
            while True:
                drain()
                if self._exited():
                    break
                await asyncio.sleep(0.01)
        except BaseException as error:
            failure = error
            raise
        finally:
            # Also clean descendants after a successful leader exit; a child may
            # otherwise keep pipes open or continue writing the attempt directory.
            cancellation = await self.terminate()
            try:
                drain(max_reads=128)
            finally:
                for pipe in pipes:
                    if pipe is not None:
                        pipe.close()
            if failure is None and cancellation is not None:
                raise cancellation
        return tuple(bytes(buffer) for buffer in buffers)


async def create_import_subprocess(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True):
    if start_new_session is not True:
        raise ValueError("source import requires its own process session")
    return OwnedImportProcess(args, stdout=stdout, stderr=stderr)
