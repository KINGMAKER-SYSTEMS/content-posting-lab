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
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
_quarantined_job_rows: dict[str, list[object]] = {}


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
        try:
            handle = open(f"{UPLOAD_JOBS_FILE}.lock", "a+b")
        except OSError as exc:
            raise UploadUnavailable("TikTok upload is unavailable: job storage lock could not be opened") from exc
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _stored_job_error(job: object, seen_ids: set[str]) -> str | None:
    """Return the first queue-safety check that a stored row fails."""
    statuses = {"queued", "uploading", "held", "completed", "failed", "cancelled"}
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


def _duplicate_stored_job_ids(rows: list[object]) -> set[str]:
    """Hold every repeated ID before a claim can make identical copies disagree."""
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if job_id in seen_ids:
            duplicate_ids.add(job_id)
        else:
            seen_ids.add(job_id)
    return duplicate_ids


def _quarantine_stored_jobs(rows: list[tuple[int, str, object]]) -> None:
    """Append rejected rows to an audit file while the job-store lock is held."""
    quarantine_path = Path(f"{UPLOAD_JOBS_FILE}.quarantine.jsonl")
    with quarantine_path.open("a+", encoding="utf-8") as handle:
        handle.seek(0)
        existing = set()
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            job_key = json.dumps(record.get("job"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            existing.add((record.get("reason"), job_key))
        handle.seek(0, os.SEEK_END)
        for index, reason, job in rows:
            record = {"row_index": index, "reason": reason, "job": job}
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            job_key = json.dumps(job, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            dedupe_key = (reason, job_key)
            if dedupe_key not in existing:
                handle.write(line + "\n")
                existing.add(dedupe_key)
            logger.error("Quarantined upload job row %d: %s", index, reason)
        handle.flush()
        os.fsync(handle.fileno())


def _read_stored_jobs(*, _lock_held: bool = False) -> list[dict]:
    if not _lock_held:
        with _job_store_write_lock():
            return _read_stored_jobs(_lock_held=True)
    path = Path(UPLOAD_JOBS_FILE)
    path_key = str(path.resolve())
    if not path.exists():
        # Absence is indistinguishable from a lost queue containing an earlier
        # browser claim. Operators must provision a real empty [] file before
        # the first submission; a deleted file never silently reinitializes.
        raise UploadUnavailable(
            f"TikTok upload is unavailable: job store {path_key} is missing. "
            "Inspect and restore the original queue or explicitly provision an empty [] store "
            "only after ruling out prior upload attempts."
        )
    recovery = (
        f"Upload job file {path_key} is unreadable. Inspect the file, fix it or restore it from a backup, then retry."
    )
    try:
        with path.open(encoding="utf-8") as handle:
            jobs = json.load(handle)
    except (OSError, ValueError):
        # An unreadable file may hide an in-flight claim; starting empty could duplicate a post.
        logger.exception("%s", recovery)
        raise UploadUnavailable(f"TikTok upload is unavailable: {recovery}") from None
    if not isinstance(jobs, list):
        logger.error("%s", recovery)
        raise UploadUnavailable(f"TikTok upload is unavailable: {recovery}")

    valid_jobs: list[dict] = []
    rejected_rows: list[tuple[int, str, object]] = []
    seen_ids: set[str] = set()
    duplicate_ids = _duplicate_stored_job_ids(jobs)
    for index, job in enumerate(jobs):
        reason = _stored_job_error(job, seen_ids)
        if (isinstance(job, dict) and isinstance(job.get("job_id"), str)
                and job["job_id"] in duplicate_ids):
            reason = f"job_id {job['job_id']} has duplicate stored rows; reconciliation required"
        if reason is not None:
            rejected_rows.append((index, reason, job))
            continue
        valid_jobs.append(job)
        seen_ids.add(job["job_id"])

    if rejected_rows:
        # Keep rejected source rows in place: one may be an unresolved claim whose
        # provider outcome is unknown. Quarantine is an audit copy, not deletion.
        _quarantined_job_rows[path_key] = [job for _index, _reason, job in rejected_rows]
        try:
            _quarantine_stored_jobs(rejected_rows)
        except OSError:
            quarantine_path = Path(f"{UPLOAD_JOBS_FILE}.quarantine.jsonl").resolve()
            reasons = "; ".join(f"row {index}: {reason}" for index, reason, _job in rejected_rows)
            logger.exception(
                "Could not write upload quarantine file %s for %s; leaving job store %s unchanged and continuing with valid rows",
                quarantine_path,
                reasons,
                path_key,
            )
            return valid_jobs
    else:
        _quarantined_job_rows.pop(path_key, None)
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
    # serializable. Keep quarantined source rows active for duplicate checks.
    rejected_rows = _quarantined_job_rows.get(str(Path(UPLOAD_JOBS_FILE).resolve()), [])
    atomic_save(UPLOAD_JOBS_FILE, [*jobs, *rejected_rows], default=str)


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


def _account_has_active_upload(jobs: list[dict], account_name: str, video_path: str, job_id: str) -> bool:
    """A saved claim outlives its store lock, process, and browser session."""
    if any(
        row["job_id"] != job_id
        and row["account_name"] == account_name
        and row["status"] in {"uploading", "held"}
        for row in jobs
    ):
        return True
    # A quarantined row's invalid shape/status cannot establish that the
    # provider was never called. Even a saved terminal label may be one of
    # multiple conflicting copies, so hold this account for reconciliation.
    return (
        _quarantined_account_attempt(account_name) is not None
        or _quarantined_unknown_account_attempt(video_path) is not None
    )


def _quarantined_account_attempt(account_name: str) -> dict | None:
    rejected = _quarantined_job_rows.get(str(Path(UPLOAD_JOBS_FILE).resolve()), [])
    return next(
        (row for row in rejected if isinstance(row, dict) and row.get("account_name") == account_name),
        None,
    )


def _quarantined_unknown_account_attempt(video_path: str) -> dict | None:
    """An unknown account may still identify a prior Post by its real video path.

    When both identity fields are lost, no target can be ruled out; all new
    claims wait for reconciliation of that original row.
    """
    rejected = _quarantined_job_rows.get(str(Path(UPLOAD_JOBS_FILE).resolve()), [])
    target = os.path.realpath(video_path)
    return next(
        (
            row for row in rejected
            if isinstance(row, dict)
            and (not isinstance(row.get("account_name"), str) or not row["account_name"])
            and (
                not isinstance(row.get("video_path"), str) or not row["video_path"]
                or os.path.realpath(row["video_path"]) == target
            )
        ),
        None,
    )


def _has_runnable_queued_job() -> bool:
    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        return any(
            row["status"] == "queued"
            and not _account_has_active_upload(jobs, row["account_name"], row["video_path"], row["job_id"])
            for row in jobs
        )


def _has_canonical_video_receipt(job: dict) -> bool:
    """Only a durable canonical TikTok ID and matching URL resolve a Post."""
    video_id = job.get("video_id")
    video_url = job.get("video_url")
    if not isinstance(video_id, str) or not re.fullmatch(r"[0-9]+", video_id):
        return False
    if not isinstance(video_url, str):
        return False
    try:
        parsed = urlsplit(video_url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"tiktok.com", "www.tiktok.com"}
        and re.fullmatch(r"/@[^/]+/video/" + re.escape(video_id), parsed.path) is not None
        and not parsed.query
        and not parsed.fragment
    )


# ── Public API ───────────────────────────────────────────────────────────────

def get_all_jobs() -> list[dict]:
    # Held IDs remain visible to callers, but never enter the executable list.
    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        rejected = _quarantined_job_rows.get(str(Path(UPLOAD_JOBS_FILE).resolve()), [])
        duplicate_ids = _duplicate_stored_job_ids(rejected)
        for row in rejected:
            if isinstance(row, dict) and isinstance(row.get("job_id"), str) and row["job_id"] in duplicate_ids:
                jobs.append({
                    **row,
                    "status": "held",
                    "error": "Duplicate stored job ID; reconciliation required before execution or resubmission",
                })
                duplicate_ids.remove(row["job_id"])
        return jobs


def get_job(job_id: str) -> dict | None:
    for job in get_all_jobs():
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
        unresolved = _quarantined_account_attempt(account_name)
        if unresolved is not None:
            raise UploadReconciliationRequired(
                "TikTok upload original attempt requires reconciliation before another "
                "submission on this account (job %s)" % (unresolved.get("job_id") or "unknown")
            )
        unresolved = _quarantined_unknown_account_attempt(video_path)
        if unresolved is not None:
            raise UploadReconciliationRequired(
                "TikTok upload original attempt requires reconciliation before another "
                "submission for this video or an unknowable target (job %s)"
                % (unresolved.get("job_id") or "unknown")
            )
        target = os.path.realpath(video_path)
        stored_rows = [*jobs, *_quarantined_job_rows.get(str(Path(UPLOAD_JOBS_FILE).resolve()), [])]
        duplicate_ids = _duplicate_stored_job_ids(stored_rows)
        for existing in stored_rows:
            if (isinstance(existing, dict)
                    and isinstance(existing.get("account_name"), str)
                    and isinstance(existing.get("video_path"), str)
                    and existing["account_name"] == account_name
                    and (existing.get("status") != "cancelled"
                         and not (existing.get("status") == "completed" and _has_canonical_video_receipt(existing))
                         or (isinstance(existing.get("job_id"), str) and existing["job_id"] in duplicate_ids))
                    and os.path.realpath(existing["video_path"]) == target):
                # Only cancelled or canonically proven completed history permits
                # explicit resubmission; unresolved history stays a barrier.
                raise UploadReconciliationRequired(
                    "TikTok upload original attempt requires reconciliation "
                    "before resubmission (job %s)" % (existing.get("job_id") or "unknown")
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


async def _process_single_job(job_id: str) -> bool:
    """Process a single upload job in the thread pool."""
    global _active_job_id

    with _job_store_write_lock():
        jobs = _read_stored_jobs(_lock_held=True)
        job = next((row for row in jobs if row["job_id"] == job_id), None)
        if not job or job["status"] != "queued":
            return False
        if _account_has_active_upload(jobs, job["account_name"], job["video_path"], job_id):
            # Other processes may execute a different video on this account.
            # The durable claim, not a process-local mutex, is the guard.
            return False
        # Persist the claim before the browser call. A lost outcome remains
        # uploading until reconciled and admission refuses the same target.
        job.update({"status": "uploading", "started_at": datetime.now().isoformat()})
        _save_jobs(jobs)

    _active_job_id = job_id
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, _run_upload_sync, job)
        now = datetime.now().isoformat()
        # The provider's bare "Completed" is not a canonical TikTok URL/video
        # ID. It may follow an ambiguous browser Post. Preserve the original
        # claim and provider return for reconciliation; never permit an
        # automatic or same-target resubmission on this evidence alone.
        _update_job(job_id, {
            "status": "held",
            "error": f"Provider outcome requires reconciliation: {result}",
            "provider_result": result,
            "provider_result_at": now,
        })
        return True
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
            # A queued job may belong to an account with an unresolved claim in
            # another process. Try each row once and leave blocked rows queued.
            jobs = _load_jobs()
            queued = [job for job in jobs if job["status"] == "queued"]
            if not queued:
                break
            processed = False
            for job in queued:
                if await _process_single_job(job["job_id"]):
                    processed = True
                    break
            if not processed:
                break

            # Random delay between uploads (anti-rate-limit)
            delay = random.uniform(DEFAULT_DELAY_MIN, DEFAULT_DELAY_MAX)
            # Check if there are more queued jobs before sleeping
            if _has_runnable_queued_job():
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
