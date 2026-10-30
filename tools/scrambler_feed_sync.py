"""Manual seed/recovery copy of the Scrambler originality feed onto the volume.

The scheduled path is services/scrambler_feed_pull.py (hourly R2 pull inside
the app, ON only). This tool is the operator's manual fallback: seed the volume
from a local directory, or recover when R2 is unreachable. It writes the same
two files to the same directory, in the same order (origins.json first).

The Scrambler ON path requires a 26-hour-fresh ``usage.jsonl`` beside a valid
``origins.json`` (services/scrambler_originality.py). This tool is the missing
provisioning/sync bridge called out in the environment review: it copies the
authoritative feed from a source directory into the Railway durable volume
(``RAILWAY_VOLUME_MOUNT_PATH/originality`` by default) atomically, refusing
rather than forwarding a stale, missing or oversized usage ledger.

It is a producer only: it never reads history, never publishes, and never
changes what the originality loader enforces. The loader stays the authority -
this tool just makes a fresh copy available where the ON path will read it.

Exit 0: feed synced. Exit 1: refused (missing/stale/oversized/corrupt source).
Exit 2: configuration error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import scrambler_originality as originality

USAGE_NAME = "usage.jsonl"
ORIGINS_NAME = "origins.json"


def _bounded_read(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise FeedSyncRefused(f"{path} is more than {max_bytes} bytes")
    return raw


def _atomic_write(path: Path, raw: bytes) -> None:
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FeedSyncRefused(RuntimeError):
    """The source feed is not safe to forward: refuse, never sync a bad feed."""


def sync_feed(
    source_dir: str | Path,
    dest_dir: str | Path,
    *,
    now_epoch: float | None = None,
    stale_after_s: float = originality.USAGE_STALE_AFTER_S,
) -> dict:
    """Copy usage.jsonl + origins.json from ``source_dir`` to ``dest_dir``.

    Refuses (``FeedSyncRefused``) unless BOTH files exist in the source, the
    usage ledger is within ``stale_after_s`` of ``now_epoch``, and each file is
    within its byte bound (usage 64 MiB, origins 8 MiB). Writes are atomic
    (tmp + fsync + replace) so a reader never sees a half-copied feed.
    """
    source = Path(source_dir)
    dest = Path(dest_dir)
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    if not source.is_dir():
        raise FeedSyncRefused(f"feed source directory missing: {source}")
    usage_src = source / USAGE_NAME
    origins_src = source / ORIGINS_NAME
    if not usage_src.is_file():
        raise FeedSyncRefused(f"usage ledger missing: {usage_src}")
    if not origins_src.is_file():
        raise FeedSyncRefused(f"origins map missing: {origins_src}")
    try:
        stat = usage_src.stat()
    except OSError as error:
        raise FeedSyncRefused(f"cannot stat {usage_src}: {error}") from error
    if now - stat.st_mtime > stale_after_s:
        hours = int((now - stat.st_mtime) / 3600)
        raise FeedSyncRefused(f"usage ledger is {hours} h stale (limit {stale_after_s / 3600:.0f} h)")
    usage_raw = _bounded_read(usage_src, originality.USAGE_MAX_BYTES)
    origins_raw = _bounded_read(origins_src, originality.ORIGINS_MAX_BYTES)
    # Refuse to forward a JSON-invalid origins map (usage.jsonl is validated
    # later by the loader, line by line, but the map is cheap to check here).
    try:
        json.loads(origins_raw)
    except (json.JSONDecodeError, ValueError) as error:
        raise FeedSyncRefused(f"origins map is not valid JSON: {error}") from error
    dest.mkdir(parents=True, exist_ok=True)
    # origins.json first, then usage.jsonl: the same order as the scheduled
    # pull (services/scrambler_feed_pull.py WRITE_ORDER). The loader reads
    # origins before usage, so the files on disk never form NEW usage beside OLD
    # origins. (A reader that spans both writes can still pair generations; the
    # impact is small: each usage row carries its own origin identity.)
    _atomic_write(dest / ORIGINS_NAME, origins_raw)
    _atomic_write(dest / USAGE_NAME, usage_raw)
    return {
        "usageBytes": len(usage_raw),
        "originsBytes": len(origins_raw),
        "usageAgeSeconds": int(now - stat.st_mtime),
        "dest": str(dest),
    }


def _default_dest() -> Path:
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount and Path(mount).is_absolute():
        return Path(mount) / "originality"
    raise ValueError("feed_sync_dest_unset: pass --dest or set RAILWAY_VOLUME_MOUNT_PATH")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Directory holding usage.jsonl + origins.json")
    parser.add_argument("--dest", help="Destination directory (default: $RAILWAY_VOLUME_MOUNT_PATH/originality)")
    parser.add_argument("--stale-hours", type=float, help="Max usage ledger age in hours (default 26)")
    args = parser.parse_args(argv)
    try:
        dest = Path(args.dest) if args.dest else _default_dest()
        stale = args.stale_hours * 3600.0 if args.stale_hours else originality.USAGE_STALE_AFTER_S
        result = sync_feed(args.source, dest, stale_after_s=stale)
    except (FeedSyncRefused, ValueError) as error:
        print(json.dumps({"ok": False, "reason": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
