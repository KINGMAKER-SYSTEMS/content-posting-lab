"""
Shared atomic JSON load/save.

The lab's data layers (telegram_config.json, page_roster.json,
content_requests.json, upload_jobs.json) all persisted JSON the same way:
read with a try/except that falls back to an empty value on missing/corrupt
files, and write via a tmp file + rename so a reader never sees a partial.
That pattern was copy-pasted four times. It lives here once.

The atomic write (tmp + flush + fsync + os.replace) is the proven one: a raw
open('w')+dump left the upload-job queue truncated/wiped if the process
crashed mid-write (a P0). Do not weaken it.
"""

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

# Matches the tmp sidecars atomic_save creates: ``<name>.<pid>.<tid>.tmp``.
# Used to find orphans left by a crash between fsync and os.replace.
_TMP_RE = re.compile(r"\.(\d+)\.\d+\.tmp$")

# Per-path reentrant locks so a load-modify-save transaction can be serialized.
# atomic_save only atomizes the write; it does NOT stop two concurrent
# read-modify-write callers from clobbering each other (TOCTOU). The setters in
# services/telegram.py run as sync calls from async routers (FastAPI's sync
# threadpool, the bot thread, asyncio.to_thread), so the racing callers share a
# process — a threading lock is enough. Reentrant so a transaction that calls
# another transaction on the same file doesn't self-deadlock.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path: Path | str) -> threading.RLock:
    """Return the process-wide reentrant lock guarding mutations to ``path``."""
    key = str(Path(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (POSIX ``kill(pid, 0)``)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive (don't reap it).
        return True
    except OSError:
        return True
    return True


def _sweep_orphan_tmps(p: Path) -> None:
    """Remove tmp sidecars of ``p`` whose owning process is no longer alive.

    A crash between fsync and ``os.replace`` orphans a ``<name>.<pid>.<tid>.tmp``.
    atomic_load never reads it (it reads ``path``), so it's not a correctness bug,
    but on the long-lived Railway volume the orphans accumulate and waste space.
    We only reap tmps whose pid is dead, so an in-flight concurrent writer's tmp
    is never touched. Best-effort: any error here is swallowed.
    """
    try:
        siblings = list(p.parent.glob(f"{p.name}.*.tmp"))
    except OSError:
        return
    for tmp in siblings:
        m = _TMP_RE.search(tmp.name)
        if not m:
            continue
        if _pid_alive(int(m.group(1))):
            continue
        try:
            tmp.unlink()
        except OSError:
            pass


def atomic_load(path: Path | str, *, default: Any = None) -> Any:
    """Load JSON from ``path``. Returns ``default`` if the file is missing or corrupt."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return default


def atomic_save(
    path: Path | str,
    data: Any,
    *,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Atomically write ``data`` as JSON to ``path`` (tmp file, fsync, then rename).

    ``default`` is the json.dump fallback serializer (e.g. ``str`` for datetimes).

    The tmp file gets a unique per-writer suffix (pid+thread) so two concurrent
    ``atomic_save`` calls on the same path don't share one tmp and clobber/race
    each other's bytes — without it the second caller's ``os.replace`` could hit
    a half-written or already-renamed-away tmp. On any failure (serialize, fsync,
    replace) the tmp is removed so it never leaks or poisons a later save; the
    pre-existing ``path`` is left untouched because the write went to tmp first.
    """
    p = Path(path)
    tmp = p.with_suffix(f"{p.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=default)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        # Opportunistically reap tmps orphaned by an earlier crash (dead pid).
        _sweep_orphan_tmps(p)
    except Exception:
        # ponytail: best-effort cleanup; a crash between fsync and replace can
        # still orphan a tmp, but a stale tmp never breaks atomic_load (it reads
        # `path`, not the tmp) and the unique suffix keeps it out of the way.
        # Catch Exception (not BaseException) so SystemExit/KeyboardInterrupt
        # propagate cleanly during shutdown — a stale tmp is harmless either way.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
