"""
TikTok upload engine router.
Manages upload job queue and cookie login sessions.
"""

import asyncio
import subprocess
import sys

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.upload import (
    cancel_job,
    get_all_jobs,
    get_cookie_status,
    get_job,
    get_queue_stats,
    list_cookie_files,
    process_queue,
    submit_job,
)

router = APIRouter()


# ── Job submission ───────────────────────────────────────────────────────────


class SubmitRequest(BaseModel):
    video_path: str
    account_name: str
    description: str = ""
    hashtags: list[str] = []
    sound_name: str | None = None
    sound_aud_vol: str = "mix"
    schedule_time: str | None = None
    schedule_day: int | None = None
    copyrightcheck: bool = False
    headless: bool = True
    stealth: bool = True
    proxy: dict | None = None


@router.post("/submit")
async def submit_upload(req: SubmitRequest):
    """Queue a new upload job."""
    # Validate cookie exists
    cookie_status = get_cookie_status(req.account_name)
    if cookie_status == "missing":
        raise HTTPException(
            status_code=400,
            detail=f"No cookies for account '{req.account_name}'. Run login first.",
        )
    if cookie_status == "expired":
        raise HTTPException(
            status_code=400,
            detail=f"Cookies expired for account '{req.account_name}'. Re-login required.",
        )

    job = submit_job(
        video_path=req.video_path,
        account_name=req.account_name,
        description=req.description,
        hashtags=req.hashtags,
        sound_name=req.sound_name,
        sound_aud_vol=req.sound_aud_vol,
        schedule_time=req.schedule_time,
        schedule_day=req.schedule_day,
        copyrightcheck=req.copyrightcheck,
        headless=req.headless,
        stealth=req.stealth,
        proxy=req.proxy,
    )

    # Start queue processing in background
    asyncio.create_task(process_queue())

    return {"job": job}


# ── Job listing + polling ────────────────────────────────────────────────────


@router.get("/jobs")
async def list_jobs():
    """List all upload jobs."""
    jobs = get_all_jobs()
    stats = get_queue_stats()
    return {"jobs": jobs, "stats": stats}


@router.get("/jobs/{job_id}")
async def poll_job(job_id: str):
    """Get status of a specific upload job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@router.post("/jobs/{job_id}/cancel")
async def cancel_upload(job_id: str):
    """Cancel a queued upload job."""
    job = cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "cancelled":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel job in '{job['status']}' state",
        )
    return {"job": job}


# ── Cookie management ────────────────────────────────────────────────────────


@router.get("/cookies")
async def list_cookies():
    """List all TikTok cookie files and their status."""
    cookies = list_cookie_files()
    return {"cookies": cookies}


@router.get("/cookies/{account_name}")
async def check_cookie(account_name: str):
    """Check cookie status for a specific account."""
    status = get_cookie_status(account_name)
    return {"account": account_name, "status": status}


@router.post("/login/{account_name}")
async def trigger_login(account_name: str):
    """Trigger a non-headless browser login for TikTok cookie acquisition.

    Opens a visible browser window for manual login. The user must complete
    the login flow manually. Cookies are saved to TK_cookies_{account}.json.
    """
    # Run login in a subprocess so it doesn't block the event loop
    # tiktokautouploader opens a browser window for manual login on first run
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"""
from tiktokautouploader import upload_tiktok
# Trigger login flow by attempting upload with headless=False
# This will open browser for manual login, save cookies, then we can cancel
try:
    upload_tiktok(
        video='__login_trigger__',
        description='',
        accountname='{account_name}',
        headless=False,
        stealth=True,
    )
except Exception:
    pass  # Expected — we just need the login/cookie save
""",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return {
            "status": "login_started",
            "account": account_name,
            "message": "Browser window opened. Complete the TikTok login manually.",
            "pid": proc.pid,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start login: {e}")


# ── Queue stats ──────────────────────────────────────────────────────────────


@router.get("/stats")
async def queue_stats():
    """Get upload queue statistics."""
    return get_queue_stats()
