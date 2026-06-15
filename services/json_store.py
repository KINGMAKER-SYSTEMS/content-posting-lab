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
from pathlib import Path
from typing import Any, Callable


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
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=default)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
