"""
TikTok upload queue manager.
Handles job persistence, FIFO queue with max 1 concurrent upload,
cookie file detection, and thread-pool execution of sync Playwright calls.
"""

import asyncio
import glob
import json
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

UPLOAD_JOBS_FILE = "upload_jobs.json"
COOKIE_DIR = "."  # TK_cookies_{account}.json stored in project root
MAX_CONCURRENT = 1
DEFAULT_DELAY_MIN = 5 * 60   # 5 minutes
DEFAULT_DELAY_MAX = 15 * 60  # 15 minutes

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiktok-upload")
_queue_lock = asyncio.Lock()
_active_job_id: str | None = None
_queue_running = False


# ── Job persistence ──────────────────────────────────────────────────────────

def _load_jobs() -> list[dict]:
    if not os.path.exists(UPLOAD_JOBS_FILE):
        return []
    try:
        with open(UPLOAD_JOBS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_jobs(jobs: list[dict]) -> None:
    with open(UPLOAD_JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2, default=str)


def _update_job(job_id: str, updates: dict) -> dict | None:
    jobs = _load_jobs()
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

    jobs = _load_jobs()
    jobs.append(job)
    _save_jobs(jobs)
    return job


def cancel_job(job_id: str) -> dict | None:
    """Cancel a queued job. Cannot cancel an in-progress upload."""
    job = get_job(job_id)
    if not job:
        return None
    if job["status"] != "queued":
        return job  # Can only cancel queued jobs
    return _update_job(job_id, {"status": "cancelled", "completed_at": datetime.now().isoformat()})


# ── Cookie helpers ───────────────────────────────────────────────────────────

def list_cookie_files() -> list[dict]:
    """Find all TK_cookies_*.json files and return status info."""
    pattern = os.path.join(COOKIE_DIR, "TK_cookies_*.json")
    results = []
    for path in glob.glob(pattern):
        name = Path(path).stem  # TK_cookies_accountname
        account = name.replace("TK_cookies_", "")
        try:
            with open(path, "r") as f:
                data = json.load(f)
            # Check for cookie expiry — look for a cookie with expiry field
            has_expired = False
            if isinstance(data, list):
                for cookie in data:
                    exp = cookie.get("expiry") or cookie.get("expires")
                    if exp and isinstance(exp, (int, float)) and exp < time.time():
                        has_expired = True
                        break
            results.append({
                "account": account,
                "path": path,
                "status": "expired" if has_expired else "valid",
                "cookie_count": len(data) if isinstance(data, list) else 0,
                "modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
            })
        except (json.JSONDecodeError, IOError):
            results.append({
                "account": account,
                "path": path,
                "status": "corrupt",
                "cookie_count": 0,
                "modified": None,
            })
    return results


def get_cookie_status(account_name: str) -> str:
    """Get cookie status for a specific account: valid, expired, missing, corrupt."""
    path = os.path.join(COOKIE_DIR, f"TK_cookies_{account_name}.json")
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            for cookie in data:
                exp = cookie.get("expiry") or cookie.get("expires")
                if exp and isinstance(exp, (int, float)) and exp < time.time():
                    return "expired"
        return "valid"
    except (json.JSONDecodeError, IOError):
        return "corrupt"


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

    job = get_job(job_id)
    if not job or job["status"] != "queued":
        return

    _active_job_id = job_id
    _update_job(job_id, {"status": "uploading", "started_at": datetime.now().isoformat()})

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _run_upload_sync, job)

    now = datetime.now().isoformat()
    if result == "Completed":
        _update_job(job_id, {"status": "completed", "completed_at": now})
    else:
        _update_job(job_id, {"status": "failed", "completed_at": now, "error": result})

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
