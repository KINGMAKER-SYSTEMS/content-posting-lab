"""Free the volume from finished generation / source-import media the Worker already holds.

A finished job's files used to stay on the volume forever, and generated clips
grew it by about 10 GB a day. Each clip the Worker delivers is copied into R2 as
``videos/<sha>.mp4``, and that R2 copy is the master. So for a terminal job:

* **A clip file** (``clips[*].path`` and that clip's thumbnail) is deleted once
  the Control Plane confirms it holds that exact clip: its
  ``/staging-video/<sha>.mp4`` door answers 200 with the clip's byte length.
  A clip the Worker has not copied is never deleted; it is re-checked after
  ``UNCONFIRMED_RECHECK_SECONDS``.
* **Every other file** in the job directory (provider intermediates, resume
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
UNCONFIRMED_RECHECK_SECONDS = 6 * 60 * 60
DEFAULT_JOB_LIMIT = 400
DEFAULT_PROBE_LIMIT = 600
DEFAULT_TIME_BUDGET_SECONDS = 120
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


def _job_dir(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return path if len(relative.parts) == JOB_DIR_DEPTH else None


def _inside(directory: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = (directory / relative).resolve()
    return path if directory in path.parents else None


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
        self._recheck_after: dict[str, float] = {}

    def plan(self, store: dict[str, Any], root: Path, now: datetime, *, pressure: str = "normal",
             job_limit: int = DEFAULT_JOB_LIMIT, probe_limit: int = DEFAULT_PROBE_LIMIT,
             time_budget: float = DEFAULT_TIME_BUDGET_SECONDS) -> Plan:
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
            directory = _job_dir(root, job.get("artifactRoot"))
            if (finished is None or now - finished < CONFIRMED_SETTLE or directory is None
                    or directory in protected or _holds_truck_pool(job) or not directory.is_dir()):
                continue
            candidates.append((finished, str(job_id), directory, job))
        candidates.sort(key=lambda item: (item[0], item[1]))

        plan = Plan()
        started = self.monotonic()
        stray_cutoff = now - STRAY_RETENTION[pressure]
        for finished, _job_id, directory, job in candidates:
            if plan.jobs >= job_limit or self.monotonic() - started > time_budget:
                break
            clip_paths: set[Path] = set()
            work = False
            for clip in job.get("clips", []) or []:
                if not isinstance(clip, dict):
                    continue
                files = [path for path in (
                    _inside(directory, clip.get("path")),
                    _inside(directory, (clip.get("thumbnail") or {}).get("path")
                            if isinstance(clip.get("thumbnail"), dict) else None),
                ) if path is not None]
                clip_paths.update(files)
                present = [path for path in files if path.is_file()]
                if not present:
                    continue
                sha = clip.get("sha256")
                if (plan.probes >= probe_limit or not isinstance(sha, str)
                        or self._recheck_after.get(sha, float("-inf")) > self.monotonic()):
                    plan.unconfirmed_bytes += sum(_size(path) for path in present)
                    continue
                plan.probes += 1
                if self.confirm(sha, clip.get("bytes")) is True:
                    plan.clip_files.extend(present)
                    self._recheck_after.pop(sha, None)
                    work = True
                else:
                    plan.unconfirmed_bytes += sum(_size(path) for path in present)
                    self._recheck_after[sha] = self.monotonic() + UNCONFIRMED_RECHECK_SECONDS
            if finished <= stray_cutoff:
                for current, _subdirs, names in os.walk(directory):
                    for name in names:
                        path = Path(current, name)
                        if path.resolve() not in clip_paths:
                            plan.stray_files.append(path)
                            work = True
            plan.jobs += work
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
