"""Free the volume from the files of jobs that have left the job store.

``generated_media_retention`` only reads the live store. Compaction moves a
terminal job out of that store 30 days after it finished, and from then on
nothing looked at its directory again: on 2026-11-21 that was 70 GB of the
158 GB under ``control_plane_generated``, every byte of it older than 30 days.

Those files are unreachable, not merely old. ``/v1/jobs/{id}``, ``/artifacts``
and ``/download`` answer 404 for a job that is not in the live store, so the
Worker can never fetch them again; the archive index keeps every permanent
fact a reader still needs (clip sha256s, source identities, cut ledgers).

What one pass deletes: the files under the ``artifactRoot`` of a job that

* has a complete record in a ``control_plane_jobs.json.archive-*.jsonl`` file,
* is not in the live store, and
* whose directory is the job's own: named after the job id, inside the
  generation root, at the job depth (or one attempt below a source import).

What is never touched:

* a directory any live job points at (its own root or a recovery master), or
  that contains or sits inside one;
* a directory the archive index lists as a truck re-crop candidate
  (``truckCandidateJobs``), because ``_truck_master_candidates`` still reads
  those bytes;
* everything, when the store or its archive index cannot be read;
* anything that is not a regular file, and the job records themselves.

Each pass is bounded by a directory count and a time budget and logs what it
freed. ``dry_run=True`` counts without deleting.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable

from services.job_store_compaction import ArchiveIndexUnreadable, read_archive_index

log = logging.getLogger("content_lab.archived_media_retention")

DEFAULT_DIR_LIMIT = 400
DEFAULT_TIME_BUDGET_SECONDS = 60
JOB_DIR_DEPTH = 3
_JOB_ID = re.compile(r"cpl-[0-9a-f]{16}")


def _job_directory(root: Path, job_id: str, value: Any) -> Path | None:
    """The archived job's own directory, or None when the record cannot prove it."""
    if not isinstance(value, str) or not value or not _JOB_ID.fullmatch(job_id):
        return None
    path = Path(value)
    try:
        if not path.is_absolute() or path.resolve() != path:
            return None  # relative, or reached through a symlink
        parts = path.relative_to(root).parts
    except (OSError, RuntimeError, ValueError):
        return None
    if len(parts) == JOB_DIR_DEPTH and parts[2] == job_id:
        return path
    if len(parts) == JOB_DIR_DEPTH + 1 and parts[1] == "source-imports" and parts[2] == job_id:
        return path
    return None


def _related(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


class ArchivedMediaRetention:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic):
        self.monotonic = monotonic
        self._offsets: dict[Path, int] = {}   # archive file -> bytes already read
        self._roots: dict[str, Any] = {}      # archived job id -> its artifactRoot
        self._gone: set[str] = set()          # archived job ids with nothing left on disk

    def _read_archives(self, store_path: Path, expired: Callable[[], bool]) -> bool:
        """Read complete archive lines added since the last pass. False = ran out of time."""
        for path in sorted(store_path.parent.glob(f"{store_path.name}.archive-*.jsonl")):
            offset = self._offsets.get(path, 0)
            try:
                with path.open("rb") as handle:
                    if handle.seek(0, os.SEEK_END) < offset:
                        offset = 0  # the file was rewritten: read it again
                    handle.seek(offset)
                    while True:
                        if expired():
                            return False
                        line = handle.readline()
                        if not line.endswith(b"\n"):
                            break  # end of file, or a frame that is still being written
                        offset = handle.tell()
                        self._offsets[path] = offset
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(record, dict) and isinstance(record.get("jobId"), str):
                            self._roots[record["jobId"]] = record.get("artifactRoot")
            except OSError:
                continue
        return True

    def sweep(self, store: Any, root: Path, store_path: Path, *, dir_limit: int = DEFAULT_DIR_LIMIT,
              time_budget: float = DEFAULT_TIME_BUDGET_SECONDS, dry_run: bool = False) -> dict[str, int]:
        summary = {"dirs": 0, "files": 0, "bytes_freed": 0, "failed": 0, "held": 0}
        deadline = self.monotonic() + time_budget
        expired = lambda: self.monotonic() >= deadline
        jobs = store.get("jobs") if isinstance(store, dict) else None
        if not isinstance(jobs, dict):
            return summary
        try:
            index = read_archive_index(store)
        except ArchiveIndexUnreadable:
            log.error("archived media retention: archive index unreadable; nothing deleted")
            return summary
        candidates = index.get("truckCandidateJobs", [])
        if not isinstance(candidates, list):
            return summary
        # Every directory something can still read. One malformed pointer holds the pass.
        held: set[Path] = set()
        pointers = [job.get("artifactRoot") for job in candidates if isinstance(job, dict)]
        for job in jobs.values():
            if not isinstance(job, dict):
                return summary
            masters = job.get("recoveryMasters") or []
            if not isinstance(masters, list):
                return summary
            pointers.append(job.get("artifactRoot"))
            pointers.extend(master.get("artifactRoot") if isinstance(master, dict) else 0 for master in masters)
        for value in pointers:
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                return summary
            try:
                held.add(Path(value).resolve())
            except (OSError, RuntimeError):
                return summary
        if expired() or not self._read_archives(store_path, expired):
            return summary
        root = root.resolve()
        for job_id, value in self._roots.items():
            if summary["dirs"] >= dir_limit or expired():
                break
            if job_id in self._gone or job_id in jobs:
                continue
            directory = _job_directory(root, job_id, value)
            if directory is None:
                continue
            if not directory.is_dir():
                self._gone.add(job_id)
                continue
            if any(_related(directory, path) for path in held):
                summary["held"] += 1
                continue
            summary["dirs"] += 1
            for current, _subdirs, names in os.walk(directory, topdown=False):
                for name in names:
                    path = Path(current, name)
                    try:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        if not dry_run:
                            path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        summary["failed"] += 1
                        log.exception("archived media retention could not delete %s", path)
                        continue
                    summary["files"] += 1
                    summary["bytes_freed"] += info.st_size
                if not dry_run and len(Path(current).relative_to(root).parts) >= JOB_DIR_DEPTH:
                    try:
                        os.rmdir(current)  # only ever removes an EMPTY directory
                    except OSError:
                        pass
        log.info("archived media retention dry_run=%s dirs=%d files=%d bytes_freed=%d failed=%d held=%d archived_jobs=%d",
                 dry_run, summary["dirs"], summary["files"], summary["bytes_freed"], summary["failed"],
                 summary["held"], len(self._roots))
        return summary
