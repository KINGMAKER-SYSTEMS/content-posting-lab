"""
MCP server for Content Posting Lab.

Exposes the batch-driven workflow (create batch -> generate -> burn -> distribute)
as MCP tools and resources. Mounted into the same FastAPI app at /mcp so it
ships in a single Railway deploy.

Long-running operations (generate, burn) follow the Runway MCP pattern:
the tool returns immediately with a job_id, the agent polls check_job until
the job reaches a terminal state. This avoids MCP's request/response timeout.

If FastMCP isn't installed, the module exposes mount_mcp() as a no-op so the
core app still boots. Run `pip install fastmcp` to enable.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("mcp_server")

try:
    from fastmcp import FastMCP  # type: ignore
    _FASTMCP_AVAILABLE = True
except ImportError:
    _FASTMCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment,misc]


def _build_mcp() -> Any:
    """Construct the MCP server with all tools/resources registered.

    Imports are local so this module is safe to import even when fastmcp
    isn't installed yet.
    """
    if not _FASTMCP_AVAILABLE or FastMCP is None:
        return None

    from project_manager import list_projects, sanitize_project_name
    from routers import video as video_router
    from services import batches as batches_service

    mcp = FastMCP("content-posting-lab")

    # ── Resources (read-only data) ──────────────────────────────────────

    @mcp.resource("projects://list")
    def list_all_projects_resource() -> dict:
        """Every project on this server. Returns name + counts per project."""
        return {"projects": list_projects()}

    @mcp.resource("batches://{project}/open")
    def list_open_batches_resource(project: str) -> dict:
        """Open batches in a project — the default working set."""
        sanitized = sanitize_project_name(project)
        batches = batches_service.list_batches(sanitized, status="open", include_legacy=False)
        return {"project": sanitized, "batches": batches}

    @mcp.resource("batches://{project}/{batch_id}/manifest")
    def batch_manifest_resource(project: str, batch_id: str) -> dict:
        """Full hydrated manifest for a batch — every output file across stages."""
        sanitized = sanitize_project_name(project)
        manifest = batches_service.hydrate_manifest(sanitized, batch_id)
        if manifest is None:
            return {"error": f"Batch '{batch_id}' not found"}
        return manifest

    # ── Tools (agent actions with side effects) ─────────────────────────

    @mcp.tool()
    def create_batch(project: str, label: str | None = None) -> dict:
        """Create a new batch in a project.

        A batch is a workflow grouping that flows through generate -> burn ->
        distribute. Use this when starting a coherent piece of work (e.g. a
        campaign push for an artist release).

        Args:
            project: Project name (e.g. 'thevelvethours').
            label: Optional human-readable label (e.g. 'drake-may-04').

        Returns:
            { "batch_id": str, "label": str, "created_at": str }
        """
        try:
            batch = batches_service.create_batch(project, label)
        except FileNotFoundError as e:
            return {"error": str(e)}
        return {
            "batch_id": batch["id"],
            "label": batch["label"],
            "created_at": batch["created_at"],
        }

    @mcp.tool()
    def list_batches(project: str, status: str = "open", since: str = "7d") -> dict:
        """List batches in a project.

        Defaults to open batches in the last 7 days (clean working set).
        Pass since='all' for full history, status='closed' for archived batches.

        Args:
            project: Project name.
            status: 'open' or 'closed'.
            since: Time window like '24h', '7d', or 'all'.

        Returns:
            { "batches": [...], "count": int }
        """
        sanitized = sanitize_project_name(project)
        since_td = batches_service.parse_since(since)
        batches = batches_service.list_batches(
            sanitized,
            status=status if status in ("open", "closed") else None,
            since=since_td,
        )
        return {"batches": batches, "count": len(batches)}

    @mcp.tool()
    def get_daily_batch(project: str) -> dict:
        """Get or create today's default daily batch for a project.

        Useful when an agent wants to drop work into a sensible default
        without naming a batch explicitly.
        """
        try:
            batch = batches_service.get_or_create_daily_batch(project)
        except FileNotFoundError as e:
            return {"error": str(e)}
        return {"batch_id": batch["id"], "label": batch["label"]}

    @mcp.tool()
    async def generate_videos(
        project: str,
        prompt: str,
        provider: str = "wan-t2v",
        count: int = 1,
        batch_id: str | None = None,
        duration: int = 10,
        aspect_ratio: str = "9:16",
        resolution: str = "720p",
        crop_mode: str | None = None,
    ) -> dict:
        """Kick off video generation — returns immediately with a job_id.

        Use check_job(job_id) to poll for completion. When done, call
        get_batch_manifest(batch_id) to get the output URLs.

        Args:
            project: Project name.
            prompt: The text prompt.
            provider: Provider ID (e.g. 'wan-t2v', 'grok', 'hailuo'). See list_providers.
            count: Number of variations to generate (1-20).
            batch_id: Optional. If omitted, lands in today's daily batch.
            duration: Seconds (1-15, provider-dependent).
            aspect_ratio: '9:16', '16:9', '1:1', etc.
            resolution: '480p', '720p', '1080p' (provider-dependent).
            crop_mode: 'none', 'dual', 'triptych', 'both' for multi-crop providers.

        Returns:
            { "job_id": str, "batch_id": str, "count": int, "status": "pending" }
        """
        # FastAPI Form parsing makes direct invocation messy. Use an
        # in-process ASGI transport so MCP calls hit the real endpoint
        # without going over the network — keeps the generate logic as the
        # single source of truth.
        import httpx
        from app import app as fastapi_app

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal") as client:
            data = {
                "prompt": prompt,
                "provider": provider,
                "count": str(count),
                "duration": str(duration),
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "project": project,
            }
            if batch_id:
                data["batch_id"] = batch_id
            if crop_mode and crop_mode != "none":
                data["crop_mode"] = crop_mode
            res = await client.post("/api/video/generate", data=data)
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            payload = res.json()
            return {
                "job_id": payload["job_id"],
                "batch_id": payload.get("batch_id"),
                "count": payload["count"],
                "status": "pending",
            }

    @mcp.tool()
    def check_job(job_id: str) -> dict:
        """Poll a generation job's status. Call repeatedly until status is
        'done' or 'error' for every video in the job.

        Returns:
            {
              "job_id": str,
              "status": "pending" | "done" | "partial",
              "videos": [{ "index": int, "status": str, "url": str | null, "error": str | null }, ...]
            }
        """
        # Try in-memory first; fall back to disk
        job = video_router.jobs.get(job_id)
        if not job:
            # Scan all projects for the job
            from project_manager import PROJECTS_DIR
            for proj_dir in PROJECTS_DIR.iterdir():
                if proj_dir.is_dir():
                    video_router._load_jobs(proj_dir.name)
            job = video_router.jobs.get(job_id)
            if not job:
                return {"error": f"Job '{job_id}' not found"}

        videos = job.get("videos", [])
        terminal = {"done", "error", "failed"}
        all_terminal = all(v.get("status") in terminal for v in videos)
        any_done = any(v.get("status") == "done" for v in videos)
        any_error = any(v.get("status") in ("error", "failed") for v in videos)

        if all_terminal:
            status = "done" if any_done and not any_error else ("partial" if any_done else "error")
        else:
            status = "pending"

        return {
            "job_id": job_id,
            "batch_id": job.get("batch_id"),
            "status": status,
            "videos": [
                {
                    "index": v.get("index"),
                    "status": v.get("status"),
                    "url": v.get("url"),
                    "error": v.get("error"),
                    "crops": v.get("crops"),
                }
                for v in videos
            ],
        }

    @mcp.tool()
    def get_batch_manifest(project: str, batch_id: str) -> dict:
        """Get the full output manifest for a batch — every video, burn,
        clip, and slideshow URL in one structured response.

        Use this to hand off a completed batch to a downstream tool, or to
        report final deliverables to the user.
        """
        manifest = batches_service.hydrate_manifest(project, batch_id)
        if manifest is None:
            return {"error": f"Batch '{batch_id}' not found"}
        return manifest

    @mcp.tool()
    def close_batch(project: str, batch_id: str) -> dict:
        """Mark a batch as closed. Closed batches are excluded from default
        list responses but stay accessible by ID.
        """
        batch = batches_service.close_batch(project, batch_id)
        if not batch:
            return {"error": f"Batch '{batch_id}' not found"}
        return {"batch_id": batch_id, "status": "closed"}

    @mcp.tool()
    def list_providers() -> dict:
        """List every video generation provider that has an API key configured."""
        from providers import PROVIDERS
        from providers.base import API_KEYS
        available = []
        for pid, info in PROVIDERS.items():
            if API_KEYS.get(info["key_id"]):
                available.append({
                    "id": pid,
                    "name": info.get("name", pid),
                    "group": info.get("group"),
                })
        return {"providers": available}

    @mcp.tool()
    def list_projects_tool() -> dict:
        """List every project on this server with file counts."""
        return {"projects": list_projects()}

    # ── Pipeline tools (burn, distribute) ──────────────────────────────

    @mcp.tool()
    async def burn_batch(
        project: str,
        batch_id: str,
        caption_text: str,
        font_size: int = 72,
        position: str = "bottom",
        font_family: str = "TikTokSans-Bold",
    ) -> dict:
        """Burn a text caption onto every completed video in a batch.

        Renders a simple PNG overlay server-side and applies it to each
        video in the batch's generate stage. Returns the burn batch ID
        and the per-video queue status. The actual burn runs async —
        poll get_batch_manifest(batch_id) to see when burned files appear,
        or use batch_status_summary to track overall progress.

        Args:
            project: Project name.
            batch_id: Workflow batch_id whose videos should be burned.
            caption_text: The text to overlay. Single line works best.
            font_size: Pixel size (default 72, good for 1080x1920).
            position: 'top', 'middle', or 'bottom' (default 'bottom').
            font_family: Font basename without extension (default TikTokSans-Bold).

        Returns:
            { "burn_batch_id": str, "queued": int, "errors": [...] }
        """
        from datetime import datetime
        from pathlib import Path
        import base64
        import io
        import httpx
        from PIL import Image, ImageDraw, ImageFont
        from app import app as fastapi_app

        # 1. Resolve the batch and find generate-stage videos
        batch = batches_service.get_batch(project, batch_id)
        if not batch:
            return {"error": f"Batch '{batch_id}' not found"}

        # Find every completed video file across the batch's generate jobs
        manifest = batches_service.hydrate_manifest(project, batch_id)
        if manifest is None:
            return {"error": "Could not hydrate manifest"}
        videos = []
        for gen_job in manifest["stages"]["generate"]:
            for f in gen_job["files"]:
                videos.append(f["file"])
        if not videos:
            return {"error": "No completed videos in this batch yet"}

        # 2. Render the overlay PNG (1080x1920 for TikTok)
        WIDTH, HEIGHT = 1080, 1920
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        font_path = Path(__file__).parent / "fonts" / f"{font_family}.ttf"
        if not font_path.exists():
            font_path = Path(__file__).parent / "fonts" / "TikTokSans-Bold.ttf"
        try:
            font = ImageFont.truetype(str(font_path), font_size)
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), caption_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (WIDTH - text_w) // 2
        if position == "top":
            y = HEIGHT // 8
        elif position == "middle":
            y = (HEIGHT - text_h) // 2
        else:  # bottom
            y = HEIGHT - text_h - HEIGHT // 8

        # Outline for legibility on busy backgrounds
        outline = 4
        for ox in range(-outline, outline + 1):
            for oy in range(-outline, outline + 1):
                if ox == 0 and oy == 0:
                    continue
                draw.text((x + ox, y + oy), caption_text, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), caption_text, font=font, fill=(255, 255, 255, 255))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        overlay_b64 = base64.b64encode(buf.getvalue()).decode()

        # 3. Build burn batch ID and queue per-video burns via /api/burn/overlay
        ts = datetime.now().strftime("%m%d%H%M")
        slug = (batch.get("label") or "burn").lower().replace(" ", "-")[:30]
        burn_batch_id = f"{slug}-{ts}-mcp"

        transport = httpx.ASGITransport(app=fastapi_app)
        queued = 0
        errors: list[str] = []
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=30) as client:
            for idx, video_rel in enumerate(videos):
                try:
                    res = await client.post("/api/burn/overlay", json={
                        "project": project,
                        "batchId": burn_batch_id,
                        "parentBatchId": batch_id,
                        "index": idx,
                        "videoPath": video_rel,
                        "overlayPng": overlay_b64,
                    })
                    if res.status_code == 200:
                        queued += 1
                    else:
                        errors.append(f"#{idx}: {res.text[:200]}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"#{idx}: {e}")

        return {
            "burn_batch_id": burn_batch_id,
            "parent_batch_id": batch_id,
            "queued": queued,
            "errors": errors,
        }

    @mcp.tool()
    async def send_batch_to_posters(
        project: str,
        batch_id: str,
        integration_ids: list[str],
    ) -> dict:
        """Send every burned video in a batch to one or more page integrations
        (each integration maps to a Telegram staging topic for its page).

        Looks up the batch's burn-stage outputs and pushes each one to the
        Telegram staging group's topic for each integration_id. Posters who
        own that page see the videos in their group.

        Args:
            project: Project name.
            batch_id: Workflow batch_id whose burned videos should be sent.
            integration_ids: List of Postiz integration IDs (page identifiers).
                             Each integration has its own staging topic.

        Returns:
            { "results": [{ "integration_id": str, "burn_batch_id": str, "sent": int, "errors": [...] }, ...] }
        """
        import httpx
        from app import app as fastapi_app

        batch = batches_service.get_batch(project, batch_id)
        if not batch:
            return {"error": f"Batch '{batch_id}' not found"}

        burn_batch_ids = batch.get("stage_jobs", {}).get("burn", [])
        if not burn_batch_ids:
            return {"error": "No burn batches attached to this workflow batch"}
        if not integration_ids:
            return {"error": "No integration_ids provided"}

        transport = httpx.ASGITransport(app=fastapi_app)
        results = []
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=120) as client:
            for integration_id in integration_ids:
                for burn_batch_id in burn_batch_ids:
                    try:
                        res = await client.post("/api/telegram/send-batch", json={
                            "project": project,
                            "batch_id": burn_batch_id,
                            "integration_id": integration_id,
                        })
                        if res.status_code == 200:
                            payload = res.json()
                            results.append({
                                "integration_id": integration_id,
                                "burn_batch_id": burn_batch_id,
                                "sent": payload.get("sent", 0),
                                "errors": payload.get("errors", []),
                            })
                        else:
                            results.append({
                                "integration_id": integration_id,
                                "burn_batch_id": burn_batch_id,
                                "sent": 0,
                                "errors": [f"HTTP {res.status_code}: {res.text[:200]}"],
                            })
                    except Exception as e:  # noqa: BLE001
                        results.append({
                            "integration_id": integration_id,
                            "burn_batch_id": burn_batch_id,
                            "sent": 0,
                            "errors": [str(e)],
                        })

        # Track distribution in batch notes (lightweight, no schema change)
        try:
            sent_total = sum(r["sent"] for r in results)
            existing_notes = batch.get("notes", "")
            stamp = f"[{integration_ids}] sent {sent_total} videos to telegram via MCP"
            new_notes = f"{existing_notes}\n{stamp}".strip() if existing_notes else stamp
            batches_service.update_notes(project, batch_id, new_notes[:5000])
        except Exception:  # noqa: BLE001
            pass

        return {"results": results}

    # ── Status / orchestration ──────────────────────────────────────────

    @mcp.tool()
    def batch_status_summary(project: str, batch_id: str) -> dict:
        """Aggregated progress for a batch — one call instead of N check_jobs.

        Walks every job in the batch's generate stage, tallies status counts,
        and returns a summary an agent can act on.

        Returns:
            {
              "batch_id": str,
              "status": "pending" | "in_progress" | "done" | "partial" | "error",
              "generate": {"total": N, "done": N, "errors": N, "pending": N, "files": N},
              "burn": {"batches": N, "files": N},
              "blocking_jobs": [job_id, ...]   # jobs not yet terminal
            }
        """
        manifest = batches_service.hydrate_manifest(project, batch_id)
        if manifest is None:
            return {"error": f"Batch '{batch_id}' not found"}

        gen_total = 0
        gen_done = 0
        gen_errors = 0
        gen_pending = 0
        gen_files = 0
        blocking: list[str] = []

        for gen_job in manifest["stages"]["generate"]:
            jid = gen_job["job_id"]
            job = video_router.jobs.get(jid)
            if not job:
                # Try loading from disk
                video_router._load_jobs(project)
                job = video_router.jobs.get(jid)
            if not job:
                continue
            for v in job.get("videos", []):
                gen_total += 1
                s = v.get("status")
                if s == "done":
                    gen_done += 1
                elif s in ("error", "failed"):
                    gen_errors += 1
                else:
                    gen_pending += 1
                    if jid not in blocking:
                        blocking.append(jid)
            gen_files += len(gen_job["files"])

        burn_batches = len(manifest["stages"]["burn"])
        burn_files = sum(len(b["files"]) for b in manifest["stages"]["burn"])

        if gen_total == 0:
            status = "pending"
        elif gen_pending > 0:
            status = "in_progress"
        elif gen_errors == 0:
            status = "done"
        elif gen_done == 0:
            status = "error"
        else:
            status = "partial"

        return {
            "batch_id": batch_id,
            "status": status,
            "generate": {
                "total": gen_total,
                "done": gen_done,
                "errors": gen_errors,
                "pending": gen_pending,
                "files": gen_files,
            },
            "burn": {"batches": burn_batches, "files": burn_files},
            "blocking_jobs": blocking,
        }

    # ── Clipper ─────────────────────────────────────────────────────────

    @mcp.tool()
    async def clip_videos(
        project: str,
        sources: list[dict],
        clip_length: float = 7.0,
        batch_id: str | None = None,
    ) -> dict:
        """Cut long videos into 9:16 short-form clips.

        Each source is {"path": "abs or projects-relative video path",
        "trim_start": float seconds (optional), "trim_end": float seconds
        (optional)}. The clipper splits each source into clip_length-second
        9:16 chunks.

        Returns immediately with a clip job_id. Poll get_clip_job(job_id)
        for progress, or wait_for_batch with stage='clip'.

        Args:
            project: Project name.
            sources: List of {path, trim_start?, trim_end?} dicts.
            clip_length: Length of each output clip in seconds (default 7).
            batch_id: Workflow batch to attach this clip job to. If omitted,
                      lands in today's daily default.

        Returns:
            { "job_id": str, "total_clips": int, "batch_id": str }
        """
        import httpx
        from app import app as fastapi_app
        if not sources:
            return {"error": "Must provide at least one source video"}

        body = {
            "project": project,
            "clip_length": clip_length,
            "sources": sources,
        }
        if batch_id:
            body["parent_batch_id"] = batch_id

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=60) as client:
            res = await client.post("/api/clipper/process-batch", json=body)
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            payload = res.json()
            return {
                "job_id": payload["job_id"],
                "total_clips": payload["total_clips"],
                "batch_id": batch_id,
            }

    @mcp.tool()
    async def get_clip_job(job_id: str) -> dict:
        """Poll a clipper job's progress. Returns clip count, files, status."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/clipper/process-batch/{job_id}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_clip_jobs(project: str) -> dict:
        """List clipper jobs for a project, newest first."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/clipper/jobs?project={project}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    # ── Caption scraper ─────────────────────────────────────────────────

    @mcp.tool()
    async def scrape_captions(
        project: str,
        profile_url: str,
        max_videos: int = 20,
        sort: str = "latest",
        batch_id: str | None = None,
    ) -> dict:
        """Scrape captions from a TikTok profile via OCR + sentiment analysis.

        Pulls thumbnails from the profile, OCRs visible captions with GPT-4
        vision, and writes results to projects/{project}/captions/{username}/captions.csv.

        Returns immediately with a caption job_id. The job runs in the
        background; the agent should poll get_captions(username) or check
        the batch's caption stage in the manifest after ~30-60 seconds.

        Args:
            project: Project name.
            profile_url: TikTok profile URL or @handle.
            max_videos: Max videos to scrape (default 20, capped at 50).
            sort: 'latest' or 'popular'.
            batch_id: Workflow batch to attach to. Defaults to today's daily.

        Returns:
            { "job_id": str, "username": str, "batch_id": str }
        """
        import asyncio as _asyncio
        from datetime import datetime
        import uuid as _uuid
        import re as _re
        from routers import captions as captions_router

        # Mirror the WS handshake pattern: spin up a job_id and call _run_pipeline
        job_id = f"cap-{datetime.now().strftime('%m%d%H%M')}-{_uuid.uuid4().hex[:6]}"

        # Normalize username for the response (best-effort)
        m = _re.search(r"@([a-zA-Z0-9_.]+)", profile_url)
        username = m.group(1) if m else profile_url.rstrip("/").split("/")[-1].lstrip("@")

        _asyncio.create_task(captions_router._run_pipeline(
            job_id,
            profile_url,
            min(max(1, max_videos), 50),
            sort,
            project,
            parent_batch_id=batch_id,
        ))

        return {
            "job_id": job_id,
            "username": username,
            "batch_id": batch_id,
            "status": "running",
            "note": "Poll list_captions or check batch manifest after ~30-60s",
        }

    @mcp.tool()
    async def list_captions(project: str) -> dict:
        """List all caption scrapes for a project (per-username folders + CSV stats)."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/captions/history?project={project}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    # ── Recreate ────────────────────────────────────────────────────────

    @mcp.tool()
    async def recreate_video(
        project: str,
        video_url: str,
        batch_id: str | None = None,
    ) -> dict:
        """Download a TikTok/IG video, extract first + last frames with text
        removed, and stage them for re-generation.

        The output (clean first/last frames) lands in
        projects/{project}/recreate/{job_id}/ and can be fed back into
        generate_videos via the WAN-i2v provider for re-creation.

        Args:
            project: Project name.
            video_url: Source video URL (TikTok, IG, etc — anything yt-dlp
                       can fetch).
            batch_id: Workflow batch to attach to. Defaults to today's daily.

        Returns:
            { "job_id": str, "batch_id": str, "status": "running" }
        """
        import asyncio as _asyncio
        from datetime import datetime
        import uuid as _uuid
        from routers import recreate as recreate_router

        job_id = f"rec-{datetime.now().strftime('%m%d%H%M')}-{_uuid.uuid4().hex[:6]}"

        _asyncio.create_task(recreate_router._run_pipeline(
            job_id,
            video_url,
            project,
            parent_batch_id=batch_id,
        ))

        return {
            "job_id": job_id,
            "batch_id": batch_id,
            "status": "running",
            "note": "Poll list_recreate_jobs after ~60s",
        }

    @mcp.tool()
    async def list_recreate_jobs(project: str) -> dict:
        """List recreate jobs for a project."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/recreate/jobs?project={project}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    # ── Slideshow ───────────────────────────────────────────────────────

    @mcp.tool()
    async def render_slideshow(
        project: str,
        slides: list[dict],
        fps: int = 30,
        batch_id: str | None = None,
    ) -> dict:
        """Render a slideshow video from images and per-slide durations.

        Each slide is {"image": "filename in slideshow-images/",
        "duration": float seconds}. Returns immediately with a job_id;
        the actual render runs in a thread pool. Poll get_slideshow_job
        for progress.

        Args:
            project: Project name.
            slides: List of slide configs. Each needs 'image' (filename
                    relative to slideshow-images/) and 'duration' (seconds).
            fps: Output framerate (default 30).
            batch_id: Workflow batch to attach to. Defaults to today's daily.

        Returns:
            { "job_id": str, "batch_id": str }
        """
        import httpx
        from app import app as fastapi_app
        body = {
            "project": project,
            "slides": slides,
            "fps": fps,
        }
        if batch_id:
            body["parent_batch_id"] = batch_id

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=30) as client:
            res = await client.post("/api/slideshow/render", json=body)
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            payload = res.json()
            return {"job_id": payload["job_id"], "batch_id": batch_id}

    @mcp.tool()
    async def get_slideshow_job(job_id: str) -> dict:
        """Poll a slideshow render job's progress."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/slideshow/job/{job_id}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_slideshow_renders(project: str) -> dict:
        """List rendered slideshow MP4s for a project."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/slideshow/renders?project={project}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    # ── Direct upload (TikTok / IG cookie-based) ────────────────────────

    @mcp.tool()
    async def submit_upload(
        video_path: str,
        account_name: str,
        description: str = "",
        hashtags: list[str] | None = None,
        sound_name: str | None = None,
        schedule_time: str | None = None,
        schedule_day: int | None = None,
        project: str | None = None,
        batch_id: str | None = None,
    ) -> dict:
        """Queue a TikTok upload via the cookie-based engine.

        The video is uploaded under the named account using its stored
        login cookies (must be logged in beforehand via the UI's login
        flow — agents can't do interactive login).

        Args:
            video_path: Path to the MP4 to upload (absolute or relative
                        to projects/).
            account_name: TikTok account whose cookies to use.
            description: Caption text for the post.
            hashtags: List of hashtags WITHOUT # prefix.
            sound_name: Optional TikTok sound to attach.
            schedule_time: ISO timestamp to schedule the post (optional).
            schedule_day: Day-of-week scheduling (1=Mon … 7=Sun).
            project: Project to attach the upload job to (optional).
            batch_id: Workflow batch to attach to (defaults to daily if
                      project is set).

        Returns:
            { "job_id": str, "status": str, "batch_id": str | None }
        """
        import httpx
        from app import app as fastapi_app
        body = {
            "video_path": video_path,
            "account_name": account_name,
            "description": description,
            "hashtags": hashtags or [],
        }
        if sound_name:
            body["sound_name"] = sound_name
        if schedule_time:
            body["schedule_time"] = schedule_time
        if schedule_day is not None:
            body["schedule_day"] = schedule_day
        if project:
            body["project"] = project
        if batch_id:
            body["parent_batch_id"] = batch_id

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=30) as client:
            res = await client.post("/api/upload/submit", json=body)
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            payload = res.json()
            return {
                "job_id": payload["job"]["id"],
                "status": payload["job"]["status"],
                "batch_id": batch_id,
            }

    @mcp.tool()
    async def get_upload_job(job_id: str) -> dict:
        """Poll an upload job's status."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/upload/jobs/{job_id}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_upload_accounts() -> dict:
        """List available TikTok upload accounts (those with stored cookies)."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get("/api/upload/cookies")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    # ── Roster + Telegram metadata ──────────────────────────────────────

    @mcp.tool()
    async def list_roster() -> dict:
        """List the page roster — every Postiz integration with its project
        + Drive folder mapping. Use this to find integration_ids for
        send_batch_to_posters.
        """
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get("/api/roster/")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_roster_for_project(project: str) -> dict:
        """List roster integrations filtered to a single project."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get(f"/api/roster/project/{project}")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_posters() -> dict:
        """List all configured Telegram posters and their owned page_ids."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get("/api/telegram/posters")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"posters": data} if isinstance(data, list) else data

    @mcp.tool()
    async def telegram_status() -> dict:
        """Check Telegram bot health (token configured, staging group, etc)."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get("/api/telegram/status")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"items": data} if isinstance(data, list) else data

    @mcp.tool()
    async def list_sounds() -> dict:
        """List the synced TikTok sound library (campaign_id → sound_url)."""
        import httpx
        from app import app as fastapi_app
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp-internal", timeout=10) as client:
            res = await client.get("/api/telegram/sounds")
            if res.status_code != 200:
                return {"error": res.text, "status_code": res.status_code}
            data = res.json()
            return {"sounds": data} if isinstance(data, list) else data

    @mcp.tool()
    async def wait_for_batch(
        project: str,
        batch_id: str,
        stage: str = "generate",
        timeout_seconds: int = 600,
        poll_interval: float = 3.0,
    ) -> dict:
        """Block until every job in a batch's stage reaches a terminal state.

        Saves the agent from writing its own polling loop. Uses
        batch_status_summary internally; returns the final summary on
        completion or timeout.

        Args:
            project: Project name.
            batch_id: Batch to wait on.
            stage: Currently only 'generate' is supported.
            timeout_seconds: Max seconds to wait (default 10 minutes).
            poll_interval: Seconds between status checks (default 3).

        Returns:
            { "completed": bool, "timed_out": bool, "summary": <batch_status_summary>, "elapsed_seconds": int }
        """
        import asyncio as _asyncio
        import time as _time

        if stage != "generate":
            return {"error": f"Unsupported stage '{stage}'. Only 'generate' is supported."}

        start = _time.time()
        elapsed = 0.0
        last_summary: dict = {}
        while elapsed < timeout_seconds:
            last_summary = batch_status_summary(project, batch_id)
            if "error" in last_summary:
                return {"completed": False, "timed_out": False, "summary": last_summary, "elapsed_seconds": int(elapsed)}
            if last_summary.get("status") in ("done", "partial", "error"):
                return {"completed": True, "timed_out": False, "summary": last_summary, "elapsed_seconds": int(elapsed)}
            await _asyncio.sleep(poll_interval)
            elapsed = _time.time() - start

        return {"completed": False, "timed_out": True, "summary": last_summary, "elapsed_seconds": int(elapsed)}

    return mcp


def get_mcp_asgi_app() -> Any | None:
    """Build and return the MCP Starlette ASGI app, or None if FastMCP is missing.

    The caller is responsible for mounting the returned app into the parent
    FastAPI app AND chaining its lifespan into the parent's lifespan (FastMCP
    requires lifespan setup to initialize its task group).
    """
    if not _FASTMCP_AVAILABLE:
        log.info("fastmcp not installed — MCP server disabled. Run `pip install fastmcp` to enable.")
        return None

    mcp = _build_mcp()
    if mcp is None:
        return None

    try:
        if hasattr(mcp, "http_app"):
            return mcp.http_app()
        if hasattr(mcp, "streamable_http_app"):
            return mcp.streamable_http_app()
        return mcp.sse_app()  # type: ignore[attr-defined]
    except Exception:
        log.exception("failed to build MCP ASGI app")
        return None


def mount_mcp(app: Any, *, path: str = "/mcp") -> bool:
    """Convenience: mount the MCP app into a FastAPI app at the given path.

    NOTE: this only handles the mount, NOT the lifespan chain. For full
    functionality, the parent app's lifespan must include the MCP app's
    lifespan. See app.py for the proper integration pattern.
    """
    mcp_app = get_mcp_asgi_app()
    if mcp_app is None:
        return False
    try:
        app.mount(path, mcp_app)
        log.info("MCP server mounted at %s (lifespan must be chained externally)", path)
        return True
    except Exception:
        log.exception("failed to mount MCP server")
        return False
