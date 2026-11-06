"""Free the volume from finished generation / source-import media the Worker already holds.

A finished job's files used to stay on the volume forever, and generated clips
grew it by about 10 GB a day. Each clip the Worker delivers is copied into R2 as
``videos/<sha>.mp4``, and that R2 copy is the master. So for a terminal job:

* **A clip file** (``clips[*].path`` and that clip's thumbnail) is deleted once
  the Control Plane confirms it holds that exact clip: its
  ``/staging-video/<sha>.mp4`` door answers 200 with the clip's byte length.
  A clip the Worker has not copied is never deleted. A definitive "not
  confirmed" answer (404, or a 200 whose length differs) backs the sha off for
  1 h, then 2 h, 4 h ... up to 24 h; an errored probe (unknown answer) retries
  on the next pass and backs off only if it keeps failing; a clip record that
  can never be probed (malformed sha or byte count) is held for 24 h. A pass
  probes the NEWEST jobs' clips first (4/5 of its probe cap) plus the least
  recently probed old ones (1/5, at least 1), so no number of permanent
  failures can starve newer clips and old admitted clips still free over time.
* **Private Scrambler checkpoints** retain their produced clip and thumbnail
  paths. A matching public clip still uses the R2-confirmed cleanup above;
  malformed or conflicting private metadata holds the whole job.
* **Every other unreferenced file** in the job directory (provider intermediates, resume
  renders, masters, unreferenced thumbnails) is a stray and is deleted
  ``STRAY_RETENTION`` after the job finished: 48 h, or sooner under volume
  pressure (see ``services.post_render_jobs.volume_pressure``).

What is never touched:

* the job record in ``control_plane_jobs.json`` -- every clip sha256 there is
  no-repeat authority, and this module only reads the store;
* a job that is not terminal (queued, running, or anything else), and any
  directory a non-terminal job points at, including truck-recovery masters;
* a completed generated job that still holds undelivered TRUCK clips (the
  re-crop pool ``_truck_master_candidates`` reads);
* anything outside a job directory exactly three levels below the root.

Each run is bounded by a job count, a confirmation budget and a time budget,
and logs the bytes it freed.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from services.job_store_compaction import TERMINAL_STATUSES, terminal_timestamp

log = logging.getLogger("content_lab.generated_media_retention")

STRAY_RETENTION = {"normal": timedelta(hours=48), "tight": timedelta(hours=24), "floor": timedelta(hours=6)}
# A confirmed clip is already in R2; this only lets a just-finished job settle.
CONFIRMED_SETTLE = timedelta(hours=1)
DEFAULT_JOB_LIMIT = 400
DEFAULT_TIME_BUDGET_SECONDS = 120
# How many clips one pass may probe at each volume pressure (the caller's own
# probe_limit still wins). A near-full volume must not turn every 60 s pass
# into ~600 control-plane D1 reads (2026-11-04 probe storm).
PROBE_LIMITS = {"normal": 100, "tight": 60, "floor": 30}
# A definitive "not confirmed" answer (a 404, or a 200 whose length differs)
# means the clip is not in R2 yet: the sha is not re-probed for an hour, then
# the back-off doubles on each repeat, up to a day.
NEGATIVE_CACHE_SECONDS = 60 * 60
NEGATIVE_CACHE_MAX_SECONDS = 24 * 60 * 60
# An errored probe (None) is retried at once the first time, then backs off
# 10 min, 20 min ... up to an hour while it keeps failing.
TRANSIENT_BACKOFF_SECONDS = 10 * 60
TRANSIENT_BACKOFF_MAX_SECONDS = 60 * 60
# A malformed sha / byte count can never be probed: hold it for a day.
MALFORMED_BACKOFF_SECONDS = 24 * 60 * 60
# Only a safety net: entries are dropped as soon as their clip file is gone,
# and the newest-first order never reads the cache, so eviction cannot starve anyone.
NEGATIVE_CACHE_MAX_ENTRIES = 200_000
# <page>/<group>/<job>: a job directory is exactly three levels below the root.
JOB_DIR_DEPTH = 3


def confirm_admitted_clip(sha256: Any, byte_count: Any, *,
                          transport: httpx.BaseTransport | None = None) -> bool | None:
    """True only when the Control Plane serves exactly this clip from R2; None when unknown.

    The door streams the whole object, so the body is never read: the status and
    Content-Length are enough, and the connection closes right after the headers.
    """
    origin = os.getenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "").strip().rstrip("/")
    client_id = os.getenv("CONTROL_PLANE_SERVICE_ID", "")
    client_secret = os.getenv("CONTROL_PLANE_SERVICE_SECRET", "")
    url = urlsplit(origin)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment or not client_id or not client_secret):
        return None
    if (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(byte_count, int) or isinstance(byte_count, bool)):
        return None
    headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret,
               "Accept-Encoding": "identity"}
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(10, connect=5), headers=headers) as client:
            with client.stream("GET", f"{origin}/staging-video/{sha256}.mp4") as response:
                status, length = response.status_code, response.headers.get("content-length", "")
    except httpx.HTTPError:
        return None
    if status == 404:
        return False
    if status != 200:
        return None
    return length.isdigit() and int(length) == byte_count


def _identity_is_probeable(sha256: Any, byte_count: Any) -> bool:
    return (isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
            and isinstance(byte_count, int) and not isinstance(byte_count, bool))


def _job_dir(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return path if len(relative.parts) == JOB_DIR_DEPTH else None


def _source_import_attempt_dirs(root: Path, job_id: str, job: dict[str, Any]) -> tuple[Path, Path] | None:
    """Return job group and current attempt only for the exact private layout."""
    attempt = job.get("sourceImportAttemptId")
    version = job.get("sourceImportClaimVersion")
    value = job.get("artifactRoot")
    if (job.get("sourceKind") != "page_source_import" or job.get("jobId") != job_id
            or type(version) is not int or version != 1
            or not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt)
            or not isinstance(value, str) or not value):
        return None
    path = Path(value).resolve()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    if (len(parts) != JOB_DIR_DEPTH + 1 or parts[0] != job.get("pageId")
            or parts[1] != "source-imports" or parts[2] != job_id or parts[3] != attempt
            or not re.fullmatch(r"cpl-[0-9a-f]{16}", job_id)):
        return None
    return path.parent, path


def _completed_flat_import_dir(root: Path, job_id: str, job: dict[str, Any]) -> Path | None:
    """Recognize completed phase-A ownership without broad private fallback."""
    page, value = job.get("pageId"), job.get("artifactRoot")
    if (job.get("sourceKind") != "page_source_import" or job.get("status") != "completed"
            or job.get("jobId") != job_id or not re.fullmatch(r"cpl-[0-9a-f]{16}", job_id)
            or not isinstance(page, str) or page in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", page)
            or type(job.get("sourceImportClaimVersion")) is not int or job["sourceImportClaimVersion"] != 1
            or not isinstance(job.get("sourceImportAttemptId"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", job["sourceImportAttemptId"])
            or not isinstance(job.get("runtimeId"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["runtimeId"])
            or not isinstance(value, str) or not Path(value).is_absolute()):
        return None
    expected = root / page / "source-imports" / job_id
    if (expected.relative_to(root).parts != (page, "source-imports", job_id)
            or Path(value) != expected or expected.resolve() != expected):
        return None
    current = root
    for part in expected.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            return None
    return expected


def _flat_import_file(directory: Path, relative: Any) -> Path | None:
    """Only direct source/thumbnail files; never a private attempt or alias."""
    if not isinstance(relative, str):
        return None
    parts = Path(relative).parts
    if len(parts) != 2 or parts[0] not in {"source", "thumbnails"}:
        return None
    path = directory / relative
    if path.parent.is_symlink() or path.is_symlink() or path.resolve().parent != directory / parts[0]:
        return None
    return path


def _inside(directory: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = (directory / relative).resolve()
    return path if directory in path.parents else None


def _private_scrambler_paths(root: Path, directory: Path, job_id: str,
                            job: dict[str, Any]) -> set[Path] | None:
    """Protect produced private clips; None holds an ambiguous whole job."""
    clips = job.get("scramblerRenderedClips", [])
    if not isinstance(clips, list):
        return None
    if not clips:
        return set()
    parts = directory.relative_to(root).parts
    if (job.get("sourceKind") != "dossier_source_dna" or job.get("jobId") != job_id
            or len(parts) != JOB_DIR_DEPTH or parts[0] != job.get("pageId") or parts[2] != job_id):
        return None
    identities: dict[Path, tuple[str, int]] = {}

    def protect(manifest: Any) -> None:
        if not isinstance(manifest, dict):
            raise ValueError("private manifest invalid")
        relative, digest, size = (manifest.get(key) for key in ("path", "sha256", "bytes"))
        if (not isinstance(relative, str) or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size <= 0):
            raise ValueError("private manifest identity invalid")
        path = _inside(directory, relative)
        if path is None:
            raise ValueError("private path outside job")
        current = directory
        for part in Path(relative).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("private path alias")
        try:
            present = path.lstat()
        except FileNotFoundError:
            pass  # A matching public clip may already have been cleaned from R2 proof.
        else:
            if not stat.S_ISREG(present.st_mode) or present.st_size != size:
                raise ValueError("private file identity conflict")
        identity = (digest, size)
        if path in identities and identities[path] != identity:
            raise ValueError("conflicting private identity")
        identities[path] = identity

    try:
        for clip in clips:
            protect(clip)
            if "thumbnail" in clip:
                protect(clip["thumbnail"])
        # A matching public manifest may still use the existing R2-confirmed
        # cleanup path. Conflicting claims must never choose which asset wins.
        public_clips = job.get("clips")
        if not isinstance(public_clips, list):
            raise ValueError("public checkpoint shape invalid")
        for clip in public_clips:
            if not isinstance(clip, dict):
                raise ValueError("public checkpoint shape invalid")
            for manifest in (clip, clip.get("thumbnail")):
                if not isinstance(manifest, dict):
                    continue
                path = _inside(directory, manifest.get("path"))
                if path in identities and (type(manifest.get("bytes")) is not int
                        or identities[path] != (manifest.get("sha256"), manifest.get("bytes"))):
                    raise ValueError("conflicting public/private identity")
    except Exception:  # An unenumerable checkpoint must never enable deletion.
        return None
    return set(identities)


def _holds_truck_pool(job: dict[str, Any]) -> bool:
    if job.get("sourceKind") != "generated" or job.get("status") != "completed":
        return False
    for clip in job.get("clips", []) or []:
        if not isinstance(clip, dict) or isinstance(clip.get("delivery"), dict):
            continue
        source = clip.get("source") if isinstance(clip.get("source"), dict) else {}
        if str(source.get("contentNiche") or "").strip().upper() == "TRUCK":
            return True
    return False


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


@dataclass
class Plan:
    """What one pass would delete. Built without touching anything."""
    clip_files: list[Path] = field(default_factory=list)      # confirmed in R2
    stray_files: list[Path] = field(default_factory=list)     # not a clip, job finished long enough ago
    unconfirmed_bytes: int = 0                                # clip bytes kept: R2 does not hold them (yet)
    jobs: int = 0
    probes: int = 0

    def bytes(self, kind: str) -> int:
        return sum(_size(path) for path in getattr(self, kind))


class GeneratedMediaRetention:
    def __init__(self, *, confirm: Callable[[Any, Any], bool | None] = confirm_admitted_clip,
                 monotonic: Callable[[], float] = time.monotonic):
        self.confirm = confirm
        self.monotonic = monotonic
        # sha -> monotonic time before which it is not probed again, and
        # sha -> (last probe time, consecutive failures, failure kind). Both are
        # touched by the scheduler thread and the remedy pass: hold the lock.
        self._negative_cache: dict[str, float] = {}
        self._probe_meta: dict[str, tuple[float, int, str]] = {}
        self._lock = threading.Lock()

    def _backed_off(self, key: str) -> bool:
        with self._lock:
            return self._negative_cache.get(key, float("-inf")) > self.monotonic()

    def _record_failure(self, key: str, kind: str, *, probed: bool = True) -> None:
        now = self.monotonic()
        with self._lock:
            last, fails, old_kind = self._probe_meta.get(key, (float("-inf"), 0, kind))
            fails = min(fails + 1, 30) if old_kind == kind else 1
            if kind == "missing":
                delay = min(NEGATIVE_CACHE_SECONDS * 2 ** (fails - 1), NEGATIVE_CACHE_MAX_SECONDS)
            elif kind == "error":
                delay = 0 if fails == 1 else min(
                    TRANSIENT_BACKOFF_SECONDS * 2 ** (fails - 2), TRANSIENT_BACKOFF_MAX_SECONDS)
            else:
                delay = MALFORMED_BACKOFF_SECONDS
            self._negative_cache[key] = now + delay
            self._probe_meta[key] = (now if probed else last, fails, kind)
            while len(self._negative_cache) > NEGATIVE_CACHE_MAX_ENTRIES:
                # Evict the entry that expires first, not the one inserted first.
                victim = min(self._negative_cache, key=self._negative_cache.__getitem__)
                del self._negative_cache[victim]
                self._probe_meta.pop(victim, None)

    def _prune(self, live: set[str]) -> None:
        with self._lock:
            for key in [key for key in self._negative_cache if key not in live]:
                del self._negative_cache[key]
            for key in [key for key in self._probe_meta if key not in live]:
                del self._probe_meta[key]

    def _forget(self, key: str) -> None:
        with self._lock:
            self._negative_cache.pop(key, None)
            self._probe_meta.pop(key, None)

    def plan(self, store: dict[str, Any], root: Path, now: datetime, *, pressure: str = "normal",
             job_limit: int = DEFAULT_JOB_LIMIT, probe_limit: int | None = None,
             time_budget: float = DEFAULT_TIME_BUDGET_SECONDS) -> Plan:
        if probe_limit is None:
            probe_limit = PROBE_LIMITS[pressure]
        root = root.resolve()
        jobs = store.get("jobs", {}) if isinstance(store, dict) else {}
        protected: set[Path] = set()
        for job in jobs.values():
            if not isinstance(job, dict) or job.get("status") in TERMINAL_STATUSES:
                continue
            for value in [job.get("artifactRoot")] + [
                master.get("artifactRoot") for master in job.get("recoveryMasters", []) or []
                if isinstance(master, dict)
            ]:
                if isinstance(value, str) and value:
                    protected.add(Path(value).resolve())
        candidates = []
        for job_id, job in jobs.items():
            if not isinstance(job, dict) or job.get("status") not in TERMINAL_STATUSES:
                continue
            finished = terminal_timestamp(job)
            flat_import = False
            if "sourceImportClaimVersion" in job or "sourceImportAttemptId" in job:
                # New-layout metadata must prove the exact attempt path; a
                # malformed marker cannot fall back to a broader job root.
                attempt_dirs = _source_import_attempt_dirs(root, str(job_id), job)
                directory, artifact_directory = attempt_dirs if attempt_dirs is not None else (None, None)
                if attempt_dirs is None:
                    directory = artifact_directory = _completed_flat_import_dir(root, str(job_id), job)
                    flat_import = directory is not None
                # A failed snapshot can be retried after it was read.  Never
                # scan sibling attempts from that snapshot: a sibling may be
                # the live retry, even if it appears while planning a sweep.
                if (directory is not None and job.get("status") == "failed"
                        and job.get("error") == "source_import_runtime_restarted"):
                    directory = artifact_directory
            else:
                directory = _job_dir(root, job.get("artifactRoot"))
                artifact_directory = directory
            if (finished is None or now - finished < CONFIRMED_SETTLE or directory is None
                    or any(path == directory or directory in path.parents for path in protected)
                    or _holds_truck_pool(job) or not directory.is_dir()):
                continue
            candidates.append((finished, str(job_id), directory, artifact_directory, job, flat_import))
        candidates.sort(key=lambda item: (item[0], item[1]))

        plan = Plan()
        pending: list[tuple[int, Any, Any, list[Path], int, datetime]] = []
        owners: list[bool] = []
        started = self.monotonic()
        stray_cutoff = now - STRAY_RETENTION[pressure]
        stopped = False
        for finished, _job_id, directory, artifact_directory, job, flat_import in candidates:
            if plan.jobs >= job_limit or self.monotonic() - started > time_budget:
                stopped = True
                break
            private_paths = _private_scrambler_paths(root, artifact_directory, _job_id, job)
            if private_paths is None:
                continue
            clip_paths: set[Path] = private_paths
            inside = _flat_import_file if flat_import else _inside
            work = False
            for clip in job.get("clips", []) or []:
                if not isinstance(clip, dict):
                    continue
                files = [path for path in (
                    inside(artifact_directory, clip.get("path")),
                    inside(artifact_directory, (clip.get("thumbnail") or {}).get("path")
                            if isinstance(clip.get("thumbnail"), dict) else None),
                ) if path is not None]
                clip_paths.update(files)
                present = [path for path in files if path.is_file()]
                if not present:
                    continue
                pending.append((len(pending), clip.get("sha256"), clip.get("bytes"), present, len(owners), finished))
            owners.append(False)
            if finished <= stray_cutoff:
                if flat_import:
                    # A flat group can gain a private retry at any point. Never
                    # walk that group (or recurse under its known media dirs).
                    paths = []
                    for namespace in ("source", "thumbnails"):
                        media_dir = directory / namespace
                        if media_dir.is_symlink() or not media_dir.is_dir():
                            continue
                        for child in media_dir.iterdir():
                            path = _flat_import_file(directory, f"{namespace}/{child.name}")
                            if path is not None and path.is_file():
                                paths.append(path)
                else:
                    paths = (Path(current, name) for current, _subdirs, names in os.walk(directory)
                             for name in names)
                for path in paths:
                    if path.resolve() not in clip_paths:
                        plan.stray_files.append(path)
                        work = True
            plan.jobs += work
            owners[-1] = work
        walked_all = not stopped
        # Classify every present clip. Malformed records and backed-off shas
        # spend no probe; the rest are eligible.
        seen: set[str] = set()
        eligible: list[tuple[int, str, int, list[Path], int, datetime]] = []
        for index, sha, size, present, owner, finished in pending:
            kept = sum(_size(path) for path in present)
            if not isinstance(sha, str):
                plan.unconfirmed_bytes += kept
                continue
            if not _identity_is_probeable(sha, size):
                key = f"!{sha}|{size!r}"
                seen.add(key)
                if not self._backed_off(key):  # no probe is spent on it; the hold is bookkeeping
                    self._record_failure(key, "malformed", probed=False)
                plan.unconfirmed_bytes += kept
                continue
            seen.add(sha)
            if self._backed_off(sha):
                plan.unconfirmed_bytes += kept
                continue
            eligible.append((index, sha, size, present, owner, finished))
        if walked_all:
            # A clip whose file is gone no longer needs a back-off entry.
            self._prune(seen)
        # Which clips to probe this pass. Most of the cap goes to the NEWEST
        # jobs first: the freshest clips are the likeliest to be in R2 and the
        # most valuable to free, and old permanent 404s cannot crowd them out
        # however many there are or whether this process just restarted. A
        # small share goes to the least recently probed OLD clips, so old
        # admitted clips also free over time. The newest share never reads the
        # cache, so evicting a cache entry cannot change who is probed first.
        with self._lock:
            last_probe = {item[1]: self._probe_meta[item[1]][0] for item in eligible if item[1] in self._probe_meta}
        old_share = max(1, probe_limit // 5) if probe_limit >= 2 else 0
        by_newest = sorted(eligible, key=lambda item: (-item[5].timestamp(), item[0]))
        chosen = by_newest[:max(probe_limit - old_share, 0)]
        taken = {item[0] for item in chosen}
        rest = sorted((item for item in eligible if item[0] not in taken),
                      key=lambda item: (last_probe.get(item[1], float("-inf")), item[5].timestamp(), item[0]))
        chosen += rest[:max(probe_limit - len(chosen), 0)]
        taken = {item[0] for item in chosen}
        for item in eligible:
            if item[0] not in taken:
                plan.unconfirmed_bytes += sum(_size(path) for path in item[3])
        for position, (_index, sha, size, present, owner, _finished) in enumerate(chosen):
            if self.monotonic() - started > time_budget:
                # Out of time: the rest wait for the next pass.
                for later in chosen[position:]:
                    plan.unconfirmed_bytes += sum(_size(path) for path in later[3])
                break
            if plan.jobs >= job_limit and not owners[owner]:
                plan.unconfirmed_bytes += sum(_size(path) for path in present)
                continue
            plan.probes += 1
            answer = self.confirm(sha, size)
            if answer is True:
                plan.clip_files.extend(present)
                self._forget(sha)
                if not owners[owner]:
                    owners[owner] = True
                    plan.jobs += 1
            else:
                plan.unconfirmed_bytes += sum(_size(path) for path in present)
                # False: not confirmed (404 or wrong length); None: unknown. Both back off.
                self._record_failure(sha, "missing" if answer is False else "error")
        return plan

    def sweep(self, store: dict[str, Any], root: Path, now: datetime, *, pressure: str = "normal",
              **limits: Any) -> dict[str, int]:
        """Delete what ``plan`` selects, prune emptied job directories, and log the bytes freed."""
        root = root.resolve()
        plan = self.plan(store, root, now, pressure=pressure, **limits)
        summary = {"clip_files": 0, "stray_files": 0, "failed": 0, "bytes_freed": 0,
                   "unconfirmed_bytes_kept": plan.unconfirmed_bytes}
        touched: set[Path] = set()
        for kind, paths in (("clip_files", plan.clip_files), ("stray_files", plan.stray_files)):
            for path in paths:
                size = _size(path)
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    summary["failed"] += 1
                    log.exception("generated media retention could not delete %s", path)
                    continue
                summary[kind] += 1
                summary["bytes_freed"] += size
                touched.add(path.parent)
        for directory in sorted(touched, key=lambda path: len(path.parts), reverse=True):
            current = directory
            while len(current.relative_to(root).parts) >= JOB_DIR_DEPTH:
                try:
                    current.rmdir()  # only ever removes an EMPTY directory
                except OSError:
                    break
                current = current.parent
        try:
            usage = shutil.disk_usage(root)
            volume = f" used_bytes={usage.used} free_bytes={usage.free} total_bytes={usage.total}"
        except OSError:
            volume = " volume=unavailable"
        log.info("generated media retention pressure=%s clip_files=%d stray_files=%d failed=%d bytes_freed=%d "
                 "unconfirmed_bytes_kept=%d probes=%d jobs=%d%s", pressure, summary["clip_files"],
                 summary["stray_files"], summary["failed"], summary["bytes_freed"], plan.unconfirmed_bytes,
                 plan.probes, plan.jobs, volume)
        return summary
