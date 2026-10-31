"""Manual seed/recovery copy of the Scrambler originality feed onto the volume.

The scheduled path is services/scrambler_feed_pull.py (hourly R2 pull inside
the app, ON only). This tool is the operator's manual fallback: seed the volume
from a local directory, or recover when R2 is unreachable. It publishes the same data through the shared manifest-verified writer.

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
from services import scrambler_feed_pull as feed

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
    within its byte bound (usage 64 MiB, origins 8 MiB). Readers accept only a complete verified generation and refuse during publication.
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
    try:
        usage_raw, origins_raw, verified_age = originality._verified_feed_snapshot(usage_src, origins_src)
        if now - verified_age > stale_after_s:
            raise FeedSyncRefused("verified usage is stale")
        after = usage_src.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise FeedSyncRefused("usage changed while reading")
        json.loads(origins_raw)
        manifest = json.dumps({
            "generated_at": datetime.fromtimestamp(min(stat.st_mtime, verified_age), timezone.utc).isoformat(),
            "rows": feed.count_usage_rows(usage_raw),
            "sha256s": {"usage.jsonl": feed.sha256_hex(usage_raw),
                        "origins.json": feed.sha256_hex(origins_raw)},
        }).encode()
        feed._write_all(dest, {"manifest.json": manifest, "usage.jsonl": usage_raw,
                               "origins.json": origins_raw}, data_mtime=min(stat.st_mtime, verified_age))
    except (originality.ScramblerOriginalityUnavailable, OSError, ValueError,
            feed.ScramblerFeedPullError) as error:
        raise FeedSyncRefused(f"feed sync failed: {error}") from error
    return {
        "usageBytes": len(usage_raw),
        "originsBytes": len(origins_raw),
        "usageAgeSeconds": int(now - min(stat.st_mtime, verified_age)),
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
