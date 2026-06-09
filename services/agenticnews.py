"""
AgenticBuilderNews — data layer for the living-workspace kanban flywheel.

One SQLite DB on the Railway volume (falls back to repo root locally) holds the
whole content operation: video cards moving through a 9-stage pipeline, the agent
job queue, the chat bridge, and the analytics that feed the flywheel back into
ideas.

Stages (the loop — Live feeds back into Idea Pool):
  idea -> ready -> scripting -> voice_visuals -> assembly -> review -> revision
       -> scheduled -> live

Sync sqlite3 wrapped in a thread executor so it never blocks the event loop.
"""
from __future__ import annotations

import os
import json
import sqlite3
import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# ---- DB location: Railway volume, else repo root ----
_VOL = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
_BASE = Path(_VOL) if _VOL and Path(_VOL).exists() else Path(__file__).resolve().parent.parent
DB_PATH = _BASE / "agenticnews.db"

# Where rendered assets live (served statically by app.py). ABN_ASSETS_DIR lets us put the heavy
# video assets on an external drive (e.g. the T9 SSD) so the internal disk never fills — the recurring
# 24/7 disk-exhaustion wall. Falls back to the repo-local dir (which may itself be a symlink to T9).
_ASSETS_OVERRIDE = os.getenv("ABN_ASSETS_DIR")
ASSETS_DIR = Path(_ASSETS_OVERRIDE) if _ASSETS_OVERRIDE else (_BASE / "agenticnews_assets")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

STAGES = [
    "idea", "ready", "scripting", "voice_visuals", "assembly",
    "review", "revision", "scheduled", "live",
]
STAGE_IDX = {s: i for i, s in enumerate(STAGES)}

_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def _init_sync() -> None:
    c = _connect()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            stage TEXT NOT NULL DEFAULT 'idea',
            lane TEXT NOT NULL DEFAULT 'week',      -- today | week | backlog
            data JSON NOT NULL,
            locked_by TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            video_id TEXT,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',  -- queued|claimed|running|done|failed
            agent_id TEXT,
            payload JSON,
            result JSON,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat (
            id TEXT PRIMARY KEY,
            who TEXT NOT NULL,        -- user | claude | sys
            text TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,  -- to page (claude->user)
            consumed INTEGER NOT NULL DEFAULT 0,   -- by claude (user->claude)
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS patterns (
            id TEXT PRIMARY KEY,
            video_id TEXT,
            hook TEXT, format TEXT, topic TEXT,
            outlier_score REAL,
            data JSON,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_videos_stage ON videos(stage);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, job_type);
        """
    )
    c.commit()


async def init_db() -> None:
    await asyncio.to_thread(_init_sync)
    # seed once if empty
    rows = await asyncio.to_thread(lambda: _connect().execute("SELECT COUNT(*) n FROM videos").fetchone()["n"])
    if rows == 0:
        await _seed()


def _now() -> float:
    return time.time()


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:8]}"


# ============ VIDEOS ============
def _row_to_video(r: sqlite3.Row) -> dict:
    d = json.loads(r["data"])
    d.update(id=r["id"], stage=r["stage"], lane=r["lane"], locked_by=r["locked_by"],
             created_at=r["created_at"], updated_at=r["updated_at"])
    return d


def _list_videos_sync(stage: Optional[str], since: Optional[float]) -> list[dict]:
    c = _connect()
    q = "SELECT * FROM videos"
    args: list[Any] = []
    conds = []
    if stage:
        conds.append("stage=?"); args.append(stage)
    if since:
        conds.append("updated_at>?"); args.append(since)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY updated_at DESC"
    return [_row_to_video(r) for r in c.execute(q, args).fetchall()]


async def list_videos(stage=None, since=None) -> list[dict]:
    return await asyncio.to_thread(_list_videos_sync, stage, since)


def _create_video_sync(d: dict) -> dict:
    c = _connect()
    vid = d.get("id") or _uid("v")
    stage = d.get("stage", "idea")
    lane = d.get("lane", "week")
    now = _now()
    body = {k: v for k, v in d.items() if k not in ("id", "stage", "lane", "locked_by", "created_at", "updated_at")}
    body.setdefault("title", "Untitled")
    body.setdefault("hook", "")
    body.setdefault("format", "Headline→Build")
    body.setdefault("artifacts", {})   # script/vo/visuals/assembly/thumbnail bools+paths
    body.setdefault("source_url", "")
    body.setdefault("source_signal", "")
    body.setdefault("iteration", 0)
    body.setdefault("metrics", {})
    body.setdefault("comments", [])
    body.setdefault("log", [{"t": now, "ev": "created", "stage": stage}])
    c.execute("INSERT INTO videos(id,stage,lane,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
              (vid, stage, lane, json.dumps(body), now, now))
    c.commit()
    return _row_to_video(c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())


async def create_video(d: dict) -> dict:
    return await asyncio.to_thread(_create_video_sync, d)


def _update_video_sync(vid: str, patch: dict) -> Optional[dict]:
    c = _connect()
    r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    body = json.loads(r["data"])
    stage = r["stage"]; lane = r["lane"]; locked = r["locked_by"]
    for k, v in patch.items():
        if k == "stage": stage = v
        elif k == "lane": lane = v
        elif k == "locked_by": locked = v
        elif k == "artifacts" and isinstance(v, dict):
            body.setdefault("artifacts", {}).update(v)
        elif k == "metrics" and isinstance(v, dict):
            body.setdefault("metrics", {}).update(v)
        else:
            body[k] = v
    now = _now()
    body.setdefault("log", []).append({"t": now, "ev": "update", "stage": stage})
    c.execute("UPDATE videos SET stage=?,lane=?,data=?,locked_by=?,updated_at=? WHERE id=?",
              (stage, lane, json.dumps(body), locked, now, vid))
    c.commit()
    return _row_to_video(c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())


async def update_video(vid: str, patch: dict) -> Optional[dict]:
    return await asyncio.to_thread(_update_video_sync, vid, patch)


def _delete_video_sync(vid: str) -> bool:
    c = _connect()
    c.execute("DELETE FROM videos WHERE id=?", (vid,)); c.commit()
    return True


async def delete_video(vid: str) -> bool:
    return await asyncio.to_thread(_delete_video_sync, vid)


# ============ JOBS (agent queue) ============
def _create_job_sync(video_id, job_type, payload) -> dict:
    c = _connect(); jid = _uid("j"); now = _now()
    c.execute("INSERT INTO jobs(id,video_id,job_type,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
              (jid, video_id, job_type, "queued", json.dumps(payload or {}), now, now))
    c.commit()
    return dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


async def create_job(video_id, job_type, payload=None) -> dict:
    return await asyncio.to_thread(_create_job_sync, video_id, job_type, payload)


def _list_jobs_sync(status, job_type) -> list[dict]:
    c = _connect(); q = "SELECT * FROM jobs"; args=[]; conds=[]
    if status: conds.append("status=?"); args.append(status)
    if job_type: conds.append("job_type=?"); args.append(job_type)
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at ASC"
    out=[]
    for r in c.execute(q, args).fetchall():
        d=dict(r)
        d["payload"]=json.loads(d.get("payload") or "{}")
        d["result"]=json.loads(d["result"]) if d.get("result") else None
        out.append(d)
    return out


async def list_jobs(status=None, job_type=None) -> list[dict]:
    return await asyncio.to_thread(_list_jobs_sync, status, job_type)


def _update_job_sync(jid, patch) -> Optional[dict]:
    c=_connect(); r=c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r: return None
    fields={**dict(r)}
    for k,v in patch.items():
        if k in ("result","payload") and isinstance(v,(dict,list)): fields[k]=json.dumps(v)
        else: fields[k]=v
    fields["updated_at"]=_now()
    c.execute("UPDATE jobs SET video_id=?,job_type=?,status=?,agent_id=?,payload=?,result=?,error=?,updated_at=? WHERE id=?",
              (fields["video_id"],fields["job_type"],fields["status"],fields.get("agent_id"),
               fields.get("payload"),fields.get("result"),fields.get("error"),fields["updated_at"],jid))
    c.commit()
    d=dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    d["payload"]=json.loads(d.get("payload") or "{}")
    d["result"]=json.loads(d["result"]) if d.get("result") else None
    return d


async def update_job(jid, patch) -> Optional[dict]:
    return await asyncio.to_thread(_update_job_sync, jid, patch)


# ============ CHAT BRIDGE ============
def _chat_post_sync(who, text) -> dict:
    c=_connect(); cid=_uid("c"); now=_now()
    c.execute("INSERT INTO chat(id,who,text,ts) VALUES(?,?,?,?)", (cid,who,text,now)); c.commit()
    return {"id":cid,"who":who,"text":text,"ts":now}


async def chat_post(who, text): return await asyncio.to_thread(_chat_post_sync, who, text)


def _chat_drain_sync(direction) -> list[dict]:
    # direction 'to_page' = claude msgs not delivered; 'to_claude' = user msgs not consumed
    c=_connect()
    if direction=="to_page":
        rows=c.execute("SELECT * FROM chat WHERE who='claude' AND delivered=0 ORDER BY ts").fetchall()
        ids=[r["id"] for r in rows]
        if ids: c.execute(f"UPDATE chat SET delivered=1 WHERE id IN ({','.join('?'*len(ids))})", ids); c.commit()
    else:
        rows=c.execute("SELECT * FROM chat WHERE who='user' AND consumed=0 ORDER BY ts").fetchall()
        ids=[r["id"] for r in rows]
        if ids: c.execute(f"UPDATE chat SET consumed=1 WHERE id IN ({','.join('?'*len(ids))})", ids); c.commit()
    return [dict(r) for r in rows]


async def chat_drain(direction): return await asyncio.to_thread(_chat_drain_sync, direction)


def _chat_history_sync(limit) -> list[dict]:
    c=_connect()
    rows=c.execute("SELECT * FROM chat ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


async def chat_history(limit=50): return await asyncio.to_thread(_chat_history_sync, limit)


# ============ PATTERNS (flywheel) ============
def _record_pattern_sync(video_id, hook, fmt, topic, score, data) -> dict:
    c=_connect(); pid=_uid("p"); now=_now()
    c.execute("INSERT INTO patterns(id,video_id,hook,format,topic,outlier_score,data,created_at) VALUES(?,?,?,?,?,?,?,?)",
              (pid,video_id,hook,fmt,topic,score,json.dumps(data or {}),now))
    c.commit()
    return {"id":pid,"video_id":video_id,"hook":hook,"format":fmt,"topic":topic,"outlier_score":score}


async def record_pattern(video_id, hook, fmt, topic, score, data=None):
    return await asyncio.to_thread(_record_pattern_sync, video_id, hook, fmt, topic, score, data)


def _top_patterns_sync(limit) -> list[dict]:
    c=_connect()
    rows=c.execute("SELECT * FROM patterns ORDER BY outlier_score DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


async def top_patterns(limit=10): return await asyncio.to_thread(_top_patterns_sync, limit)


# ============ SEED ============
async def _seed() -> None:
    seeds = [
        dict(title="Episode 01 — Benchmarks Are Theater", stage="assembly", lane="today",
             hook="Every 'our model beats theirs' headline might be theater. Let me prove it.",
             format="Headline→Build", source_signal="HN climax",
             artifacts={"script": True, "vo": True, "vo_path": "/agenticnews-assets/ep01_coldopen_cloned.wav",
                        "visuals": True, "assembly": True,
                        "assembly_path": "/agenticnews-assets/ep01_coldopen_assembled.mp4"}),
        dict(title="We Gave AI Agents Money", stage="scripting", lane="today",
             hook="We gave agents a database. Now Robinhood gave them your brokerage account.",
             format="Headline→Build", source_url="https://news.ycombinator.com",
             source_signal="HN 112pts", artifacts={"script": True}),
        dict(title="Microsoft Scout — autonomous agent on OpenClaw", stage="ready", lane="week",
             hook="Same runtime that wrote a hit piece on a maintainer. Now Microsoft's shipping it.",
             format="Headline→Build", source_signal="HN 94pts"),
        dict(title="Gemma 4 runs a frontier model on your laptop", stage="idea", lane="week",
             hook="Frontier-ish coding, quantized to fit on the machine you already own.",
             format="Build X in Y", source_signal="HN 314pts"),
        dict(title="Agents need what RSS does", stage="idea", lane="backlog",
             hook="The agent-feed problem nobody's solved. Let's build the fix.",
             format="Headline→Build", source_signal="HN 85pts"),
    ]
    for s in seeds:
        await create_video(s)
