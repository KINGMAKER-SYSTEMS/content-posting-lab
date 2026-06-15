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
import copy
import shlex
import asyncio
import subprocess
import shutil
import textwrap
import urllib.request
import urllib.parse
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse

import services.agenticnews as db
import services.abn_factory as factory
import services.abn_assets as abn_assets
import services.openshot_bridge as openshot_bridge
import services.editor_render as editor_render
import services.editor_timeline as editor_timeline
from fastapi.responses import StreamingResponse

router = APIRouter()
EDITOR_TITLE_ASSET_VERSION = 1
EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION = (
    os.getenv("EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", "0") == "1"
)


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
    # Episode-scoped VO (name == 'ep_<hex>[_sN]') routes through the asset gateway so it
    # lands in {ep_id}/audio/ instead of colliding in the flat ASSETS_DIR root — where the
    # glob GC has eaten original VO wavs. A bare default ('vo') or a video id (no ep_ prefix)
    # has nowhere to be scoped, so fall back to the legacy flat path for those ad-hoc renders.
    try:
        out = abn_assets.asset_path_from_slug(name, "voice")
    except abn_assets.AssetPathError:
        out = db.ASSETS_DIR / f"{name}.wav"
    # Pocket-TTS built-in English voice only — the channel's single narrator (no clone, no cloud).
    cmd = f'pocket-tts generate --text {shlex.quote(text)} --output-path {shlex.quote(str(out))} --quiet'
    code, log = await _sh(cmd, timeout=600)
    if code != 0 or not out.exists():
        raise HTTPException(500, f"tts failed: {log[-500:]}")
    # Carry the full subpath (ep_x/audio/...) so the URL resolves back to the SAME file —
    # not a basename that collides across episodes in the flat root.
    rel = f"/agenticnews-assets/{out.relative_to(db.ASSETS_DIR)}"
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
    # Episode-scoped cards (name == 'ep_<hex>[_sN]') route through the asset gateway so
    # they land in {ep_id}/css/ instead of colliding in the flat ASSETS_DIR root. A bare
    # default ('card') or a video id (no ep_ prefix) has nowhere to be scoped, so fall
    # back to the legacy flat path for those ad-hoc/test renders.
    try:
        out = abn_assets.asset_path_from_slug(name, "card")
    except abn_assets.AssetPathError:
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
    # Carry the full subpath (ep_x/css/...) so the URL resolves back to the SAME file —
    # not a basename that collides across episodes in the flat root.
    rel = f"/agenticnews-assets/{out.relative_to(db.ASSETS_DIR)}"
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
    # Resolve the FULL subpath off the /agenticnews-assets/ URL (ep_x/css/sN_card.png),
    # not just the basename — otherwise scoped cards/VO read the wrong file (or nothing)
    # when two episodes share a card name.
    cardf = _asset_path_from_url(card)
    vof = _asset_path_from_url(vo)
    # Episode-scoped assemblies (name == 'ep_<hex>[_sN]') route through the asset gateway so
    # they land in {ep_id}/renders/ instead of the flat ASSETS_DIR root. A bare default ('clip')
    # or a video id (no ep_ prefix) falls back to the legacy flat path for those ad-hoc renders.
    try:
        out = abn_assets.asset_path_from_slug(name, "assembled")
    except abn_assets.AssetPathError:
        out = db.ASSETS_DIR / f"{name}_assembled.mp4"
    cmd = (
        f'ffmpeg -y -loop 1 -i {shlex.quote(str(cardf))} -i {shlex.quote(str(vof))} '
        f'-filter_complex "[0:v]scale=1920:1080,zoompan=z=\'min(zoom+0.0006,1.1)\':d=99999:s=1920x1080:fps=25[v]" '
        f'-map "[v]" -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest {shlex.quote(str(out))}'
    )
    code, log = await _sh(cmd, timeout=300)
    if code != 0 or not out.exists():
        raise HTTPException(500, f"assemble failed: {log[-600:]}")
    # Carry the full subpath (ep_x/renders/...) so the URL resolves back to the SAME file.
    rel = f"/agenticnews-assets/{out.relative_to(db.ASSETS_DIR)}"
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
        try:
            with open(props_path) as _f:
                props = _json.load(_f)
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


def _reject_demo_editor_project(project_id: str) -> None:
    if project_id.startswith("demo"):
        raise HTTPException(
            status_code=400,
            detail="demo editor timelines are disabled; open a real ABN episode id",
        )


def _timeline_file_for_episode(episode_id: str) -> Path:
    # Read the timeline the factory ACTUALLY wrote: the schema path {ep_id}/timeline.json
    # (via the gateway), not the flat {ep_id}_timeline.json legacy name — which now only
    # exists as a back-compat symlink the migration left behind. Fall back to the flat path
    # only for a non-episode id (gateway returns None) or an episode not yet migrated to the
    # schema, so the cutover stays read-safe.
    schema = abn_assets.episode_singleton_path(episode_id, "timeline")
    flat = db.ASSETS_DIR / f"{episode_id}_timeline.json"
    if schema is not None and (schema.exists() or not flat.exists()):
        return schema
    return flat


async def _load_real_abn_timeline(project_id: str) -> tuple[str, dict]:
    _reject_demo_editor_project(project_id)
    timeline_path = _timeline_file_for_episode(project_id)

    if timeline_path.exists():
        try:
            timeline = json.loads(timeline_path.read_text())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"episode timeline is unreadable: {project_id}") from exc
        if timeline.get("segments"):
            return project_id, timeline

    video = await _find_video(project_id)
    episode_id = video.get("id", project_id) if video else project_id
    if episode_id != project_id:
        timeline_path = _timeline_file_for_episode(episode_id)
        if timeline_path.exists():
            try:
                timeline = json.loads(timeline_path.read_text())
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"episode timeline is unreadable: {episode_id}") from exc
            if timeline.get("segments"):
                return episode_id, timeline

    timeline = (video or {}).get("timeline") or {}
    if timeline.get("segments"):
        return episode_id, timeline

    raise HTTPException(status_code=404, detail="real ABN episode timeline not found")


async def _load_or_import_real_editor_project(project_id: str) -> dict:
    _reject_demo_editor_project(project_id)
    store = _editor_timeline_store()
    try:
        project = store.load(project_id)
        project, changed = _sanitize_render_cache(project)
        try:
            episode_id, timeline = await _load_real_abn_timeline(project_id)
        except HTTPException:
            episode_id, timeline = project.get("sourceEpisodeId") or project_id, None
        if timeline:
            if _needs_abn_import_migration(project):
                project = editor_timeline.reimport_abn_timeline_preserving_commands(
                    project,
                    timeline,
                    source_episode_id=episode_id,
                )
                changed = True
        return store.save(project) if changed else project
    except FileNotFoundError:
        pass

    episode_id, timeline = await _load_real_abn_timeline(project_id)
    project = editor_timeline.project_from_abn_timeline(
        episode_id,
        timeline,
        source_episode_id=episode_id,
    )
    return store.save(project)


def _needs_abn_import_migration(project: dict) -> bool:
    return (
        (project.get("metadata") or {}).get("abnImportVersion")
        != editor_timeline.ABN_IMPORT_VERSION
    )


def _materialize_editor_sources(episode_id: str, timeline: dict) -> list[dict[str, str]]:
    return _plan_editor_source_materialization(episode_id, timeline, materialize=True)


def _plan_editor_source_materialization(
    episode_id: str,
    timeline: dict,
    *,
    materialize: bool = False,
) -> list[dict[str, str]]:
    # Read the render the factory wrote at the schema path {ep_id}/renders/episode.mp4 (via the
    # gateway), not the flat {ep_id}_episode.mp4 legacy name (now a back-compat symlink only).
    # Non-episode id -> gateway returns None -> flat fallback; un-migrated episode -> flat fallback.
    _schema_video = abn_assets.episode_singleton_path(episode_id, "episode")
    if _schema_video is not None and _schema_video.exists():
        episode_video = _schema_video
    else:
        episode_video = db.ASSETS_DIR / f"{episode_id}_episode.mp4"
    if not episode_video.exists():
        return []

    materialized: list[dict[str, str]] = []
    cursor = 0.0
    planned_copy_targets: set[str] = set()
    for segment in timeline.get("segments") or []:
        segment_duration = float(segment.get("durationSec") or 0)
        if segment_duration <= 0:
            continue

        vo = ((segment.get("audio") or {}).get("vo") or {})
        if vo.get("src"):
            target = _asset_path_from_url(str(vo["src"]))
            if not target.exists():
                if EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION:
                    if materialize:
                        _extract_audio_window(episode_video, target, start=cursor, duration=segment_duration)
                    materialized.append({
                        "type": "audio",
                        "path": str(target),
                        "source": str(episode_video),
                        "provenance": "derived_from_flattened_episode",
                        **({} if materialize else {"status": "available", "action": "extract"}),
                    })
                else:
                    materialized.append({
                        "type": "audio",
                        "path": str(target),
                        "source": str(episode_video),
                        "status": "blocked",
                        "provenance": "would_derive_from_flattened_episode",
                        "reason": "flattened source extraction is disabled",
                    })

        shots = segment.get("shots") or []
        demo_groups: dict[str, list[tuple[int, dict]]] = {}
        for shot_index, shot in enumerate(shots):
            src = str(shot.get("src") or "")
            if not src:
                continue
            target = _asset_path_from_url(src)
            if target.exists():
                continue
            if target.name.endswith("_src.png"):
                fallback = target.with_name(target.name.replace("_src.png", "_card.png"))
                target_key = str(target)
                if fallback.exists() and target_key not in planned_copy_targets:
                    planned_copy_targets.add(target_key)
                    if materialize:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(fallback, target)
                    materialized.append({
                        "type": "image",
                        "path": str(target),
                        "source": str(fallback),
                        "provenance": "copied_from_existing_layer_parent",
                        **({} if materialize else {"status": "available", "action": "copy"}),
                    })
            elif target.name.endswith("_demo.mp4"):
                demo_groups.setdefault(src, []).append((shot_index, shot))

        for src, source_shots in demo_groups.items():
            target = _asset_path_from_url(src)
            if target.exists() or not source_shots:
                continue
            if not EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION:
                materialized.append({
                    "type": "video",
                    "path": str(target),
                    "source": str(episode_video),
                    "status": "blocked",
                    "provenance": "would_derive_from_flattened_episode",
                    "reason": "flattened source extraction is disabled",
                })
                continue
            base_offset = min(
                max(0.0, float(shot.get("startSec") or 0) - float(shot.get("clipStartSec") or 0))
                for _shot_index, shot in source_shots
            )
            window_end = max(
                float(shot.get("clipStartSec") or 0)
                + _abn_shot_duration(segment, shots, shot_index, segment_duration)
                for shot_index, shot in source_shots
            )
            if materialize:
                _extract_video_window(
                    episode_video,
                    target,
                    start=cursor + base_offset,
                    duration=max(0.1, window_end),
                )
            materialized.append({
                "type": "video",
                "path": str(target),
                "source": str(episode_video),
                "provenance": "derived_from_flattened_episode",
                **({} if materialize else {"status": "available", "action": "extract"}),
            })

        cursor += segment_duration
    return materialized


def _asset_path_from_url(src: str) -> Path:
    if src.startswith("/agenticnews-assets/"):
        return db.ASSETS_DIR / src.removeprefix("/agenticnews-assets/")
    return Path(src)


async def _load_editor_project_for_asset_health(project_id: str) -> tuple[dict, str | None, dict | None, bool]:
    _reject_demo_editor_project(project_id)
    store = _editor_timeline_store()
    try:
        project = store.load(project_id)
        try:
            episode_id, timeline = await _load_real_abn_timeline(project.get("sourceEpisodeId") or project_id)
        except HTTPException:
            episode_id, timeline = project.get("sourceEpisodeId") or project_id, None
        return project, episode_id, timeline, False
    except FileNotFoundError:
        episode_id, timeline = await _load_real_abn_timeline(project_id)
        project = editor_timeline.project_from_abn_timeline(
            episode_id,
            timeline,
            source_episode_id=episode_id,
        )
        return project, episode_id, timeline, True


def _editor_asset_health(project: dict, materialization_plan: list[dict[str, str]] | None = None) -> dict:
    assets = project.get("assets") or {}
    clips = project.get("clips") or {}
    tracks = project.get("tracks") or {}
    clip_ids_by_asset: dict[str, list[str]] = {}
    enabled_clip_ids_by_asset: dict[str, list[str]] = {}
    missing_clip_assets: list[dict[str, str]] = []
    missing_clip_tracks: list[dict[str, str]] = []

    for clip in clips.values():
        clip_id = str(clip.get("id") or "")
        asset_id = str(clip.get("assetId") or "")
        track_id = str(clip.get("trackId") or "")
        if asset_id:
            clip_ids_by_asset.setdefault(asset_id, []).append(clip_id)
        if clip.get("enabled", True):
            if asset_id:
                enabled_clip_ids_by_asset.setdefault(asset_id, []).append(clip_id)
            if asset_id not in assets:
                missing_clip_assets.append({"clipId": clip_id, "assetId": asset_id})
            if track_id not in tracks:
                missing_clip_tracks.append({"clipId": clip_id, "trackId": track_id})

    checked_files = 0
    missing_files: list[dict[str, object]] = []
    bad_sources: list[dict[str, str]] = []
    seen_missing: set[tuple[str, str]] = set()
    for asset_id, asset in assets.items():
        src = str(asset.get("src") or "")
        if not src:
            bad_sources.append({"assetId": str(asset_id), "reason": "empty src"})
            continue
        if src.startswith(("http://", "https://", "data:")):
            bad_sources.append({"assetId": str(asset_id), "src": src, "reason": "non-local src"})
            continue
        path = _asset_path_from_url(src)
        checked_files += 1
        if path.exists():
            continue
        key = (str(asset_id), str(path))
        if key in seen_missing:
            continue
        seen_missing.add(key)
        missing_files.append({
            "assetId": str(asset_id),
            "type": str(asset.get("type") or ""),
            "src": src,
            "file": str(path),
            "clipIds": clip_ids_by_asset.get(str(asset_id), []),
            "enabledClipIds": enabled_clip_ids_by_asset.get(str(asset_id), []),
        })

    plan = materialization_plan or []
    blocked_materializations = [item for item in plan if item.get("status") == "blocked"]
    copy_candidates = [
        item
        for item in plan
        if item.get("status") == "available" and item.get("provenance") == "copied_from_existing_layer_parent"
    ]
    derivative_materializations = [
        item
        for item in plan
        if "flattened_episode" in str(item.get("provenance") or "")
    ]
    unique_missing_files = sorted({str(item["file"]) for item in missing_files})
    render_blockers = [
        item
        for item in missing_files
        if item.get("enabledClipIds")
    ] + missing_clip_assets + missing_clip_tracks
    ok = not (bad_sources or missing_files or missing_clip_assets or missing_clip_tracks or blocked_materializations)
    return {
        "ok": ok,
        "renderable": not render_blockers and not blocked_materializations,
        "projectId": project.get("projectId"),
        "sourceEpisodeId": project.get("sourceEpisodeId"),
        "revision": project.get("revision"),
        "assetCount": len(assets),
        "clipCount": len(clips),
        "trackCount": len(tracks),
        "checkedFiles": checked_files,
        "missingFiles": missing_files,
        "uniqueMissingFiles": unique_missing_files,
        "badSources": bad_sources,
        "missingClipAssets": missing_clip_assets,
        "missingClipTracks": missing_clip_tracks,
        "materializationPlan": plan,
        "blockedMaterializations": blocked_materializations,
        "copyCandidates": copy_candidates,
        "derivativeMaterializations": derivative_materializations,
    }


def _editor_load_mutation_reasons(project: dict, *, imported: bool, materialization_plan: list[dict[str, str]]) -> list[str]:
    reasons: list[str] = []
    if imported:
        reasons.append("would import ABN timeline into editor store")
    _, cache_changed = _sanitize_render_cache(copy.deepcopy(project))
    if cache_changed:
        reasons.append("would sanitize render cache")
    if _needs_abn_import_migration(project):
        reasons.append("would migrate ABN import version")
    return reasons


def _abn_shot_duration(segment: dict, shots: list[dict], shot_index: int, segment_duration: float) -> float:
    shot = shots[shot_index]
    if shot.get("durationSec") not in {None, ""}:
        return max(0.001, float(shot.get("durationSec") or 0))
    start = float(shot.get("startSec") or 0)
    if shot.get("endSec") not in {None, ""}:
        return max(0.001, float(shot.get("endSec") or 0) - start)
    future_starts = [
        float(next_shot.get("startSec") or 0)
        for next_shot in shots[shot_index + 1 :]
        if next_shot.get("src") and float(next_shot.get("startSec") or 0) > start
    ]
    end = min(future_starts) if future_starts else segment_duration
    return max(0.001, end - start)


def _extract_audio_window(source: Path, target: Path, *, start: float, duration: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _extract_video_window(source: Path, target: Path, *, start: float, duration: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _materialize_editor_title_assets(project: dict) -> tuple[list[dict[str, str]], bool]:
    materialized: list[dict[str, str]] = []
    title_dir = db.ASSETS_DIR / "editor_title_assets"
    title_dir.mkdir(parents=True, exist_ok=True)
    width = int(project.get("width") or 1920)
    height = int(project.get("height") or 1080)
    project_id = str(project.get("projectId") or "editor")
    metadata = project.setdefault("metadata", {})
    stale_title_version = metadata.get("titleAssetVersion") != EDITOR_TITLE_ASSET_VERSION
    output_changed = False

    for asset in (project.get("assets") or {}).values():
        if asset.get("type") != "title":
            continue
        current_src = str(asset.get("src") or "")
        current_path = _asset_path_from_url(current_src) if current_src else None
        if current_path and current_path.exists() and not stale_title_version:
            continue

        asset_id = str(asset.get("id") or "title")
        out = title_dir / f"{_safe_file_stem(project_id)}_{_safe_file_stem(asset_id)}.png"
        metadata = asset.get("metadata") or {}
        text = str(metadata.get("text") or asset_id).strip()
        source_url = str(metadata.get("sourceUrl") or "").strip()
        _render_title_asset_png(out, text=text, source_url=source_url, width=width, height=height)
        asset["src"] = f"/agenticnews-assets/editor_title_assets/{out.name}"
        materialized.append({"type": "title", "assetId": asset_id, "path": str(out)})
        output_changed = True

    if stale_title_version and any(asset.get("type") == "title" for asset in (project.get("assets") or {}).values()):
        project.setdefault("metadata", {})["titleAssetVersion"] = EDITOR_TITLE_ASSET_VERSION
        output_changed = True

    return materialized, output_changed


def _render_title_asset_png(path: Path, *, text: str, source_url: str, width: int, height: int) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"title asset renderer unavailable: {exc}") from exc

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    headline_font = _load_title_font(ImageFont, 54, bold=True)
    source_font = _load_title_font(ImageFont, 28, bold=False)
    max_text_width = int(width * 0.62)
    headline_lines = _wrap_title_text(draw, text, headline_font, max_text_width, max_lines=2)
    source_label = _source_label(source_url)
    source_lines = _wrap_title_text(draw, source_label, source_font, max_text_width, max_lines=1) if source_label else []

    padding_x = 36
    padding_y = 26
    gap = 12 if source_lines else 0
    headline_height = sum(_text_size(draw, line, headline_font)[1] for line in headline_lines) + max(0, len(headline_lines) - 1) * 8
    source_height = sum(_text_size(draw, line, source_font)[1] for line in source_lines)
    box_height = padding_y * 2 + headline_height + gap + source_height
    box_width = max(
        620,
        min(
            int(width * 0.74),
            max([_text_size(draw, line, headline_font)[0] for line in headline_lines] + [_text_size(draw, line, source_font)[0] for line in source_lines] + [0])
            + padding_x * 2
            + 18,
        ),
    )
    x = 92
    y = height - box_height - 118
    draw.rounded_rectangle([x, y, x + box_width, y + box_height], radius=24, fill=(8, 13, 22, 218))
    draw.rounded_rectangle([x, y, x + 10, y + box_height], radius=5, fill=(34, 211, 238, 255))

    cursor_y = y + padding_y
    text_x = x + padding_x
    for line in headline_lines:
        draw.text((text_x, cursor_y), line, font=headline_font, fill=(245, 248, 252, 255))
        cursor_y += _text_size(draw, line, headline_font)[1] + 8
    if source_lines:
        cursor_y += gap
        for line in source_lines:
            draw.text((text_x, cursor_y), line, font=source_font, fill=(125, 211, 252, 235))
            cursor_y += _text_size(draw, line, source_font)[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _load_title_font(image_font, size: int, *, bold: bool):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return image_font.truetype(candidate, size=size)
        except Exception:
            continue
    return image_font.load_default(size=size)


def _wrap_title_text(draw, text: str, font, max_width: int, *, max_lines: int) -> list[str]:
    words = textwrap.wrap(text.strip(), width=34) or [text.strip() or " "]
    lines: list[str] = []
    current = ""
    for chunk in " ".join(words).split():
        candidate = f"{current} {chunk}".strip()
        if current and _text_size(draw, candidate, font)[0] > max_width:
            lines.append(current)
            current = chunk
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(lines)) < len(text.strip()):
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return lines or [" "]


def _text_size(draw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _source_label(source_url: str) -> str:
    if not source_url:
        return ""
    parsed = urllib.parse.urlparse(source_url)
    return parsed.netloc or source_url


def _safe_file_stem(value: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    return "_".join(part for part in clean.split("_") if part)[:96] or "asset"


def _sanitize_render_cache(project: dict) -> tuple[dict, bool]:
    next_project, changed = _strip_source_reference_render_cache(project)
    next_project, pruned = _prune_missing_render_cache(next_project)
    next_project, stale = _prune_stale_revision_render_cache(next_project)
    next_project, stamped = _stamp_legacy_render_cache_revisions(next_project)
    return next_project, changed or pruned or stale or stamped


def _strip_source_reference_render_cache(project: dict) -> tuple[dict, bool]:
    video = ((project.get("renderCache") or {}).get("video") or {})
    video_path = str(video.get("video") or "")
    is_source_reference = video.get("backend") == "abn-source" or video_path.endswith("_episode.mp4")
    is_window_cache = float(video.get("start") or 0) > 0
    if not is_window_cache and video.get("duration") is not None:
        project_duration = max(
            [float(c.get("start") or 0) + float(c.get("duration") or 0) for c in (project.get("clips") or {}).values()]
            or [0.0]
        )
        is_window_cache = float(video.get("duration") or 0) + 0.05 < project_duration
    if not is_source_reference and not is_window_cache:
        return project, False
    project = copy.deepcopy(project)
    project.setdefault("renderCache", {}).pop("video", None)
    return project, True


def _prune_missing_render_cache(project: dict) -> tuple[dict, bool]:
    if "renderCache" not in project:
        return project, False
    render_cache = project.get("renderCache") or {}
    if not isinstance(render_cache, dict):
        return project, False

    changed = False
    next_project = copy.deepcopy(project)
    next_cache = next_project.setdefault("renderCache", {})

    video = next_cache.get("video") or {}
    if video and not _render_cache_path_exists(str(video.get("video") or "")):
        next_cache.pop("video", None)
        changed = True

    windows = next_cache.get("windows") or {}
    if isinstance(windows, dict):
        for key, item in list(windows.items()):
            if not _render_cache_path_exists(str((item or {}).get("video") or "")):
                windows.pop(key, None)
                changed = True
        if not windows and "windows" in next_cache:
            next_cache.pop("windows", None)
            changed = True

    frames = next_cache.get("frames") or {}
    if isinstance(frames, dict):
        for key, item in list(frames.items()):
            if not _render_cache_path_exists(str((item or {}).get("frame") or "")):
                frames.pop(key, None)
                changed = True
        if not frames and "frames" in next_cache:
            next_cache.pop("frames", None)
            changed = True

    if not next_cache and "renderCache" in next_project:
        next_project.pop("renderCache", None)
        changed = True

    return (next_project, True) if changed else (project, False)


def _prune_stale_revision_render_cache(project: dict) -> tuple[dict, bool]:
    if "renderCache" not in project:
        return project, False
    render_cache = project.get("renderCache") or {}
    if not isinstance(render_cache, dict):
        return project, False

    current_revision = int(project.get("revision") or 0)
    changed = False
    next_project = copy.deepcopy(project)
    next_cache = next_project.setdefault("renderCache", {})

    video = next_cache.get("video") or {}
    if _render_cache_revision_mismatch(video, current_revision):
        next_cache.pop("video", None)
        changed = True

    windows = next_cache.get("windows") or {}
    if isinstance(windows, dict):
        for key, item in list(windows.items()):
            if _render_cache_revision_mismatch(item or {}, current_revision):
                windows.pop(key, None)
                changed = True
        if not windows and "windows" in next_cache:
            next_cache.pop("windows", None)
            changed = True

    frames = next_cache.get("frames") or {}
    if isinstance(frames, dict):
        for key, item in list(frames.items()):
            if _render_cache_revision_mismatch(item or {}, current_revision):
                frames.pop(key, None)
                changed = True
        if not frames and "frames" in next_cache:
            next_cache.pop("frames", None)
            changed = True

    if not next_cache and "renderCache" in next_project:
        next_project.pop("renderCache", None)
        changed = True

    return (next_project, True) if changed else (project, False)


def _stamp_legacy_render_cache_revisions(project: dict) -> tuple[dict, bool]:
    if "renderCache" not in project:
        return project, False
    render_cache = project.get("renderCache") or {}
    if not isinstance(render_cache, dict):
        return project, False

    current_revision = int(project.get("revision") or 0)
    changed = False
    next_project = copy.deepcopy(project)
    next_cache = next_project.setdefault("renderCache", {})

    video = next_cache.get("video")
    if isinstance(video, dict) and "revision" not in video:
        video["revision"] = current_revision
        changed = True

    windows = next_cache.get("windows") or {}
    if isinstance(windows, dict):
        for item in windows.values():
            if isinstance(item, dict) and "revision" not in item:
                item["revision"] = current_revision
                changed = True

    frames = next_cache.get("frames") or {}
    if isinstance(frames, dict):
        for item in frames.values():
            if isinstance(item, dict) and "revision" not in item:
                item["revision"] = current_revision
                changed = True

    return (next_project, True) if changed else (project, False)


def _refresh_render_cache_revisions(project: dict) -> tuple[dict, bool]:
    render_cache = project.get("renderCache") or {}
    if not isinstance(render_cache, dict):
        return project, False

    current_revision = int(project.get("revision") or 0)
    changed = False
    next_project = copy.deepcopy(project)
    next_cache = next_project.setdefault("renderCache", {})

    entries = [next_cache.get("video")]
    windows = next_cache.get("windows") or {}
    if isinstance(windows, dict):
        entries.extend(windows.values())
    frames = next_cache.get("frames") or {}
    if isinstance(frames, dict):
        entries.extend(frames.values())

    for item in entries:
        if isinstance(item, dict) and item.get("revision") != current_revision:
            item["revision"] = current_revision
            changed = True

    return (next_project, True) if changed else (project, False)


def _render_cache_revision_mismatch(entry: dict, current_revision: int) -> bool:
    if "revision" not in entry:
        return False
    try:
        return int(entry.get("revision")) != current_revision
    except (TypeError, ValueError):
        return True


def _render_cache_path_exists(path: str) -> bool:
    if not path:
        return False
    if path.startswith("http://") or path.startswith("https://"):
        return True
    if path.startswith("/agenticnews-assets/"):
        return (db.ASSETS_DIR / path.removeprefix("/agenticnews-assets/")).exists()
    return Path(path).exists()


def _command_invalidates_render_cache(op: str) -> bool:
    return op in {
        "asset.import",
        "track.create",
        "clip.create",
        "clip.split",
        "clip.unsplit",
        "clip.move",
        "clip.trim",
        "clip.update",
        "clip.hide",
        "clip.show",
        "clip.mute",
        "clip.unmute",
        "clip.transform",
        "clip.opacity",
        "clip.volume",
    }


def _sync_render_cache_after_command(store: editor_timeline.EditorTimelineStore, project: dict, op: str) -> dict:
    if not project.get("renderCache"):
        return project
    if _command_invalidates_render_cache(op):
        project = copy.deepcopy(project)
        project.pop("renderCache", None)
        return store.save(project)
    project, changed = _refresh_render_cache_revisions(project)
    return store.save(project) if changed else project


def _editor_render_dir() -> Path:
    path = db.ASSETS_DIR / "editor_renders"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _editor_render_output(project_id: str, suffix: str) -> Path:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in project_id).strip("_")
    return _editor_render_dir() / f"{safe_id or 'project'}{suffix}"


def _save_render_cache_entry_if_current(
    store: editor_timeline.TimelineStore,
    project: dict,
    *,
    cache_kind: str,
    result: dict,
    key: str | None = None,
) -> dict:
    latest = store.load(project["projectId"])
    base_revision = int(project.get("revision") or 0)
    current_revision = int(latest.get("revision") or 0)
    if current_revision != base_revision:
        return {
            **result,
            "revision": base_revision,
            "cacheSkipped": True,
            "cacheSkipReason": "timeline revision changed during render",
            "currentRevision": current_revision,
        }

    latest = copy.deepcopy(latest)
    entry = {**result, "revision": base_revision}
    if cache_kind == "video":
        latest.setdefault("renderCache", {})["video"] = entry
    elif cache_kind == "window" and key:
        windows = latest.setdefault("renderCache", {}).setdefault("windows", {})
        video_path = str(entry.get("video") or "")
        if video_path:
            for existing_key, existing_entry in list(windows.items()):
                if existing_key != key and str((existing_entry or {}).get("video") or "") == video_path:
                    windows.pop(existing_key, None)
        windows[key] = entry
    elif cache_kind == "frame" and key:
        latest.setdefault("renderCache", {}).setdefault("frames", {})[key] = entry
    store.save(latest)
    return {**result, "revision": base_revision, "cacheSkipped": False}


@router.post("/editor-timelines", status_code=201)
async def editor_timeline_create(body: dict = Body(...)):
    project_id = body.get("projectId")
    if not project_id:
        raise HTTPException(status_code=400, detail="projectId is required")
    _reject_demo_editor_project(project_id)
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
    _reject_demo_editor_project(project_id)
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
    return await _load_or_import_real_editor_project(project_id)


@router.get("/editor-timelines/{project_id}/asset-health")
async def editor_timeline_asset_health(project_id: str):
    try:
        project, episode_id, timeline, imported = await _load_editor_project_for_asset_health(project_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    materialization_plan = (
        _plan_editor_source_materialization(episode_id, timeline, materialize=False)
        if episode_id and timeline
        else []
    )
    health = _editor_asset_health(project, materialization_plan)
    mutation_reasons = _editor_load_mutation_reasons(
        project,
        imported=imported,
        materialization_plan=materialization_plan,
    )
    return {
        **health,
        "importedInMemory": imported,
        "wouldMutateOnLoad": bool(mutation_reasons),
        "wouldMutateOnLoadReasons": mutation_reasons,
    }


@router.post("/editor-timelines/{project_id}/commands")
async def editor_timeline_command(project_id: str, command: dict = Body(...)):
    _reject_demo_editor_project(project_id)
    try:
        store = _editor_timeline_store()
        project = store.apply_command(project_id, command)
        project = _sync_render_cache_after_command(store, project, str(command.get("op") or ""))
        return project
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_timeline.RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except editor_timeline.CommandValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        # A non-integer expectedRevision (or other int() conversion failure) reaches
        # apply_command before our validators run; surface it as a clean 400 rather
        # than leaking an opaque 500 to live editor-bay clients.
        raise HTTPException(status_code=400, detail="expectedRevision must be an integer") from exc


@router.post("/editor-timelines/{project_id}/commands/revert-last")
async def editor_timeline_revert_last_command(project_id: str, body: dict = Body(...)):
    _reject_demo_editor_project(project_id)
    if body.get("expectedRevision") is None:
        raise HTTPException(status_code=400, detail="expectedRevision is required")
    try:
        store = _editor_timeline_store()
        project = store.revert_last_command(
            project_id,
            actor=str(body.get("actor") or "human"),
            expected_revision=int(body.get("expectedRevision")),
        )
        inverse_op = str(((project.get("commandLog") or [{}])[-1] or {}).get("op") or "")
        project = _sync_render_cache_after_command(store, project, inverse_op)
        return project
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_timeline.RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except editor_timeline.CommandValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="expectedRevision must be an integer") from exc


@router.get("/editor-timelines/{project_id}/openshot")
async def editor_timeline_openshot_export(project_id: str):
    project = await _load_or_import_real_editor_project(project_id)
    capabilities = editor_render.detect_render_backends()
    return {
        "engineAvailable": capabilities["openshot"]["available"],
        "engineReason": capabilities["openshot"]["reason"],
        "timeline": openshot_bridge.timeline_json(project, asset_root=db.ASSETS_DIR),
        "updateActions": openshot_bridge.flattened_update_actions(project, asset_root=db.ASSETS_DIR),
    }


# ============ EDITOR BAY V2 — render backend spike ============
@router.get("/editor-render/capabilities")
async def editor_render_capabilities():
    return editor_render.detect_render_backends()


@router.post("/editor-render/{project_id}/render")
async def editor_render_project(project_id: str, body: dict = Body(default_factory=dict)):
    store = _editor_timeline_store()
    try:
        start = max(0.0, float(body.get("start", 0) or 0))
        duration = body.get("duration")
        duration = None if duration in {None, ""} else max(0.1, float(duration))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="start and duration must be numeric")
    if start > 0 and duration is None:
        raise HTTPException(status_code=400, detail="partial render requests require duration")

    try:
        project = await _load_or_import_real_editor_project(project_id)
        renderer = editor_render.choose_renderer(_editor_render_dir(), asset_root=db.ASSETS_DIR)
        is_window_render = start > 0 or duration is not None
        output_path = (
            _editor_render_output(project_id, f"_{start:.2f}_{duration:.2f}.mp4")
            if is_window_render and duration is not None
            else _editor_render_output(project_id, ".mp4")
        )
        result = renderer.render(
            project,
            output_path=output_path,
            start=start,
            duration=duration,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="editor timeline not found")
    except editor_render.RenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if start > 0 or duration is not None:
        key = f"{start:.2f}_{result.get('duration', duration or 0):.2f}"
        return _save_render_cache_entry_if_current(
            store,
            project,
            cache_kind="window",
            key=key,
            result=result,
        )
    return _save_render_cache_entry_if_current(
        store,
        project,
        cache_kind="video",
        result=result,
    )


@router.post("/editor-render/{project_id}/frame")
async def editor_render_frame(project_id: str, body: dict = Body(default_factory=dict)):
    store = _editor_timeline_store()
    try:
        at = max(0.0, float(body.get("at", 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="at must be numeric")

    try:
        project = await _load_or_import_real_editor_project(project_id)
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

    return _save_render_cache_entry_if_current(
        store,
        project,
        cache_kind="frame",
        key=f"{at:.2f}",
        result=result,
    )


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
    # Point the player at the render the factory wrote (schema {ep_id}/renders/episode.mp4)
    # when it exists, not the flat {ep_id}_episode.mp4 legacy URL (a back-compat symlink only).
    _schema_video = abn_assets.episode_singleton_path(rid, "episode")
    if _schema_video is not None and _schema_video.exists():
        _episode_url = f"/agenticnews-assets/{_schema_video.relative_to(db.ASSETS_DIR)}"
    else:
        _episode_url = f"/agenticnews-assets/{rid}_episode.mp4"
    video_url = arts.get("assembly_path") or _episode_url
    # Prefer the FULL on-disk timeline.json (per-segment shots[]/wordTimestamps[]/lowerThirds[] — the
    # atomic layers the editor bay renders as tracks). The DB copy is trimmed to keep rows small.
    timeline = v.get("timeline", {}) or {}
    _tl_file = _timeline_file_for_episode(rid)
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
    # Read-modify-write the timeline at the schema path {ep_id}/timeline.json the factory wrote
    # (resolved via the gateway) so the re-render below picks up the same file. Falls back to the
    # flat legacy path only for a non-episode/un-migrated id (see _timeline_file_for_episode).
    tl_file = _timeline_file_for_episode(rid)
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
