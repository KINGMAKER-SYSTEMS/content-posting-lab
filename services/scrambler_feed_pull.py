"""Scheduled pull of the Scrambler's originality feed from a PRIVATE R2 bucket.

Every 60 minutes (only when ``SCRAMBLER_ENABLED=1``) this pulls the three feed
files ``originality/feed/{manifest.json,usage.jsonl,origins.json}`` from a
private R2 bucket given by env. ``SCRAMBLER_FEED_R2_BUCKET`` names the bucket
and ``SCRAMBLER_FEED_R2_PREFIX`` is the key prefix of the feed directory itself
(pilot: ``rt-content-buckets`` and ``originality/feed/``, so the objects are
``originality/feed/{manifest.json,usage.jsonl,origins.json}``, exactly where the
producer, control-plane tools/launchd/bin/originality-feed.sh, puts them). Each
data file's sha256 is verified against the manifest's REQUIRED ``sha256s`` map
and the usage row count against ``rows``, then all
three files are written with per-file atomicity (temp file + ``os.replace``)
into the destination directory, origins.json before usage.jsonl (this narrows,
but does not close, the window in which a concurrent reader can pair two
generations; see ``WRITE_ORDER``). usage.jsonl and origins.json get the
manifest's ``generated_at`` as their mtime (capped at now), NOT the pull time:
the Scrambler's 26-hour usage freshness gate (services/scrambler_originality.py
``USAGE_STALE_AFTER_S``) reads that mtime, so it measures the age of the DATA.
A feed the producer has not refreshed for 26 hours therefore still refuses ON,
however often it is pulled.

Failure handling: nothing is written until every file is downloaded AND
verified. Any fetch error, missing sha256s entry, sha256 mismatch, or row-count
mismatch keeps the OLD files untouched and logs a named reason. A mid-write
``os.replace`` failure rolls the already-replaced files back to their previous
contents (best-effort) and logs the same named reason. A manifest whose
``generated_at`` cannot be parsed as an ISO 8601 time is refused
(``manifest_invalid``), because the files' mtime is taken from it. A manifest
whose ``generated_at`` is older than 20 hours logs a warning but does NOT fail
the pull; the files keep that old mtime, so the 26 h gate still refuses them
once the data itself is over 26 hours old.

An independent optional summary pull runs when
``CONTENT_LAB_ORIGINALITY_SUMMARY_FEED_ENABLED=1`` and publishes only to the
absolute ``CONTENT_LAB_ORIGINALITY_SUMMARY_PATH``. It verifies the private
manifest hash, bound timestamps, shape and 26-hour freshness; errors keep the
last valid file. This switch does not enable the Scrambler.

The scheduled Scrambler pull runs only when ``SCRAMBLER_ENABLED=1``. The scheduler thread
itself is started on every boot (app lifespan); while OFF it only sleeps and
re-reads the switch once an hour: no R2 call, no write. One manual pull,
regardless of the switch, is ``tools/scrambler_report.py --pull-feed-once``
(the pilot-flip bootstrap). No new HTTP response fields and no new public
endpoint are added.

Env names are kept in lockstep with the existing switches:
- ``SCRAMBLER_ENABLED`` == services.scrambler.SCRAMBLER_ENABLED_ENV
- ``CONTENT_LAB_ORIGINALITY_USAGE_PATH`` == services.scrambler_originality.ORIGINALITY_USAGE_PATH_ENV
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import threading
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCRAMBLER_ENABLED_ENV = "SCRAMBLER_ENABLED"
FEED_R2_BUCKET_ENV = "SCRAMBLER_FEED_R2_BUCKET"
FEED_R2_PREFIX_ENV = "SCRAMBLER_FEED_R2_PREFIX"
CONTENT_LAB_ORIGINALITY_USAGE_PATH_ENV = "CONTENT_LAB_ORIGINALITY_USAGE_PATH"
RAILWAY_VOLUME_MOUNT_PATH_ENV = "RAILWAY_VOLUME_MOUNT_PATH"

# Default key prefix of the feed directory, used only when
# SCRAMBLER_FEED_R2_PREFIX is empty. The env prefix IS the feed directory
# (pilot value ``originality/feed/``); nothing is appended to it.
FEED_REL_PREFIX = "originality/feed"
FEED_FILES = ("manifest.json", "usage.jsonl", "origins.json")
DATA_FILES = ("usage.jsonl", "origins.json")

# Replace order for the final files. origins.json precedes usage.jsonl:
# ``load_originality_index`` (services/scrambler_originality.py) reads
# origins.json BEFORE usage.jsonl, so replacing origins first means a reader
# that reads NEW origins always reads NEW usage after it. This NARROWS the
# mixed-generation window but does not close it: a reader that read OLD
# origins.json just before both renames and then reads usage.jsonl after them
# still pairs OLD origins with NEW usage. The impact is small, because each
# producer usage row carries its own origin identity (origin_kind, origin_id,
# origin_start_s/end_s). manifest.json is not consumed by the Scrambler reader
# and goes last.
WRITE_ORDER = ("origins.json", "usage.jsonl", "manifest.json")

PULL_INTERVAL_SECONDS = 60 * 60
MANIFEST_STALE_AFTER_S = 20 * 3600.0

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ScramblerFeedPullError(RuntimeError):
    """Named reason a feed pull refused to write (old files kept).

    ``reason`` is the stable, greppable name (e.g. ``sha256_mismatch:usage.jsonl``).
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FeedManifest:
    generated_at: str
    generated_at_epoch: float | None
    rows: int
    sha256s: dict[str, str]  # filename -> lowercase hex64


@dataclass(frozen=True)
class PullResult:
    wrote: tuple[str, ...]      # filenames written, sorted
    warnings: tuple[str, ...]   # e.g. ("manifest_stale",)


def _parse_epoch(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_manifest(data: bytes) -> FeedManifest:
    """Parse and validate the feed manifest; raises a named reason on any fault."""
    try:
        obj = json.loads(data)
    except Exception as error:
        raise ScramblerFeedPullError(f"manifest_invalid: not valid JSON: {error}") from error
    if not isinstance(obj, dict):
        raise ScramblerFeedPullError("manifest_invalid: top-level value is not an object")
    generated_at = obj.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ScramblerFeedPullError("manifest_invalid: generated_at missing or not a string")
    rows = obj.get("rows")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ScramblerFeedPullError("manifest_invalid: rows missing or not a non-negative int")
    sha256s = obj.get("sha256s")
    if not isinstance(sha256s, dict):
        raise ScramblerFeedPullError("manifest_invalid: sha256s missing or not an object")
    normalized: dict[str, str] = {}
    for name, digest in sha256s.items():
        if not isinstance(name, str) or not isinstance(digest, str) \
                or not _SHA256_RE.match(digest):
            raise ScramblerFeedPullError(
                f"manifest_invalid: sha256s entry for {name!r} is not a hex64 digest"
            )
        normalized[name] = digest.lower()
    return FeedManifest(
        generated_at=generated_at,
        generated_at_epoch=_parse_epoch(generated_at),
        rows=rows,
        sha256s=normalized,
    )


def count_usage_rows(data: bytes) -> int:
    """Number of non-empty lines in usage.jsonl (one row per confirmed post)."""
    return sum(1 for line in data.split(b"\n") if line.strip())


def feed_config() -> tuple[str, str] | None:
    """(bucket, prefix) from env, or None when the bucket name is not configured."""
    bucket = os.environ.get(FEED_R2_BUCKET_ENV, "").strip()
    if not bucket:
        return None
    prefix = os.environ.get(FEED_R2_PREFIX_ENV, "").strip()
    return bucket, prefix


def feed_key(prefix: str, filename: str) -> str:
    """R2 object key for one feed file: ``<prefix>/<filename>``.

    The prefix is the feed directory itself (``originality/feed/`` gives
    ``originality/feed/manifest.json``); an empty prefix uses FEED_REL_PREFIX.
    """
    base = (prefix or "").strip().strip("/") or FEED_REL_PREFIX
    return f"{base}/{filename}"


def dest_dir() -> Path | None:
    """Where the feed files land: the usage-path dir, else the volume mount."""
    usage = os.environ.get(CONTENT_LAB_ORIGINALITY_USAGE_PATH_ENV, "").strip()
    if usage:
        return Path(usage).parent
    mount = os.environ.get(RAILWAY_VOLUME_MOUNT_PATH_ENV, "").strip()
    if mount:
        return Path(mount) / "originality"
    return None


def scrambler_enabled_now() -> bool:
    return os.environ.get(SCRAMBLER_ENABLED_ENV, "0").strip() == "1"


def _fetch_one(fetch, name: str) -> bytes:
    try:
        data = fetch(name)
    except ScramblerFeedPullError:
        raise
    except Exception as error:
        raise ScramblerFeedPullError(f"fetch_failed:{name}: {error}") from error
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ScramblerFeedPullError(f"fetch_failed:{name}: fetched value is not bytes")
    return bytes(data)


def _verify_sha(name: str, data: bytes, manifest: FeedManifest) -> None:
    if name not in manifest.sha256s:
        raise ScramblerFeedPullError(f"sha256_missing:{name}")
    if sha256_hex(data) != manifest.sha256s[name]:
        raise ScramblerFeedPullError(f"sha256_mismatch:{name}")


def _snapshot_old(dest: Path, names: tuple[str, ...]) -> dict[str, Path | None]:
    """Snapshot each current file so a failed replace can be rolled back.

    Uses a hardlink (no byte copy, no extra RAM) with a streaming-copy fallback
    for filesystems without hardlinks. Returns ``{name: backup_path_or_None}``;
    ``None`` means the old file did not exist or could not be snapshotted, so a
    rollback will best-effort remove that name instead of restoring it.
    """
    backups: dict[str, Path | None] = {}
    for name in names:
        final = dest / name
        backup = dest / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.bak"
        try:
            os.link(final, backup)
        except FileNotFoundError:
            backups[name] = None
        except OSError:
            try:
                with open(final, "rb") as src, open(backup, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                backups[name] = backup
            except FileNotFoundError:
                backups[name] = None
            except OSError:
                _unlink_quiet(backup)
                backups[name] = None
        else:
            backups[name] = backup
    return backups


def _rollback_quiet(dest: Path, backups: dict[str, Path | None],
                    replaced: list[str]) -> None:
    """Best-effort restore of the files a mid-replace failure already swapped.

    Only the names in ``replaced`` actually got a NEW inode; files that were not
    yet replaced still carry their old content, so trying to rename their
    hardlink backup onto them would be a same-inode rename (a no-op that leaves
    the ``.bak`` link behind). Those backups are discarded by the caller.

    Restores run in REVERSE replace order (usage.jsonl before origins.json), so
    the files on disk never form the OLD origins.json + NEW usage.jsonl pair
    mid-rollback. (A reader that spans the restores can still pair generations;
    see ``WRITE_ORDER``.)
    """
    for name in reversed(replaced):
        backup = backups[name]
        final = dest / name
        if backup is None:
            # The old file did not exist: restoring means removing the new one.
            _unlink_quiet(final)
            continue
        try:
            os.replace(backup, final)
        except OSError:
            _unlink_quiet(backup)


def _discard_backups_quiet(backups: dict[str, Path | None]) -> None:
    for backup in backups.values():
        if backup is not None:
            _unlink_quiet(backup)


def _write_all(dest: Path, payloads: dict[str, bytes],
               data_mtime: float | None = None) -> None:
    """Write the feed files with per-file atomicity and rollback on failure.

    Each file is still temp + ``os.replace``, so no reader ever sees a
    half-written file. The replace order (``WRITE_ORDER``) replaces origins.json
    BEFORE usage.jsonl, which narrows (does not close) the window in which a
    reader following ``load_originality_index``'s read order pairs two
    generations (see ``WRITE_ORDER``). With ``data_mtime`` set, each DATA file's
    temp file gets that mtime BEFORE its rename, so the final file appears with
    the data's age already in place (no fresh-mtime moment). If a replace fails
    part-way, the already-replaced files are rolled back to their old contents
    (best-effort) and a named reason is raised.
    """
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ScramblerFeedPullError(f"write_failed: cannot create {dest}: {error}") from error

    order = tuple(name for name in WRITE_ORDER if name in payloads)
    backups = _snapshot_old(dest, order)

    tmp_paths: dict[str, Path] = {}
    try:
        for name in order:
            tmp = dest / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(payloads[name])
                handle.flush()
                os.fsync(handle.fileno())
            tmp_paths[name] = tmp
            if data_mtime is not None and name in DATA_FILES:
                os.utime(tmp, (data_mtime, data_mtime))
    except OSError as error:
        for tmp in tmp_paths.values():
            _unlink_quiet(tmp)
        _discard_backups_quiet(backups)
        raise ScramblerFeedPullError(f"write_failed: {error}") from error

    replaced: list[str] = []
    try:
        for name in order:
            os.replace(tmp_paths[name], dest / name)
            del tmp_paths[name]
            replaced.append(name)
    except OSError as error:
        for tmp in tmp_paths.values():
            _unlink_quiet(tmp)
        _rollback_quiet(dest, backups, replaced)
        _discard_backups_quiet(backups)
        raise ScramblerFeedPullError(f"write_failed: {error}") from error

    _discard_backups_quiet(backups)
    _fsync_dir_quiet(dest)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _fsync_dir_quiet(dest: Path) -> None:
    try:
        fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def pull_once(fetch, dest: Path, now_epoch: float | None = None) -> PullResult:
    """One pull with an injected ``fetch(name) -> bytes`` callable (offline-testable).

    Fetches and verifies EVERYTHING before writing ANYTHING, so any fetch/verify
    failure writes nothing. A write-phase ``os.replace`` failure rolls the
    already-replaced files back to their old contents (best-effort) and raises a
    named reason.
    """
    now = now_epoch if now_epoch is not None else time.time()

    manifest_raw = _fetch_one(fetch, "manifest.json")
    manifest = parse_manifest(manifest_raw)
    if manifest.generated_at_epoch is None:
        # The data files' mtime is taken from generated_at (the 26 h gate reads
        # it), so a time we cannot read must refuse, never fall back to "now".
        raise ScramblerFeedPullError(
            "manifest_invalid: generated_at is not an ISO 8601 time")

    payloads: dict[str, bytes] = {"manifest.json": manifest_raw}
    for name in DATA_FILES:
        data = _fetch_one(fetch, name)
        _verify_sha(name, data, manifest)
        payloads[name] = data

    actual_rows = count_usage_rows(payloads["usage.jsonl"])
    if actual_rows != manifest.rows:
        raise ScramblerFeedPullError(
            f"rows_mismatch: manifest rows={manifest.rows} usage.jsonl rows={actual_rows}"
        )

    warnings: list[str] = []
    if now - manifest.generated_at_epoch > MANIFEST_STALE_AFTER_S:
        warnings.append("manifest_stale")
        log.warning(
            "content_lab.scrambler-feed-pull.manifest_stale generated_at=%s is "
            "%d h old (threshold 20 h); writing it with that age as its mtime",
            manifest.generated_at,
            int((now - manifest.generated_at_epoch) / 3600),
        )

    # mtime = the DATA's age (capped at now for a producer clock ahead of ours),
    # so the loader's 26 h gate refuses a feed the producer stopped refreshing.
    _write_all(dest, payloads, data_mtime=min(manifest.generated_at_epoch, now))
    log.info(
        "content_lab.scrambler-feed-pull.ok generated_at=%s rows=%d dest=%s",
        manifest.generated_at, manifest.rows, dest,
    )
    return PullResult(wrote=tuple(sorted(payloads)), warnings=tuple(warnings))


def r2_fetch(bucket: str, key: str) -> bytes:
    """Fetch one object from the private feed bucket using the shared R2 client."""
    from services import r2  # local import: keep this module import-light for tests

    response = r2.client().get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise ScramblerFeedPullError(f"fetch_failed:{key}: empty/non-bytes body")
    return bytes(body)


def run_feed_pull_once() -> PullResult:
    """One pull wired to the real R2 client and env config (the ON path)."""
    config = feed_config()
    if config is None:
        raise ScramblerFeedPullError(
            f"feed_unconfigured: {FEED_R2_BUCKET_ENV} is not set"
        )
    bucket, prefix = config
    dest = dest_dir()
    if dest is None:
        raise ScramblerFeedPullError(
            f"dest_unconfigured: neither {CONTENT_LAB_ORIGINALITY_USAGE_PATH_ENV} "
            f"nor {RAILWAY_VOLUME_MOUNT_PATH_ENV} is set"
        )

    def fetch(name: str) -> bytes:
        return r2_fetch(bucket, feed_key(prefix, name))

    return pull_once(fetch, dest)


SUMMARY_FEED_ENABLED_ENV = "CONTENT_LAB_ORIGINALITY_SUMMARY_FEED_ENABLED"
SUMMARY_PATH_ENV = "CONTENT_LAB_ORIGINALITY_SUMMARY_PATH"
SUMMARY_NAME = "usage-summary.json"
SUMMARY_MAX_BYTES = 2 * 1024 * 1024
SUMMARY_MANIFEST_MAX_BYTES = 128 * 1024


def summary_enabled_now() -> bool:
    return os.environ.get(SUMMARY_FEED_ENABLED_ENV, "0").strip() == "1"


def _summary_fetch(fetch, name: str, limit: int) -> bytes:
    try:
        data = fetch(name)
    except Exception:
        raise ScramblerFeedPullError("summary_fetch_failed") from None
    if not isinstance(data, (bytes, bytearray, memoryview)) or not 0 < len(data) <= limit:
        raise ScramblerFeedPullError("summary_size_invalid")
    return bytes(data)


def pull_summary_once(fetch, destination: Path, now_epoch: float | None = None) -> None:
    """Validate a private summary before atomically replacing the reader's file."""
    from services import originality_shadow
    now = time.time() if now_epoch is None else now_epoch
    manifest_raw = _summary_fetch(fetch, "manifest.json", SUMMARY_MANIFEST_MAX_BYTES)
    manifest = parse_manifest(manifest_raw)
    try:
        manifest_doc = json.loads(manifest_raw)
    except Exception as error:
        raise ScramblerFeedPullError("summary_manifest_invalid") from error
    data = _summary_fetch(fetch, SUMMARY_NAME, SUMMARY_MAX_BYTES)
    _verify_sha(SUMMARY_NAME, data, manifest)
    try:
        summary_doc = json.loads(data)
        gen = summary_doc["generated_at"]
        stamp = _parse_epoch(gen)
        if (not isinstance(gen, str) or gen != manifest_doc.get("usage_summary_generated_at")
                or stamp is None or manifest.generated_at_epoch is None
                or stamp > now or manifest.generated_at_epoch > now
                or stamp > manifest.generated_at_epoch
                or manifest.generated_at_epoch - stamp > 300
                or now - stamp > 26 * 3600):
            raise ValueError("summary timestamp")
        originality_shadow._parse_summary(data, now)
    except Exception as error:
        raise ScramblerFeedPullError("summary_invalid") from error
    destination = Path(destination)
    if not destination.is_absolute() or destination.is_symlink() or not destination.parent.is_dir():
        raise ScramblerFeedPullError("summary_destination_invalid")
    tmp_path = None
    lock_fd = None
    try:
        # Serialize cooperating pullers so an older generation cannot replace a
        # newer one after a slow fetch. Lock refusal leaves the last file intact.
        lock_path = destination.with_name("." + destination.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScramblerFeedPullError("summary_lock_busy") from None
        if destination.is_symlink():
            raise ScramblerFeedPullError("summary_destination_invalid")
        try:
            with destination.open("rb") as handle:
                existing = handle.read(SUMMARY_MAX_BYTES + 1)
            if len(existing) > SUMMARY_MAX_BYTES:
                raise ScramblerFeedPullError("summary_existing_oversize")
            old = json.loads(existing)
            old_stamp = _parse_epoch(old.get("generated_at")) if isinstance(old, dict) else None
            if old_stamp is not None and old_stamp > stamp:
                raise ScramblerFeedPullError("summary_generation_regressed")
        except (OSError, ValueError, UnicodeError):
            # An unreadable or corrupt cache cannot prove a newer generation.
            # The incoming object was fully verified before taking the lock.
            pass
        fd, raw_path = tempfile.mkstemp(prefix=".summary-", dir=str(destination.parent))
        tmp_path = Path(raw_path)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
        tmp_path = None
        _fsync_dir_quiet(destination.parent)
    except OSError as error:
        raise ScramblerFeedPullError("summary_write_failed") from error
    finally:
        if tmp_path is not None:
            _unlink_quiet(tmp_path)
        if lock_fd is not None:
            os.close(lock_fd)



def run_summary_pull_once() -> None:
    config = feed_config()
    raw_path = os.environ.get(SUMMARY_PATH_ENV, "").strip()
    if config is None or not raw_path:
        raise ScramblerFeedPullError("summary_unconfigured")
    bucket, prefix = config
    def fetch(name):
        from services import r2
        response = r2.client().get_object(Bucket=bucket, Key=feed_key(prefix, name))
        body = response["Body"]
        limit = SUMMARY_MANIFEST_MAX_BYTES if name == "manifest.json" else SUMMARY_MAX_BYTES
        try:
            return body.read(limit + 1)
        finally:
            body.close()
    pull_summary_once(fetch, Path(raw_path))


def maybe_pull_summary_once() -> None:
    if summary_enabled_now():
        run_summary_pull_once()


def maybe_pull_once() -> PullResult | None:
    """Run one pull only when SCRAMBLER_ENABLED=1; None (no I/O) when OFF."""
    if not scrambler_enabled_now():
        return None
    return run_feed_pull_once()


# ── scheduler (daemon thread, mirrors start_compaction_scheduler) ──────────
_FEED_PULL_STOP = threading.Event()
_FEED_PULL_THREAD: threading.Thread | None = None


def _feed_pull_loop() -> None:
    while True:
        if scrambler_enabled_now():
            try:
                run_feed_pull_once()
            except ScramblerFeedPullError as error:
                log.error(
                    "content_lab.scrambler-feed-pull.%s (old files kept)", error.reason
                )
            except Exception:  # a failed pull must never take the process down
                log.exception("content_lab.scrambler-feed-pull.unexpected (old files kept)")
        # OFF: no R2 call, no write — byte-identical to base.
        if summary_enabled_now():
            try:
                run_summary_pull_once()
            except Exception:
                # Never print provider exception, key, bucket, path or token.
                log.error("content_lab.originality-summary-pull.failed")
        if _FEED_PULL_STOP.wait(PULL_INTERVAL_SECONDS):
            break


def start_feed_pull_scheduler() -> None:
    global _FEED_PULL_THREAD
    if _FEED_PULL_THREAD is not None and _FEED_PULL_THREAD.is_alive():
        return
    _FEED_PULL_STOP.clear()
    _FEED_PULL_THREAD = threading.Thread(
        target=_feed_pull_loop, name="content-lab-scrambler-feed-pull", daemon=True,
    )
    _FEED_PULL_THREAD.start()


def stop_feed_pull_scheduler() -> None:
    _FEED_PULL_STOP.set()


__all__ = [
    "DATA_FILES", "FEED_FILES", "FEED_REL_PREFIX", "FEED_R2_BUCKET_ENV",
    "FEED_R2_PREFIX_ENV", "MANIFEST_STALE_AFTER_S", "PULL_INTERVAL_SECONDS",
    "WRITE_ORDER",
    "FeedManifest", "PullResult", "ScramblerFeedPullError", "count_usage_rows",
    "dest_dir", "feed_config", "feed_key", "maybe_pull_once", "parse_manifest",
    "pull_once", "run_feed_pull_once", "r2_fetch", "scrambler_enabled_now",
    "sha256_hex", "start_feed_pull_scheduler", "stop_feed_pull_scheduler",
]
