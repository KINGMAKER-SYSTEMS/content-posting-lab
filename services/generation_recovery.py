"""Private generation checkpoints; no prompts, provider retries, or wire API."""

import asyncio
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading

from services.json_store import lock_for as thread_lock_for

_held = threading.local()


@contextmanager
def store_lock(path):
    """Serialize the existing JSON transaction across threads AND runtimes."""
    path = Path(path).resolve()
    key = str(path)
    with thread_lock_for(path):
        held = getattr(_held, "paths", set())
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.paths = held | {key}
            yield
        finally:
            _held.paths = held
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@contextmanager
def runner_lock(store_path, job_id):
    """A kernel-held claim survives no process; lock files are not leases."""
    root = Path(store_path).parent / ".generation-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / (job_id + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def input_hash(model, parameters):
    return hashlib.sha256(json.dumps(
        {"model": model, "input": parameters}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


class PredictionCheckpoint:
    """Await durable intent before submission; await identity before polling."""

    def __init__(self, record, persist):
        self.record = copy.deepcopy(record)
        self.persist = persist

    async def save(self, record):
        snapshot = copy.deepcopy(record)
        await asyncio.to_thread(self.persist, snapshot)
        self.record = snapshot
