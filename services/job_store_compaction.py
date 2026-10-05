"""Bound the growing control_plane_jobs.json store by archiving old terminal jobs.

The plane persists every job forever. The whole file is re-decoded + re-encoded
under one global lock on every write, and (before #185) on every capabilities
poll, so unbounded growth is the root cause of the 499s. #185 fixes the READ
side; this module fixes the FILE side.

Design (see zcode/lab-jobstore-compaction-criteria.md, K1-K8):

* Only terminal jobs (completed/failed/cancelled) older than ``retention_days``
  are archived. ``retention_days`` (30) is the stated max of every K1 reader's
  minimum window plus margin: the Worker polls status then downloads artifacts
  soon after terminal (no long poll), and the §42 provider self-report needs
  >=8 days of attempt rows; 30 covers both with room.
* Archived jobs are appended, one JSON object per line, to an append-only
  ``control_plane_jobs.archive-<YYYYMM>.jsonl`` on the same volume. Complete
  records are never deleted or rewritten; a torn final frame is truncated
  before retrying its full record.
* Every permanent fact a live reader still needs from an archived job is
  folded into a compact durable index stored in the store itself under
  ``archiveIndex`` (version 1). The readers merge that index, so "full store"
  answers equal "compact store + index" answers exactly.
* Compaction runs INSIDE the Lab process under ``lock_for(_jobs_path())`` and
  writes the store through ``atomic_save`` (tmp + fsync + os.replace), so a
  crash leaves either the old or the new store valid. The archive append is
  idempotent: a re-run skips jobIds already present in the archive (the only
  way a duplicate can arise is a crash between the append and the store
  commit, and the retry re-archives the same ids).
* Each run is bounded (batch cap) so it can run incrementally instead of
  holding the writer lock for an unbounded time.

Orthogonality to #185: this module only writes the store via ``atomic_save``.
That ``os.replace`` changes inode/ctime/size, which is exactly the signature
``_read_jobs_snapshot`` keys on, so a post-compaction read re-decodes the
compacted file correctly. No other snapshot state is touched.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Shared with every live no-repeat reader. Because archived jobs are terminal,
# membership in this set is effectively completed-only during index folding.
SOURCE_DNA_UNAVAILABLE_STATUSES = frozenset({"queued", "running", "completed"})
ARCHIVE_INDEX_KEY = "archiveIndex"
ARCHIVE_INDEX_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
DEFAULT_BATCH_SIZE = 2000
DEFAULT_SIZE_THRESHOLD_BYTES = 50 * 1024 * 1024
# §42 (lab-provider-selfreport-criteria.md) needs at least 8 days of per-attempt
# rows. The default retention above comfortably exceeds this; the constant is
# kept here to document the floor the compaction must never drop below.
PROVIDER_ATTEMPT_RETENTION_DAYS = 8

# The truck recipe id whose completed generated clips remain live re-crop
# candidates and therefore need their full manifests preserved in the index
# (not just their dedupe sha256).
TRUCK_RECIPE_ID = "truck-scenic:master"

_SHA256_RE = __import__("re").compile(r"[0-9a-f]{64}")
log = logging.getLogger("job_store_compaction")


class ArchiveIndexUnreadable(RuntimeError):
    """Compaction cannot safely preserve the existing archive authority."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"index_unreadable {reason}")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def terminal_timestamp(job: dict[str, Any]) -> datetime | None:
    """The instant a job became terminal (completedAt, else createdAt)."""
    return _parse_iso(job.get("completedAt")) or _parse_iso(job.get("createdAt"))


def archivable_job_ids(store: dict[str, Any], cutoff: datetime) -> list[str]:
    """Terminal jobs strictly older than ``cutoff``, in insertion order."""
    jobs = store.get("jobs", {})
    out: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or job.get("status") not in TERMINAL_STATUSES:
            continue
        stamped = terminal_timestamp(job)
        if stamped is None or stamped >= cutoff:
            continue
        out.append(job_id)
    return out


def archive_path_for(store_path: Path, when: datetime) -> Path:
    """Append-only archive file for the month of ``when``, next to the store."""
    return store_path.with_name(f"{store_path.name}.archive-{when:%Y%m}.jsonl")


def empty_index() -> dict[str, Any]:
    return {
        "version": ARCHIVE_INDEX_VERSION,
        "sourceIdentities": {},       # pageId -> sorted list of canonical identities
        "sourceDnaCuts": [],          # completed dossier_source_dna cut projections
        "slideshow": [],              # {pageId, sourceLibraryId, signature}
        "truckRecoveryMasters": [],   # sha256 of recovered masters (permanent)
        "truckCandidateJobs": [],     # completed generated truck clips (re-crop)
        "usedClipSha256": {},         # pageId -> sorted list of clip sha256
        "providerHealth": {},         # key_id -> carried-forward streak/last-* state
    }


def _canonical_identity(value: Any) -> str | None:
    from services.control_plane_sources import canonical_source_identity
    return canonical_source_identity(value)


def _clip_sha256s(job: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for clip in job.get("clips", []):
        if not isinstance(clip, dict):
            continue
        digest = clip.get("sha256")
        if isinstance(digest, str) and _SHA256_RE.fullmatch(digest):
            out.append(digest)
    return out


def extract_index_entries(job: dict[str, Any]) -> dict[str, Any]:
    """The compact permanent facts one archived job contributes to the index."""
    entries: dict[str, Any] = {
        "sourceIdentities": [],       # (pageId, identity)
        "sourceDnaCuts": [],
        "slideshow": [],
        "truckRecoveryMasters": [],
        "truckCandidateJobs": [],
        "usedClipSha256": [],         # (pageId, sha256)
        "providerAttempts": [],       # (key_id, engine, row)
    }
    page_id = job.get("pageId")
    kind = job.get("sourceKind")

    if kind == "page_source_import" and job.get("status") == "completed":
        identity = _canonical_identity(job.get("sourceUrl"))
        if identity is not None and isinstance(page_id, str):
            entries["sourceIdentities"].append((page_id, identity))

    if kind == "dossier_source_dna" and job.get("status") == "completed":
        # usedAt/cutIndex let the cut planner vary re-cuts against the most
        # recent ones and prefer the least recently cut footage. Entries
        # archived before they existed simply count as the oldest.
        used_at = job.get("completedAt") or job.get("createdAt")
        for cut_index, cut in enumerate(job.get("sourceCuts", [])):
            if not isinstance(cut, dict):
                continue
            entries["sourceDnaCuts"].append({
                "sourceLibraryId": job.get("sourceLibraryId"),
                "sourceLibraryHash": job.get("sourceLibraryHash"),
                "recipeVersion": job.get("recipeVersion"),
                "slotId": cut.get("slotId"),
                "masterSha256": cut.get("masterSha256"),
                "startMs": cut.get("startMs"),
                "durationMs": cut.get("durationMs"),
                "usedAt": used_at if isinstance(used_at, str) else None,
                "cutIndex": cut_index,
            })

    if kind == "syzygy_slideshow" and job.get("status") == "completed":
        for plan in job.get("slideshowPlan", []):
            signature = plan.get("signature") if isinstance(plan, dict) else None
            if isinstance(signature, str) and _SHA256_RE.fullmatch(signature):
                entries["slideshow"].append({
                    "pageId": page_id,
                    "sourceLibraryId": job.get("sourceLibraryId"),
                    "signature": signature,
                })

    if kind == "truck_master_recovery" and job.get("status") == "completed":
        for master in job.get("recoveryMasters", []):
            sha256 = master.get("sha256") if isinstance(master, dict) else None
            if isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256):
                entries["truckRecoveryMasters"].append(sha256)

    # Use the live readers' exact status predicate. Archived jobs are terminal,
    # so only completed jobs can contribute permanent delivered-byte facts.
    if job.get("status") in SOURCE_DNA_UNAVAILABLE_STATUSES:
        for digest in _clip_sha256s(job):
            entries["usedClipSha256"].append((page_id, digest))

    # Completed generated truck renders remain live re-crop candidates; keep the
    # full clip manifest the reader needs. Non-truck renders keep only dedupe.
    if (
        kind == "generated"
        and job.get("status") == "completed"
        and job.get("recipeId") == TRUCK_RECIPE_ID
        and isinstance(job.get("artifactRoot"), str)
    ):
        clips = []
        for clip in job.get("clips", []):
            if not isinstance(clip, dict) or isinstance(clip.get("delivery"), dict):
                continue
            clips.append({
                "path": clip.get("path"),
                "sha256": clip.get("sha256"),
                "bytes": clip.get("bytes"),
                "source": clip.get("source"),
                "sourceTreatment": clip.get("sourceTreatment"),
            })
        if clips:
            entries["truckCandidateJobs"].append({
                "jobId": job.get("jobId"),
                "pageId": page_id,
                "sourceKind": "generated",
                "status": "completed",
                "engine": job.get("engine"),
                "recipeId": job.get("recipeId"),
                "engineRegistryHash": job.get("engineRegistryHash"),
                "engineProfileHash": job.get("engineProfileHash"),
                "formatContractVersion": job.get("formatContractVersion"),
                "executorVersion": job.get("executorVersion"),
                "promptCatalogHash": job.get("promptCatalogHash"),
                "family": job.get("family"),
                "providerModel": job.get("providerModel"),
                "materialSource": job.get("materialSource"),
                "assetType": job.get("assetType"),
                "artifactRoot": job.get("artifactRoot"),
                "createdAt": job.get("createdAt"),
                "recipeSpecHash": job.get("recipeSpecHash"),
                "clips": clips,
            })

    # Provider self-report attempt rows (first attempts + E005 retries + 402s).
    attempts = job.get("generationAttempts")
    if isinstance(attempts, dict):
        key_id = _key_id_for_engine(job.get("engine"))
        engine = job.get("engine")
        for call_key, rows in attempts.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    entries["providerAttempts"].append((key_id, engine, call_key, row))

    return entries


def _key_id_for_engine(engine: Any) -> str | None:
    if not isinstance(engine, str):
        return None
    try:
        from providers import PROVIDERS
    except Exception:
        return None
    return PROVIDERS.get(engine, {}).get("key_id")


def fold_into_index(index: dict[str, Any], job: dict[str, Any]) -> None:
    """Fold one archived job's compact facts into ``index`` (mutates it)."""
    e = extract_index_entries(job)
    page_id = job.get("pageId")

    for (pid, identity) in e["sourceIdentities"]:
        index["sourceIdentities"].setdefault(pid, [])
        if identity not in index["sourceIdentities"][pid]:
            index["sourceIdentities"][pid].append(identity)

    index["sourceDnaCuts"].extend(e["sourceDnaCuts"])
    index["slideshow"].extend(e["slideshow"])
    index["truckRecoveryMasters"].extend(e["truckRecoveryMasters"])
    index["truckCandidateJobs"].extend(e["truckCandidateJobs"])

    for (pid, digest) in e["usedClipSha256"]:
        if not isinstance(pid, str):
            continue
        index["usedClipSha256"].setdefault(pid, [])
        if digest not in index["usedClipSha256"][pid]:
            index["usedClipSha256"][pid].append(digest)

    for (key_id, engine, call_key, row) in e["providerAttempts"]:
        if key_id is None:
            continue
        fold_provider_attempt(index, key_id, engine, call_key, row)


# ── provider self-report carry-forward (per §42 + its Tides rulings) ─────
# The unit is a prediction-CREATE attempt, ordered by its recorded timestamp
# (ties: a stable (call_key, row index) id). Success = a prediction was
# created (outcome "succeeded"). A credit failure = class "insufficient_credit"
# (a 402). Non-credit failures neither increment nor reset the streak. Every
# E005 retry is its own attempt. Pending rows never reached the provider.
# The time-dependent fields (paid_calls_today / paid_calls_7d_by_engine) are
# always derived from the live retained attempts (>=30d >> the 7d window) and
# are NOT carried here; only the streak/last-* state that can span the archive
# boundary is carried forward.

_EXECUTED_OUTCOMES = frozenset({"succeeded", "refused", "failed"})


def _provider_state() -> dict[str, Any]:
    return {
        "last_success_at": None,
        "last_failure_class": None,
        "last_failure_at": None,
        "consecutive_credit_failures": 0,
        "credit_streak_started_at": None,
        "last_attempt_at": None,
    }


def _attempt_rows_sorted(records: Iterable[tuple]) -> list[tuple]:
    """Sort (key_id, engine, call_key, row) records into stable time order."""
    rows = []
    for (key_id, engine, call_key, row) in records:
        at = row.get("at")
        if not isinstance(at, str):
            continue
        if row.get("outcome") not in _EXECUTED_OUTCOMES:
            continue
        rows.append((at, call_key, key_id, engine, row))
    rows.sort(key=lambda item: (item[0], str(item[1])))
    return rows


def fold_provider_attempt(
    index: dict[str, Any],
    key_id: str,
    engine: Any,
    call_key: Any,
    row: dict[str, Any],
) -> None:
    state = index["providerHealth"].setdefault(key_id, _provider_state())
    _apply_attempt(state, row)


def _apply_attempt(state: dict[str, Any], row: dict[str, Any]) -> None:
    outcome = row.get("outcome")
    at = row.get("at")
    if outcome not in _EXECUTED_OUTCOMES or not isinstance(at, str):
        return
    state["last_attempt_at"] = _max_iso(state["last_attempt_at"], at)
    if outcome == "succeeded":
        state["last_success_at"] = _max_iso(state["last_success_at"], at)
        state["consecutive_credit_failures"] = 0
        state["credit_streak_started_at"] = None
        return
    klass = row.get("class")
    if klass == "insufficient_credit":
        state["last_failure_class"] = "insufficient_credit"
        state["last_failure_at"] = _max_iso(state["last_failure_at"], at)
        if state["consecutive_credit_failures"] == 0:
            state["credit_streak_started_at"] = at
        state["consecutive_credit_failures"] += 1
    elif isinstance(klass, str) and klass:
        # Non-credit failure: record last failure, leave the streak alone.
        state["last_failure_class"] = klass
        state["last_failure_at"] = _max_iso(state["last_failure_at"], at)


def _max_iso(a: Any, b: str) -> str:
    if a is None:
        return b
    return b if b > a else a


def provider_health_from_records(
    records: Iterable[tuple],
) -> dict[str, dict[str, Any]]:
    """Derive per-key_id provider-health state from scratch (pure)."""
    index = {"providerHealth": {}}
    for (_at, _call_key, key_id, engine, row) in _attempt_rows_sorted(records):
        fold_provider_attempt(index, key_id, engine, _call_key, row)
    return index["providerHealth"]


def provider_health_merge(
    carried: dict[str, dict[str, Any]],
    records: Iterable[tuple],
) -> dict[str, dict[str, Any]]:
    """Continue a carried-forward aggregate over fresh (live) attempt rows."""
    merged = {key: dict(state) for key, state in carried.items()}
    index = {"providerHealth": merged}
    for (_at, _call_key, key_id, engine, row) in _attempt_rows_sorted(records):
        if key_id is None:
            continue
        fold_provider_attempt(index, key_id, engine, _call_key, row)
    return merged


def provider_records_from_jobs(jobs: Iterable[dict[str, Any]]) -> list[tuple]:
    records: list[tuple] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        attempts = job.get("generationAttempts")
        if not isinstance(attempts, dict):
            continue
        key_id = _key_id_for_engine(job.get("engine"))
        for call_key, rows in attempts.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    records.append((key_id, job.get("engine"), call_key, row))
    return records


# ── the compaction transform (pure; no IO) ───────────────────────────────

def compact_job_store(
    store: dict[str, Any],
    now: datetime,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (new_store, archived_jobs). Pure; the caller persists both.

    ``new_store`` shares no mutable containers with ``store``: it is a shallow
    copy with fresh ``jobs`` / ``byIdempotency`` / ``archiveIndex``. Archived
    jobs are the original dicts moved out of ``jobs``.
    """
    cutoff = now - timedelta(days=retention_days)
    archived_ids = archivable_job_ids(store, cutoff)[:batch_size]

    jobs = dict(store.get("jobs", {}))
    by_idem = dict(store.get("byIdempotency", {}))
    if ARCHIVE_INDEX_KEY not in store:
        index = empty_index()
    else:
        current_index = store[ARCHIVE_INDEX_KEY]
        if not isinstance(current_index, dict):
            raise ArchiveIndexUnreadable(
                f"not_a_dict type={type(current_index).__name__}",
            )
        if current_index.get("version") != ARCHIVE_INDEX_VERSION:
            # A future version is a migration, never an empty-index reset.
            raise ArchiveIndexUnreadable(
                f"version={current_index.get('version')!r}",
            )
        index = json.loads(json.dumps(current_index))

    archived: list[dict[str, Any]] = []
    for job_id in archived_ids:
        job = jobs[job_id]
        archived.append(job)
        fold_into_index(index, job)
        del jobs[job_id]

    # Normalize lists for deterministic on-disk shape.
    for page in index["sourceIdentities"]:
        index["sourceIdentities"][page] = sorted(set(index["sourceIdentities"][page]))
    index["truckRecoveryMasters"] = sorted(set(index["truckRecoveryMasters"]))
    for page in index["usedClipSha256"]:
        index["usedClipSha256"][page] = sorted(set(index["usedClipSha256"][page]))

    new_store = {
        **{key: value for key, value in store.items() if key not in {"jobs", "byIdempotency", ARCHIVE_INDEX_KEY}},
        "jobs": jobs,
        "byIdempotency": by_idem,
        ARCHIVE_INDEX_KEY: index,
    }
    return new_store, archived


def archive_lines_for(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )


def existing_archive_job_ids(path: Path) -> set[str]:
    """Job ids in the complete-line archive view prepared by the appender."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("jobId"), str):
                    ids.add(record["jobId"])
    except OSError:
        return set()
    return ids


def _repair_partial_archive_tail(path: Path) -> int:
    """Truncate an incomplete final JSONL frame and return removed bytes."""
    if not path.exists():
        return 0
    with open(path, "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        if end == 0:
            return 0
        handle.seek(end - 1)
        if handle.read(1) == b"\n":
            return 0

        cursor = end
        last_newline = -1
        while cursor:
            block_size = min(64 * 1024, cursor)
            cursor -= block_size
            handle.seek(cursor)
            block = handle.read(block_size)
            offset = block.rfind(b"\n")
            if offset >= 0:
                last_newline = cursor + offset
                break
        repaired_size = last_newline + 1
        removed = end - repaired_size
        handle.truncate(repaired_size)
        handle.flush()
        os.fsync(handle.fileno())
    log.warning("repaired partial archive tail bytes=%d path=%s", removed, path)
    return removed


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def append_archive_records(
    store_path: Path,
    records: list[dict[str, Any]],
    when: datetime,
) -> int:
    """Idempotently append archived jobs to the month archive. Returns count.

    Idempotent: records whose ``jobId`` is already in the archive are skipped.
    The append is fsynced so a crash cannot leave the records half-durable.
    """
    if not records:
        return 0
    archive = archive_path_for(store_path, when)
    archive.parent.mkdir(parents=True, exist_ok=True)
    created = not archive.exists()
    _repair_partial_archive_tail(archive)
    known = existing_archive_job_ids(archive)
    to_write = [record for record in records if record.get("jobId") not in known]
    if not to_write:
        return 0
    payload = archive_lines_for(to_write)
    with open(archive, "a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_directory(archive.parent)
    return len(to_write)
