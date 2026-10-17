"""
TikTok upload queue manager.
Handles job persistence, FIFO queue with max 1 concurrent upload,
cookie file detection, and thread-pool execution of sync Playwright calls.
"""

import asyncio
import fcntl
import glob
import importlib
import json
import logging
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from services.json_store import atomic_save, lock_for

UPLOAD_JOBS_FILE = os.getenv("CONTENT_LAB_UPLOAD_JOBS_FILE", "upload_jobs.json")
COOKIE_DIR = "."  # TK_cookies_{account}.json stored in project root
MAX_CONCURRENT = 1
DEFAULT_DELAY_MIN = 5 * 60   # 5 minutes
DEFAULT_DELAY_MAX = 15 * 60  # 15 minutes

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiktok-upload")
_queue_lock = asyncio.Lock()
_active_job_id: str | None = None
_queue_running = False
logger = logging.getLogger(__name__)


class UploadUnavailable(RuntimeError):
    """A new upload cannot be admitted without its executor and durable job record."""


class UploadReconciliationRequired(UploadUnavailable):
    """The same account/video has an unresolved upload attempt."""


@contextmanager
def _job_store_write_lock():
    """Serialize admission and status writes across local worker processes."""
    with lock_for(UPLOAD_JOBS_FILE):
        # The JSON file is replaced atomically, so its inode cannot be the lock.
        # A stable sidecar coordinates read/check/append with other processes.
        with open(f"{UPLOAD_JOBS_FILE}.lock", "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _stored_job_error(job: object, seen_ids: set[str]) -> str | None:
    """Return the first queue-safety check that a stored row fails."""
    statuses = {"queued", "uploading", "completed", "failed", "cancelled"}
    if not isinstance(job, dict):
        return "row must be an object"
    if not isinstance(job.get("job_id"), str) or not job["job_id"]:
        return "job_id must be a non-empty string"
    if job["job_id"] in seen_ids:
        return "job_id is duplicated"
    if not isinstance(job.get("status"), str) or job["status"] not in statuses:
        return "status is invalid"
    for key in ("video_path", "account_name"):
        if not isinstance(job.get(key), str) or not job[key]:
            return f"{key} must be a non-empty string"
    if job["status"] == "queued":
        required = (
            "description", "hashtags", "sound_name", "sound_aud_vol",
            "schedule_time", "schedule_day", "copyrightcheck", "headless",
            "stealth", "proxy",
        )
        missing = next((key for key in required if key not in job), None)
        if missing:
            return f"{missing} is required for a queued job"
        if not isinstance(job.get("description"), str):
            return "description must be a string"
        if not isinstance(job.get("hashtags"), list) or not all(isinstance(tag, str) for tag in job["hashtags"]):
            return "hashtags must be a list of strings"
        for key in ("sound_name", "schedule_time"):
            if job.get(key) is not None and not isinstance(job[key], str):
                return f"{key} must be a string or null"
        if not isinstance(job.get("sound_aud_vol"), str):
            return "sound_aud_vol must be a string"
        if job.get("schedule_day") is not None and type(job["schedule_day"]) is not int:
            return "schedule_day must be an integer or null"
        for key in ("copyrightcheck", "headless", "stealth"):
            if type(job.get(key)) is not bool:
                return f"{key} must be a boolean"
        if job.get("proxy") is not None and not isinstance(job["proxy"], dict):
            return "proxy must be an object or null"
    return None


def _valid_stored_jobs(value: object) -> bool:
    """Check fields the queue reads before accepting another mutation."""
    if not isinstance(value, list):
        return False
    seen_ids: set[str] = set()
    for job in value:
        if _stored_job_error(job, seen_ids) is not None:
            return False
        seen_ids.add(job["job_id"])
    return True


def _quarantine_stored_jobs(rows: list[tuple[int, str, object]]) -> None:
    """Append rejected rows to an audit file while the job-store lock is held."""
    quarantine_path = Path(f"{UPLOAD_JOBS_FILE}.quarantine.jsonl")
    with quarantine_path.open("a", encoding="utf-8") as handle:
        for index, reason, job in rows:
            record = {"row_index": index, "reason": reason, "job": job}
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            logger.error("Quarantined upload job row %d: %s", index, reason)
        handle.flush()
        os.fsync(handle.fileno())


def _read_stored_jobs(*, _lock_held: bool = False) -> list[dict]:
    if not _lock_held:
        with _job_store_write_lock():
            return _read_stored_jobs(_lock_held=True)
    path = Path(UPLOAD_JOBS_FILE)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as handle:
            jobs = json.load(handle)
    except (OSError, ValueError):
        logger.exception("Upload job file is corrupt or unreadable; refusing to start an empty queue")
        raise UploadUnavailable("TikTok upload is unavailable: job storage is unreadable") from None
    if not isinstance(jobs, list):
        logger.error("Upload job file root is not a list; refusing to start an empty queue")
        raise UploadUnavailable("TikTok upload is unavailable: job storage is unreadable")

    valid_jobs: list[dict] = []
    rejected_rows: list[tuple[int, str, object]] = []
    seen_ids: set[str] = set()
    for index, job in enumerate(jobs):
        reason = _stored_job_error(job, seen_ids)
        if reason is not None:
            rejected_rows.append((index, reason, job))
            continue
        valid_jobs.append(job)
        seen_ids.add(job["job_id"])

    if rejected_rows:
        # A valid list may contain an interrupted or malformed row. Preserve the
        # valid jobs and quarantine only the rows that failed their own checks.
        _quarantine_stored_jobs(rejected_rows)
        _save_jobs(valid_jobs)
    return valid_jobs


def require_upload_runtime() -> None:
    """Refuse hosted or broken provider runtimes before upload or login actions."""
    if any(os.getenv(name) for name in (
        "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID", "RAILWAY_VOLUME_MOUNT_PATH"
    )):
        raise UploadUnavailable(
            "TikTok upload is unavailable on this hosted service: durable upload reconciliation is not configured"
        )

    try:
        uploader = importlib.import_module("tiktokautouploader")
    except Exception:
        uploader = None
    if uploader is None or not callable(getattr(uploader, "upload_tiktok", None)):
        raise UploadUnavailable("TikTok upload is unavailable: provider runtime is not installed")


def require_upload_admission() -> None:
    """Refuse an upload before writing a job when the executor or store is absent."""
    require_upload_runtime()

    path = Path(UPLOAD_JOBS_FILE)
    if not path.parent.is_dir():
        raise UploadUnavailable("TikTok upload is unavailable: job storage directory is missing")

    _read_stored_jobs()


# ── Job persistence ──────────────────────────────────────────────────────────

def _load_jobs() -> list[dict]:
    return _read_stored_jobs()


def _save_jobs(jobs: list[dict]) -> None:
    # ATOMIC write (tmp + rename) via the shared store — a raw open('w')+dump left the upload-job
    # queue truncated/wiped if the process crashed mid-write (a P0). default=str keeps datetimes
    # serializable.
    atomic_save(UPLOAD_JOBS_FILE, jobs, default=str)


def _update_job(job_id: str, updates: dict) -> dict | None:
    # Serialize the local queue read/modify/write with submissions so a status
    # transition cannot clobber a newly admitted job across local processes.
    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        for job in jobs:
            if job["job_id"] == job_id:
                job.update(updates)
                _save_jobs(jobs)
                return job
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def get_all_jobs() -> list[dict]:
    return _load_jobs()


def get_job(job_id: str) -> dict | None:
    for job in _load_jobs():
        if job["job_id"] == job_id:
            return job
    return None


def submit_job(
    video_path: str,
    account_name: str,
    description: str = "",
    hashtags: list[str] | None = None,
    sound_name: str | None = None,
    sound_aud_vol: str = "mix",
    schedule_time: str | None = None,
    schedule_day: int | None = None,
    copyrightcheck: bool = False,
    headless: bool = True,
    stealth: bool = True,
    proxy: dict | None = None,
) -> dict:
    """Create a new upload job and add to queue."""
    require_upload_admission()
    job = {
        "job_id": str(uuid.uuid4()),
        "status": "queued",
        "video_path": video_path,
        "account_name": account_name,
        "description": description,
        "hashtags": hashtags or [],
        "sound_name": sound_name,
        "sound_aud_vol": sound_aud_vol,
        "schedule_time": schedule_time,
        "schedule_day": schedule_day,
        "copyrightcheck": copyrightcheck,
        "headless": headless,
        "stealth": stealth,
        "proxy": proxy,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "error": None,
    }

    if not _valid_stored_jobs([job]):
        raise ValueError("invalid upload job")

    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        target = os.path.realpath(video_path)
        for existing in jobs:
            if (existing["account_name"] == account_name
                    and os.path.realpath(existing["video_path"]) == target
                    and existing["status"] in {"queued", "uploading", "failed"}):
                # `uploading` may have posted before a crash; even a failed
                # browser call can have an unknown provider outcome. Never
                # enqueue a second same-target Post until the original job is
                # reconciled. A queued duplicate likewise needs no new job.
                raise UploadReconciliationRequired(
                    "TikTok upload original attempt requires reconciliation "
                    "before resubmission (job %s)" % existing["job_id"]
                )
        jobs.append(job)
        _save_jobs(jobs)
    return job


def cancel_job(job_id: str) -> dict | None:
    """Cancel only a job that remains queued under the upload claim lock."""
    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        for job in jobs:
            if job["job_id"] != job_id:
                continue
            if job["status"] == "queued":
                job.update({"status": "cancelled", "completed_at": datetime.now().isoformat()})
                _save_jobs(jobs)
            return job
    return None


# ── Cookie helpers ───────────────────────────────────────────────────────────

def _load_cookie_file(path: str) -> tuple[str, int]:
    """Load and validate a cookie file. Returns (status, cookie_count).

    status is one of: valid, expired, corrupt. Single source of truth for
    cookie expiry-check logic shared by list_cookie_files / get_cookie_status.
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return "corrupt", 0
    if isinstance(data, list):
        for cookie in data:
            exp = cookie.get("expiry") or cookie.get("expires")
            if exp and isinstance(exp, (int, float)) and exp < time.time():
                return "expired", len(data)
        return "valid", len(data)
    return "valid", 0


def list_cookie_files() -> list[dict]:
    """Find all TK_cookies_*.json files and return status info."""
    pattern = os.path.join(COOKIE_DIR, "TK_cookies_*.json")
    results = []
    for path in glob.glob(pattern):
        name = Path(path).stem  # TK_cookies_accountname
        account = name.replace("TK_cookies_", "")
        status, count = _load_cookie_file(path)
        results.append({
            "account": account,
            "path": path,
            "status": status,
            "cookie_count": count,
            "modified": (
                datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
                if status != "corrupt" else None
            ),
        })
    return results


def get_cookie_status(account_name: str) -> str:
    """Get cookie status for a specific account: valid, expired, missing, corrupt."""
    path = os.path.join(COOKIE_DIR, f"TK_cookies_{account_name}.json")
    if not os.path.exists(path):
        return "missing"
    return _load_cookie_file(path)[0]


# ── Upload execution ─────────────────────────────────────────────────────────

def _run_upload_sync(job: dict) -> str:
    """Run tiktokautouploader synchronously (called in thread pool)."""
    try:
        from tiktokautouploader import upload_tiktok
    except ImportError:
        return "Error: tiktokautouploader not installed"

    kwargs: dict[str, Any] = {
        "video": job["video_path"],
        "description": job["description"],
        "accountname": job["account_name"],
        "copyrightcheck": job["copyrightcheck"],
        "headless": job["headless"],
        "stealth": job["stealth"],
    }

    if job["hashtags"]:
        kwargs["hashtags"] = job["hashtags"]
    if job["sound_name"]:
        kwargs["sound_name"] = job["sound_name"]
        kwargs["sound_aud_vol"] = job["sound_aud_vol"]
    if job["schedule_time"]:
        kwargs["schedule"] = job["schedule_time"]
    if job["schedule_day"] is not None:
        kwargs["day"] = job["schedule_day"]
    if job["proxy"]:
        kwargs["proxy"] = job["proxy"]

    try:
        result = upload_tiktok(**kwargs)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


async def _process_single_job(job_id: str) -> None:
    """Process a single upload job in the thread pool."""
    global _active_job_id

    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        job = next((row for row in jobs if row["job_id"] == job_id), None)
        if not job or job["status"] != "queued":
            return
        # Persist the claim before the browser call. A lost outcome stays
        # uploading and admission will refuse the same target.
        job.update({"status": "uploading", "started_at": datetime.now().isoformat()})
        _save_jobs(jobs)

    _active_job_id = job_id
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, _run_upload_sync, job)
        now = datetime.now().isoformat()
        if result == "Completed":
            _update_job(job_id, {"status": "completed", "completed_at": now})
        else:
            _update_job(job_id, {"status": "failed", "completed_at": now, "error": result})
    finally:
        _active_job_id = None


async def process_queue() -> None:
    """Process the upload queue. Called after submitting a job."""
    global _queue_running

    async with _queue_lock:
        if _queue_running:
            return
        _queue_running = True

    try:
        while True:
            # Find next queued job
            jobs = _load_jobs()
            next_job = None
            for job in jobs:
                if job["status"] == "queued":
                    next_job = job
                    break

            if not next_job:
                break

            await _process_single_job(next_job["job_id"])

            # Random delay between uploads (anti-rate-limit)
            delay = random.uniform(DEFAULT_DELAY_MIN, DEFAULT_DELAY_MAX)
            # Check if there are more queued jobs before sleeping
            remaining = [j for j in _load_jobs() if j["status"] == "queued"]
            if remaining:
                await asyncio.sleep(delay)
    finally:
        _queue_running = False


def get_queue_stats() -> dict:
    """Get queue statistics."""
    jobs = _load_jobs()
    return {
        "queued": sum(1 for j in jobs if j["status"] == "queued"),
        "uploading": sum(1 for j in jobs if j["status"] == "uploading"),
        "completed": sum(1 for j in jobs if j["status"] == "completed"),
        "failed": sum(1 for j in jobs if j["status"] == "failed"),
        "cancelled": sum(1 for j in jobs if j["status"] == "cancelled"),
        "active_job_id": _active_job_id,
        "queue_running": _queue_running,
    }
