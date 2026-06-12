"""
AgenticBuilderNews — router for the living-workspace kanban flywheel.

Mounted at /api/agenticnews. Endpoints:
  Board:   GET/POST /videos, PATCH/DELETE /videos/{id}, POST /videos/{id}/move
  Jobs:    GET/POST /jobs, POST /jobs/{id}/claim|complete|fail
  Chat:    POST /chat, GET /chat/poll, GET /chat/inbox, POST /chat/reply, GET /chat/history
  Tools:   POST /tools/tts, /tools/cards, /tools/assemble, /tools/scrape
  Flywheel: GET /patterns, GET /stats
"""
from __future__ import annotations

import os
import json
import shlex
import asyncio
import subprocess
import urllib.request
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse

import services.agenticnews as db
import services.abn_factory as factory
import services.editor_render as editor_render
import services.editor_timeline as editor_timeline
from fastapi.responses import StreamingResponse

router = APIRouter()


# ============ FACTORY EVENT STREAM (SSE) ============
@router.get("/stream")
async def stream(since: int = 0):
    async def gen():
        for ev in factory.BUS.replay(since):
            yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
        q = await factory.BUS.subscribe()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            factory.BUS.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/factory/state")
async def factory_state():
    return factory.STATE


@router.post("/factory/pause")
async def factory_pause():
    factory.pause(); return {"ok": True}


@router.post("/factory/resume")
async def factory_resume():
    factory.resume(); return {"ok": True}


@router.post("/tools/approve")
async def approve(body: dict = Body(...)):
    v = await db.update_video(body["episode_id"], {"stage": "scheduled"})
    factory.BUS.emit("operator", "episode.approved", "episode approved by operator",
                     episode_id=body["episode_id"])
    # FLYWHEEL: mark approved in memory so its thesis becomes a proven angle
    try:
        import services.abn_memory as mem
        if v:
            tl = v.get("timeline", {}).get("segments", [])
            titles = [s.get("title", "") for s in tl] or [v.get("title", "")]
            thesis = (v.get("artifacts", {}) or {}).get("cold_open", "")
            mem.record_episode(body["episode_id"], titles, thesis, approved=True, rendered=True)
    except Exception:
        pass
    return v or {}


@router.post("/tools/reject")
async def reject(body: dict = Body(...)):
    v = await db.update_video(body["episode_id"], {"stage": "revision"})
    factory.BUS.emit("operator", "episode.rejected", f"rejected: {body.get('note','')}",
                     episode_id=body["episode_id"])
    # FLYWHEEL (negative signal): rejection = "not good stories" — learn what to avoid
    try:
        import services.abn_memory as mem
        if v:
            titles = [s.get("title", "") for s in (v.get("timeline", {}) or {}).get("segments", [])] or [v.get("title", "")]
            mem.record_rejection(titles)
    except Exception:
        pass
    return v or {}

VOICE_DEFAULT = os.getenv("ABN_VOICE", "")  # path to cloned voice .safetensors, else default


# ---------- helpers ----------
async def _sh(cmd: str, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


# ============ BOARD ============
@router.get("/videos")
async def get_videos(stage: str | None = None, since: float | None = None):
    return {"videos": await db.list_videos(stage, since), "stages": db.STAGES}


@router.post("/videos")
async def post_video(body: dict = Body(...)):
    return await db.create_video(body)


@router.patch("/videos/{vid}")
async def patch_video(vid: str, body: dict = Body(...)):
    v = await db.update_video(vid, body)
    if not v:
        raise HTTPException(404, "not found")
    return v


@router.delete("/videos/{vid}")
async def del_video(vid: str):
    await db.delete_video(vid)
    return {"ok": True}


@router.post("/videos/{vid}/move")
async def move_video(vid: str, body: dict = Body(...)):
    stage = body.get("stage")
    if stage not in db.STAGES:
        raise HTTPException(400, f"bad stage {stage}")
    patch = {"stage": stage}
    if "lane" in body:
        patch["lane"] = body["lane"]
    v = await db.update_video(vid, patch)
    if not v:
        raise HTTPException(404, "not found")
    # flywheel: entering 'live' spawns analytics capture + a model-this idea on strong outliers
    if stage == "live":
        await _on_publish(v)
    return v


async def _on_publish(v: dict):
    # stub analytics until YouTube API wired; record a baseline pattern
    score = (v.get("metrics") or {}).get("outlier_score", 1.0)
    await db.record_pattern(v["id"], v.get("hook", ""), v.get("format", ""),
                            v.get("title", ""), score)
    if score >= 3.0:
        await db.create_video(dict(
            title=f"[model-this] next angle on: {v.get('title','')}",
            stage="idea", lane="week", format=v.get("format", "Headline→Build"),
            hook=f"Winner at {score}× — what's the next angle?",
            source_signal=f"flywheel ← {v['id']}"))


# ============ JOBS ============
@router.get("/jobs")
async def get_jobs(status: str | None = None, type: str | None = None):
    return {"jobs": await db.list_jobs(status, type)}


@router.post("/jobs")
async def post_job(body: dict = Body(...)):
    return await db.create_job(body.get("video_id"), body["job_type"], body.get("payload"))


@router.post("/jobs/{jid}/claim")
async def claim_job(jid: str, body: dict = Body(...)):
    j = await db.update_job(jid, {"status": "running", "agent_id": body.get("agent_id", "agent")})
    if not j:
        raise HTTPException(404, "not found")
    if j.get("video_id"):
        await db.update_video(j["video_id"], {"locked_by": body.get("agent_id", "agent")})
    return j


@router.post("/jobs/{jid}/complete")
async def complete_job(jid: str, body: dict = Body(...)):
    j = await db.update_job(jid, {"status": "done", "result": body.get("result", {})})
    if not j:
        raise HTTPException(404, "not found")
    if j.get("video_id"):
        patch = {"locked_by": None}
        if body.get("artifacts"):
            patch["artifacts"] = body["artifacts"]
        if body.get("stage"):
            patch["stage"] = body["stage"]
        await db.update_video(j["video_id"], patch)
    return j


@router.post("/jobs/{jid}/fail")
async def fail_job(jid: str, body: dict = Body(...)):
    j = await db.update_job(jid, {"status": "failed", "error": body.get("error", "")})
    if j and j.get("video_id"):
        await db.update_video(j["video_id"], {"locked_by": None})
    return j or {}


# ============ CHAT BRIDGE ============
@router.post("/chat")
async def chat_send(body: dict = Body(...)):
    return await db.chat_post("user", body["text"])


@router.get("/chat/poll")
async def chat_poll():
    return {"messages": [m["text"] for m in await db.chat_drain("to_page")]}


@router.get("/chat/inbox")
async def chat_inbox():
    return {"messages": [m["text"] for m in await db.chat_drain("to_claude")]}


@router.post("/chat/reply")
async def chat_reply(body: dict = Body(...)):
    return await db.chat_post("claude", body["text"])


@router.get("/chat/history")
async def chat_hist(limit: int = 50):
    return {"messages": await db.chat_history(limit)}


# ============ TOOLS (the real wiring) ============
@router.post("/tools/tts")
async def tool_tts(body: dict = Body(...)):
    """Render VO with Pocket-TTS. Optionally tied to a video card."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "no text")
    vid = body.get("video_id")
    name = body.get("name") or (vid or "vo")
    out = db.ASSETS_DIR / f"{name}.wav"
    voice = body.get("voice") or VOICE_DEFAULT
    cmd = f'pocket-tts generate --text {shlex.quote(text)} --output-path {shlex.quote(str(out))} --quiet'
    if voice and Path(voice).exists():
        cmd += f' --voice {shlex.quote(voice)}'
    code, log = await _sh(cmd, timeout=600)
    if code != 0 or not out.exists():
        raise HTTPException(500, f"tts failed: {log[-500:]}")
    rel = f"/agenticnews-assets/{out.name}"
    if vid:
        await db.update_video(vid, {"artifacts": {"vo": True, "vo_path": rel}})
    return {"ok": True, "path": rel}


@router.post("/tools/cards")
async def tool_cards(body: dict = Body(...)):
    """Render a branded title card PNG with ImageMagick."""
    vid = body.get("video_id")
    name = body.get("name") or (vid or "card")
    title = body.get("title", "AGENTICBUILDERNEWS")
    sub = body.get("subtitle", "")
    foot = body.get("footer", "AgenticBuilderNews")
    out = db.ASSETS_DIR / f"{name}_card.png"
    font = "/System/Library/Fonts/Helvetica.ttc"
    cmd = (
        f'magick -size 1920x1080 xc:"#08090b" -gravity center '
        f'-font {shlex.quote(font)} '
        f'-fill "#f2f4f7" -pointsize 92 -annotate +0-120 {shlex.quote(title)} '
        f'-fill "#6e8bff" -pointsize 44 -annotate +0+10 {shlex.quote(sub)} '
        f'-fill "#6b7280" -pointsize 32 -annotate +0+120 {shlex.quote(foot)} '
        f'{shlex.quote(str(out))}'
    )
    code, log = await _sh(cmd, timeout=60)
    if code != 0 or not out.exists():
        raise HTTPException(500, f"card failed: {log[-500:]}")
    rel = f"/agenticnews-assets/{out.name}"
    if vid:  # auto-attach so caller can't desync the path
        await db.update_video(vid, {"artifacts": {"thumbnail": True, "thumbnail_path": rel}})
    return {"ok": True, "path": rel}


@router.post("/tools/assemble")
async def tool_assemble(body: dict = Body(...)):
    """Assemble card(s) + VO into an MP4 with ffmpeg (zoom + audio)."""
    vid = body.get("video_id")
    name = body.get("name") or (vid or "clip")
    card = body.get("card_path")
    vo = body.get("vo_path")
    if not card or not vo:
        raise HTTPException(400, "need card_path and vo_path")
    cardf = db.ASSETS_DIR / Path(card).name
    vof = db.ASSETS_DIR / Path(vo).name
    out = db.ASSETS_DIR / f"{name}_assembled.mp4"
    cmd = (
        f'ffmpeg -y -loop 1 -i {shlex.quote(str(cardf))} -i {shlex.quote(str(vof))} '
        f'-filter_complex "[0:v]scale=1920:1080,zoompan=z=\'min(zoom+0.0006,1.1)\':d=99999:s=1920x1080:fps=25[v]" '
        f'-map "[v]" -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest {shlex.quote(str(out))}'
    )
    code, log = await _sh(cmd, timeout=300)
    if code != 0 or not out.exists():
        raise HTTPException(500, f"assemble failed: {log[-600:]}")
    rel = f"/agenticnews-assets/{out.name}"
    if vid:
        await db.update_video(vid, {"artifacts": {"assembly": True, "assembly_path": rel}, "stage": "review"})
    return {"ok": True, "path": rel}


@router.post("/tools/scrape")
async def tool_scrape(body: dict = Body(...)):
    """Scrape HN + GitHub for fresh agentic items, score, create idea cards."""
    created = []
    # HN Algolia
    try:
        url = "https://hn.algolia.com/api/v1/search?query=AI%20agent&tags=story&numericFilters=points%3E80&hitsPerPage=8"
        with urllib.request.urlopen(url, timeout=15) as r:
            hits = json.load(r).get("hits", [])
        for h in hits[:6]:
            t = h.get("title", "")
            if not t:
                continue
            pts = h.get("points", 0)
            v = await db.create_video(dict(
                title=t, stage="idea", lane="week", format="Headline→Build",
                hook="", source_url=h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                source_signal=f"HN {pts}pts · {h.get('num_comments',0)}c",
                metrics={"score": pts}))
            created.append({"id": v["id"], "title": t, "signal": f"HN {pts}pts"})
    except Exception as e:
        return JSONResponse({"created": created, "warn": f"hn: {e}"}, status_code=200)
    return {"created": created, "count": len(created)}


@router.get("/episodes/{ep_id}/qa")
async def episode_qa(ep_id: str):
    """Automated self-QA: grade the episode against GROUND-TRUTH artifacts (render props + mp4 + package),
    codifying the manual checks I kept re-deriving. Reads the render-props json (not the stripped DB
    timeline) so it always inspects what Remotion actually used. Returns pass/fail per dimension."""
    import json as _json, os as _os, subprocess as _sp, re as _re
    base = _os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "."
    assets = _os.path.join(base, "agenticnews_assets")
    if not _os.path.isdir(assets):
        assets = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "agenticnews_assets")
    props_path = _os.path.join(assets, f"{ep_id}_timeline.json")
    mp4 = _os.path.join(assets, f"{ep_id}_episode.mp4")
    checks, props = {}, None
    if _os.path.exists(props_path):
        try: props = _json.load(open(props_path))
        except Exception: props = None
    # video present + audio leveled
    checks["video_rendered"] = _os.path.exists(mp4)
    if _os.path.exists(mp4):
        try:
            out = _sp.run(["ffmpeg", "-i", mp4, "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=60).stderr
            m = _re.search(r'mean_volume:\s*(-?[\d.]+)', out)
            mean = float(m.group(1)) if m else None
            checks["audio_leveled"] = bool(mean is not None and -30 < mean < -12)
        except Exception:
            checks["audio_leveled"] = None
    # render-props dimensions (ground truth)
    if props:
        segs = props.get("segments", [])
        checks["has_segments"] = len(segs) >= 3
        # chapters in props would be in package; here check pops + tool-name coverage
        toolpop = 0
        for s in segs:
            pops = [p.get("word", "").lower() for p in s.get("keywordPops", [])]
            tool = (s.get("title", "").split(":")[0].split(" — ")[0].split() or [""])[0].lower()
            if tool and any(tool in p for p in pops):
                toolpop += 1
        checks["tool_name_pops"] = f"{toolpop}/{len(segs)} segments"
        checks["music_bed"] = bool(props.get("musicBed"))
        checks["sfx"] = bool(props.get("sfx"))
    # package completeness
    v = next((x for x in await db.list_videos() if x.get("id") == ep_id), None)
    if v:
        pkg = (v.get("artifacts", {}) or {}).get("package", {})
        checks["title"] = bool(pkg.get("titler"))
        checks["seo_with_chapters"] = bool(pkg.get("seo") and "0:00" in (pkg.get("seo") or ""))
        checks["thumbnail"] = bool(pkg.get("thumbnail_image"))
        checks["pinned_comment"] = bool(pkg.get("commenter"))
    passed = sum(1 for k, val in checks.items() if val is True or (isinstance(val, str) and not val.startswith("0/")))
    return {"episode_id": ep_id, "checks": checks, "score": f"{passed}/{len(checks)}", "props_found": props is not None}


# ============ FLYWHEEL ============
@router.get("/patterns")
async def get_patterns(limit: int = 10):
    return {"patterns": await db.top_patterns(limit)}


@router.get("/episodes/{ep_id}/publish-package")
async def publish_package(ep_id: str):
    """The ready-to-upload bundle: video + full metadata. One-click publish once the channel + OAuth exist."""
    v = None
    for vid in await db.list_videos():
        if vid.get("id") == ep_id:
            v = vid; break
    if not v:
        raise HTTPException(404, "episode not found")
    import re
    arts = v.get("artifacts", {}) or {}
    pkg = arts.get("package", {})
    def _clean(t):  # strip leading "1. " / "- " / quotes the titler might emit
        return re.sub(r'^\s*(?:\d+[.)]\s*|[-•]\s*)', '', (t or "").strip()).strip('"').strip()
    titles = [_clean(t) for t in (pkg.get("titler") or "").splitlines() if t.strip()]
    video = arts.get("assembly_path")
    has_pkg = bool(titles and pkg.get("seo"))
    return {
        "episode_id": ep_id,
        "video_path": video,
        "title": (titles[0] if titles else v.get("title", "")),
        "title_alternates": titles[1:6],
        "description": pkg.get("seo", ""),
        "thumbnail": pkg.get("thumbnail_image"),
        "pinned_comment": pkg.get("commenter", ""),
        "duration": v.get("duration"),
        "ready": bool(video and has_pkg),  # truly publish-ready = rendered video + full metadata package
        "blockers": _publish_blockers(),
    }


def _publish_blockers():
    import services.abn_youtube as yt
    b = []
    if not yt.is_configured():
        b.append("YouTube OAuth not configured (set YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN)")
        b.append("YouTube channel @agenticbuildernews not yet created (handle confirmed free)")
    return b


@router.post("/episodes/{ep_id}/publish")
async def publish_episode(ep_id: str, body: dict = Body(default={})):
    """The publish flip — uploads an episode to YouTube. Dormant until OAuth creds exist."""
    import services.abn_youtube as yt
    if not yt.is_configured():
        return {"ok": False, "blocked": True, "blockers": _publish_blockers()}
    pkg = await publish_package(ep_id)  # reuse the package builder
    if not pkg.get("ready"):
        return {"ok": False, "error": "episode not publish-ready", "package": pkg}
    res = await asyncio.to_thread(yt.upload, pkg, body.get("privacy", "private"))
    if res.get("ok"):
        await db.update_video(ep_id, {"stage": "live", "artifacts": {"yt_url": res["url"], "yt_video_id": res["video_id"]}})
        factory.BUS.emit("publisher", "episode.published", f"LIVE: {res['url']}", episode_id=ep_id, artifact_url=res["url"])
    return res


@router.post("/gc")
async def garbage_collect(body: dict = Body(default={})):
    """Prune board cruft: standalone segment cards (they live inside an episode's timeline anyway)
    and archive old scheduled/published episodes. Keeps the board legible + the DB lean."""
    vids = await db.list_videos()
    pruned = 0
    keep_recent = body.get("keep_recent_episodes", 12)
    eps = sorted([v for v in vids if v.get("kind") == "episode"], key=lambda x: x.get("created_at", 0), reverse=True)
    archive_ep_ids = {e["id"] for e in eps[keep_recent:]}
    import time as _t
    now = _t.time(); STALE = 2 * 3600
    midstages = ("scripting", "voice_visuals", "assembly", "bundling", "narrative", "scouting", "scoring")
    for v in vids:
        kind, stage = v.get("kind"), v.get("stage")
        stale_inflight = (kind == "episode" and stage in midstages and now - v.get("created_at", now) > STALE)
        # segment cards = noise; rejected 'revision' = spent; old scheduled/live past window;
        # mid-production episodes >2h old = crashed/interrupted, never finished
        if (kind == "segment"
                or (v.get("id") in archive_ep_ids and stage in ("scheduled", "live"))
                or (kind == "episode" and stage == "revision")
                or stale_inflight):
            await db.delete_video(v["id"]); pruned += 1
    # also free disk: purge spent intermediates + trim old episodes if low (the heavy disk hog)
    freed_mb = 0
    try:
        freed_mb = factory.purge_disk(intermediate_age_s=body.get("intermediate_age_s", 600))
    except Exception:
        pass
    factory.BUS.emit("system", "gc", f"pruned {pruned} board cards + freed {freed_mb}MB disk")
    return {"pruned": pruned, "remaining": len(vids) - pruned, "freed_mb": freed_mb}


@router.get("/memory")
async def get_memory():
    try:
        import services.abn_memory as mem
        return mem.stats()
    except Exception as e:
        return {"error": str(e)}


@router.get("/workshop")
async def workshop_feed(limit: int = 8):
    """Workshop view data: recent episodes with their REAL work product — title, thumbnail, per-segment
    scripts, format, duration — so you can actually READ and judge the content, not just watch status."""
    vids = await db.list_videos()
    eps = [v for v in vids if v.get("kind") == "episode" and (v.get("timeline", {}) or {}).get("segments")]
    eps.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    out = []
    for e in eps[:limit]:
        arts = e.get("artifacts", {}) or {}
        pkg = arts.get("package", {}) or {}
        tl = e.get("timeline", {}).get("segments", [])
        titles = [t.strip() for t in (pkg.get("titler") or "").splitlines() if t.strip()]
        out.append({
            "id": e["id"], "stage": e.get("stage"), "format": e.get("format"),
            "duration": round(e.get("duration", 0)),
            "title": titles[0] if titles else e.get("title", ""),
            "thumbnail": pkg.get("thumbnail_image"),
            # prefer the local mp4 if still on disk, else the R2 cloud URL (survives disk GC)
            "video": arts.get("assembly_path") or arts.get("cloud_url"),
            "cloud_url": arts.get("cloud_url"),
            "segments": [{"title": s.get("title", ""), "script": s.get("script", "")} for s in tl],
            "seo": (pkg.get("seo") or "")[:600],
            "pinned_comment": pkg.get("commenter", ""),
        })
    return {"episodes": out}


@router.get("/stats")
async def get_stats():
    vids = await db.list_videos()
    by_stage = {s: 0 for s in db.STAGES}
    for v in vids:
        by_stage[v.get("stage", "idea")] = by_stage.get(v.get("stage", "idea"), 0) + 1
    jobs = await db.list_jobs()
    return {
        "total": len(vids),
        "by_stage": by_stage,
        "active_jobs": len([j for j in jobs if j["status"] in ("queued", "running")]),
        "by_lane": {l: len([v for v in vids if v.get("lane") == l]) for l in ("today", "week", "backlog")},
    }


# ============ EDITOR BAY V2 — commanded timeline core ============
def _editor_timeline_store() -> editor_timeline.TimelineStore:
    return editor_timeline.TimelineStore(db.ASSETS_DIR / "editor_timelines")


def _editor_render_dir() -> Path:
    path = db.ASSETS_DIR / "editor_renders"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _editor_render_output(project_id: str, suffix: str) -> Path:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in project_id).strip("_")
    return _editor_render_dir() / f"{safe_id or 'project'}{suffix}"


@router.post("/editor-timelines", status_code=201)
async def editor_timeline_create(body: dict = Body(...)):
    project_id = body.get("projectId")
    if not project_id:
        raise HTTPException(status_code=400, detail="projectId is required")
    project = editor_timeline.new_project(
        project_id,
        source_episode_id=body.get("sourceEpisodeId"),
        title=body.get("title", ""),
        fps=int(body.get("fps") or 30),
        width=int(body.get("width") or 1920),
        height=int(body.get("height") or 1080),
    )
    return _editor_timeline_store().save(project)


@router.post("/editor-timelines/{project_id}/import-abn", status_code=201)
async def editor_timeline_import_abn(project_id: str, body: dict = Body(...)):
    source = body.get("timeline")
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="timeline object is required")
    project = editor_timeline.project_from_abn_timeline(
        project_id,
        source,
        source_episode_id=body.get("sourceEpisodeId"),
    )
    return _editor_timeline_store().save(project)


@router.get("/editor-timelines/{project_id}")
async def editor_timeline_load(project_id: str):
    try:
        return _editor_timeline_store().load(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")


@router.post("/editor-timelines/{project_id}/commands")
async def editor_timeline_command(project_id: str, command: dict = Body(...)):
    try:
        return _editor_timeline_store().apply_command(project_id, command)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_timeline.RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except editor_timeline.CommandValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============ EDITOR BAY V2 — render backend spike ============
@router.get("/editor-render/capabilities")
async def editor_render_capabilities():
    return editor_render.detect_render_backends()


@router.post("/editor-render/{project_id}/render")
async def editor_render_project(project_id: str):
    store = _editor_timeline_store()
    try:
        project = store.load(project_id)
        renderer = editor_render.choose_renderer(_editor_render_dir(), asset_root=db.ASSETS_DIR)
        result = renderer.render(project, output_path=_editor_render_output(project_id, ".mp4"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_render.RenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    project.setdefault("renderCache", {})["video"] = result
    store.save(project)
    return result


@router.post("/editor-render/{project_id}/frame")
async def editor_render_frame(project_id: str, body: dict = Body(default_factory=dict)):
    store = _editor_timeline_store()
    try:
        at = max(0.0, float(body.get("at", 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="at must be numeric")

    try:
        project = store.load(project_id)
        renderer = editor_render.choose_renderer(_editor_render_dir(), asset_root=db.ASSETS_DIR)
        result = renderer.render_frame(
            project,
            at=at,
            output_path=_editor_render_output(project_id, f"_{at:.2f}.png"),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_render.RenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    project.setdefault("renderCache", {}).setdefault("frames", {})[f"{at:.2f}"] = result
    store.save(project)
    return result


# ============ EDITOR BAY — review + visual/temporal edit notes ============
# A review scratchpad (NOT an NLE): the operator scrubs the rendered episode, sees the atomic
# timeline layers (cards/VO/captions/lower-thirds/b-roll) as tracks, and drops notes rooted in
# visual + temporal context (frame drawings + track pins) for the editors/factory to act on.
import json as _json
from pathlib import Path as _Path

_REVIEW_NOTES = db.ASSETS_DIR / "review_notes.json"


def _load_review_notes() -> dict:
    if not _REVIEW_NOTES.exists():
        return {}
    try:
        return _json.loads(_REVIEW_NOTES.read_text())
    except Exception:
        return {}


def _save_review_notes(data: dict) -> None:
    # atomic write (tmp + rename) per the storage SOP — never leave a partial notes file
    tmp = _REVIEW_NOTES.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, indent=2, default=str))
    tmp.replace(_REVIEW_NOTES)


async def _find_video(ep_id: str) -> dict | None:
    for v in await db.list_videos():
        if v.get("id") == ep_id or v.get("id", "").startswith(ep_id):
            return v
    return None


@router.get("/editor/{ep_id}")
async def editor_load(ep_id: str):
    """One call → everything the editor bay needs: the rendered video URL, the atomic timeline
    (segments → shots/cards/captions/lower-thirds/b-roll, all time-aligned), and existing notes."""
    v = await _find_video(ep_id)
    if not v:
        raise HTTPException(status_code=404, detail="episode not found")
    rid = v.get("id", ep_id)
    arts = v.get("artifacts", {}) or {}
    video_url = arts.get("assembly_path") or f"/agenticnews-assets/{rid}_episode.mp4"
    # Prefer the FULL on-disk timeline.json (per-segment shots[]/wordTimestamps[]/lowerThirds[] — the
    # atomic layers the editor bay renders as tracks). The DB copy is trimmed to keep rows small.
    timeline = v.get("timeline", {}) or {}
    _tl_file = db.ASSETS_DIR / f"{rid}_timeline.json"
    if _tl_file.exists():
        try:
            full = _json.loads(_tl_file.read_text())
            if full.get("segments"):
                timeline = full
        except Exception:
            pass
    notes = _load_review_notes().get(rid, [])
    return {
        "epId": rid,
        "title": v.get("title", ""),
        "stage": v.get("stage", ""),
        "duration": v.get("duration", timeline.get("totalSec", 0)),
        "videoUrl": video_url,
        "timeline": timeline,        # segments[] each with shots[], wordTimestamps[], lowerThirds[]
        "notes": notes,              # [{id, t, kind:'frame'|'pin', track?, text, frameImg?, createdAt}]
    }


@router.post("/editor/{ep_id}/notes")
async def editor_save_note(ep_id: str, note: dict = Body(...)):
    """Append (or upsert by id) one review note for this episode. The editors/factory read the queue."""
    v = await _find_video(ep_id)
    rid = v.get("id", ep_id) if v else ep_id
    data = _load_review_notes()
    notes = data.get(rid, [])
    nid = note.get("id")
    if nid and any(n.get("id") == nid for n in notes):
        notes = [note if n.get("id") == nid else n for n in notes]
    else:
        note.setdefault("id", f"note_{len(notes)+1}_{int(v.get('updated_at', 0)) if v else 0}")
        notes.append(note)
    data[rid] = notes
    _save_review_notes(data)
    return {"ok": True, "epId": rid, "count": len(notes), "note": note}


@router.delete("/editor/{ep_id}/notes/{note_id}")
async def editor_delete_note(ep_id: str, note_id: str):
    v = await _find_video(ep_id)
    rid = v.get("id", ep_id) if v else ep_id
    data = _load_review_notes()
    before = len(data.get(rid, []))
    data[rid] = [n for n in data.get(rid, []) if n.get("id") != note_id]
    _save_review_notes(data)
    return {"ok": True, "epId": rid, "removed": before - len(data[rid])}


# ── EDITOR REFINER — apply per-asset edits + re-render ─────────────────────────────────────────
# Each edit targets one timeline shot (by src or id) with an action:
#   {"action":"delete", "src": "..."}                      → drop the shot from timeline.json
#   {"action":"retext", "src": "..._number.png",
#       "value":"60%","label":"cheaper"}                   → regenerate that card PNG with new text
#   {"action":"retext", "src":"..._hook.png","text":"..."} → regenerate hook/quote/statement card
# After applying, the episode re-renders from the modified timeline (only the touched assets change).
import asyncio as _asyncio
import re as _re2

_CARD_KIND = _re2.compile(r'_(number|vs|quote|diagram|hook)\.png$')
# hold background re-render tasks so they aren't GC'd mid-flight (the fire-and-forget pitfall)
_RERENDER_TASKS: set = set()


def _regen_card(src: str, edit: dict) -> bool:
    """Regenerate ONE designed-card PNG in place from the edit's new text. Returns True on success."""
    name = Path(src).name
    m = _CARD_KIND.search(name)
    if not m:
        return False
    kind = m.group(1)
    stem = name[: m.start()]          # 'ep_x_s1_v2sc2' — the card generator writes back to this stem
    cards = factory._v2cards
    A, F = factory.ASSETS, factory._FONTS_DIR
    txt = (edit.get("text") or "").strip()
    try:
        if kind == "number":
            cards.number_card(edit.get("value", txt), edit.get("label", ""), stem, A, F)
        elif kind == "vs":
            cards.vs_card(edit.get("left", txt), edit.get("right", ""), stem, A, F)
        elif kind == "quote":
            cards.quote_card(txt, stem, A, F)
        elif kind == "diagram":
            steps = [s.strip() for s in (edit.get("steps") or txt.split("\n")) if s.strip()]
            cards.diagram_card(edit.get("title", "How it works"), steps or [txt], stem, A, F)
        else:  # hook / statement
            cards.hook_card(txt, stem, A, F, accent=cards.BRAND_CYAN if edit.get("statement") else cards.BRAND_RED)
        return True
    except Exception:
        return False


@router.post("/editor/{ep_id}/apply")
async def editor_apply(ep_id: str, body: dict = Body(...)):
    """Apply the operator's edits to the timeline + regenerate touched cards, then re-render the episode.
    body = {"edits":[{action, src/id, ...}]}. Returns immediately; re-render runs in the background."""
    v = await _find_video(ep_id)
    if not v:
        raise HTTPException(status_code=404, detail="episode not found")
    rid = v.get("id", ep_id)
    tl_file = db.ASSETS_DIR / f"{rid}_timeline.json"
    if not tl_file.exists():
        raise HTTPException(status_code=404, detail="timeline not found")
    timeline = _json.loads(tl_file.read_text())
    edits = body.get("edits", [])
    applied = {"deleted": 0, "retext": 0, "skipped": 0}

    for e in edits:
        target = e.get("src") or e.get("id")
        action = e.get("action")
        if not target or not action:
            applied["skipped"] += 1
            continue
        for seg in timeline.get("segments", []):
            shots = seg.get("shots", [])
            if action == "delete":
                n0 = len(shots)
                seg["shots"] = [s for s in shots if s.get("src") != target and s.get("id") != target]
                applied["deleted"] += n0 - len(seg["shots"])
            elif action == "retext":
                for s in shots:
                    if s.get("src") == target or s.get("id") == target:
                        if _regen_card(s.get("src", ""), e):
                            applied["retext"] += 1
                        else:
                            applied["skipped"] += 1

    # atomic write of the modified timeline
    tmp = tl_file.with_suffix(".json.tmp"); tmp.write_text(_json.dumps(timeline)); tmp.replace(tl_file)

    # re-render in the background (the re-render guard reuses nothing here — timeline changed)
    import logging as _logging
    _log = _logging.getLogger("editor")
    async def _rerender():
        try:
            _log.info("editor re-render START %s (%d edits)", rid, len(edits))
            factory.BUS.emit("operator", "editor.apply", f"applying {len(edits)} edits + re-rendering", episode_id=rid)
            await factory._render_remotion(rid, timeline, force=True)  # force: timeline changed, never reuse
            _log.info("editor re-render DONE %s", rid)
            factory.BUS.emit("operator", "editor.rerendered", "episode re-rendered from operator edits", episode_id=rid)
        except Exception as ex:
            import traceback
            _log.error("editor re-render FAILED %s: %s", rid, traceback.format_exc()[-500:])
            factory.BUS.emit("operator", "error", f"re-render failed: {str(ex)[:160]}", episode_id=rid)
        finally:
            _RERENDER_TASKS.discard(_t)
    # RETAIN the task — a bare create_task with no reference can be garbage-collected before it runs.
    _t = _asyncio.create_task(_rerender())
    _RERENDER_TASKS.add(_t)

    return {"ok": True, "epId": rid, "applied": applied, "rerendering": True}
