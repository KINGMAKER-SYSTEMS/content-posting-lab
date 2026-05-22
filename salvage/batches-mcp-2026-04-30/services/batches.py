"""
Batch data layer.

A batch is a named, scoped grouping above 'job' that flows through every
stage (generate -> clip -> burn -> distribute). Batches live inside projects.
Stored at projects/{name}/batches.json with atomic writes.

Schema:
{
  "<batch_id>": {
    "id": "...",
    "label": "...",
    "project": "...",
    "created_at": "ISO8601",
    "status": "open" | "closed",
    "is_legacy": bool,
    "is_daily_default": bool,
    "stage_jobs": {
      "generate": ["job_id", ...],
      "burn": ["batch_id", ...],
      "clip": [...],
      "slideshow": [...]
    },
    "notes": ""
  }
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import project_manager
from project_manager import sanitize_project_name

log = logging.getLogger("batches")

VALID_STAGES = ("generate", "burn", "clip", "slideshow", "recreate", "captions", "upload")
VALID_STATUSES = ("open", "closed")

# In-process mtime cache for batches.json. Keyed by absolute path string.
# Eliminates redundant disk reads when multiple endpoints hit _load() in
# the same request lifecycle (list_batches + filter_jobs + manifest, etc).
# Cache invalidates when the file's mtime changes, so atomic tmp+rename
# writes from this same process or any other writer are picked up next call.
_load_cache: dict[str, tuple[float, dict]] = {}


def _batches_path(project: str) -> Path:
    return project_manager.PROJECTS_DIR / project / "batches.json"


def _project_dir(project: str) -> Path:
    return project_manager.PROJECTS_DIR / project


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_batch(batch_id: str, label: str, project: str, *, is_daily_default: bool = False, is_legacy: bool = False) -> dict:
    return {
        "id": batch_id,
        "label": label,
        "project": project,
        "created_at": _now_iso(),
        "status": "open",
        "is_legacy": is_legacy,
        "is_daily_default": is_daily_default,
        "stage_jobs": {stage: [] for stage in VALID_STAGES},
        "notes": "",
    }


def _load(project: str) -> dict:
    """Load batches.json for a project, returning {} if missing/invalid.

    Uses an mtime-based cache to avoid re-reading the same JSON file across
    rapid successive calls within one request. Returns a deep-ish copy so
    callers can mutate without polluting the cache.
    """
    p = _batches_path(project)
    key = str(p)
    if not p.exists():
        _load_cache.pop(key, None)
        return {}
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}

    cached = _load_cache.get(key)
    if cached is not None and cached[0] == mtime:
        # Return a shallow copy so callers mutating the dict don't write
        # through to the cache; nested batch dicts are also re-copied below
        # when needed (mutations happen via _save which invalidates the cache).
        return {k: v for k, v in cached[1].items()}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("batches.json unreadable for %s: %s", project, e)
        return {}

    _load_cache[key] = (mtime, data)
    return {k: v for k, v in data.items()}


def _save(project: str, data: dict) -> None:
    """Atomic write: tmp file then rename. Invalidates the load cache."""
    p = _batches_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(p)
    # Refresh cache with the new mtime + data so the next read is a cache hit
    try:
        _load_cache[str(p)] = (p.stat().st_mtime, data)
    except OSError:
        _load_cache.pop(str(p), None)


def _slugify(label: str, max_len: int = 40) -> str:
    import re
    s = label.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len] or "batch"


def _make_batch_id(project: str, existing: dict, label: str | None = None) -> str:
    """Generate a unique, human-readable batch ID: {prefix}-{MMDDHHmm}-{run}.

    Mirrors the pattern from the original burn router's batch ID generator.
    """
    ts = datetime.now().strftime("%m%d%H%M")
    prefix = _slugify(label or project, max_len=30)
    run = 1
    for existing_id in existing:
        if existing_id.startswith(f"{prefix}-{ts}-"):
            try:
                existing_run = int(existing_id.rsplit("-", 1)[-1])
                run = max(run, existing_run + 1)
            except ValueError:
                pass
    return f"{prefix}-{ts}-{run}"


def _daily_batch_id(project: str) -> str:
    """Return the deterministic ID of today's default batch for a project."""
    sanitized = sanitize_project_name(project)
    today = datetime.now().strftime("%Y%m%d")
    return f"{sanitized}-{today}-default"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_batch(project: str, label: str | None = None) -> dict:
    """Create a new batch in a project. Returns the batch dict."""
    sanitized = sanitize_project_name(project)
    if not _project_dir(sanitized).exists():
        raise FileNotFoundError(f"Project '{sanitized}' does not exist")

    data = _load(sanitized)
    batch_id = _make_batch_id(sanitized, data, label)
    batch = _empty_batch(batch_id, label or batch_id, sanitized)
    data[batch_id] = batch
    _save(sanitized, data)
    log.info("created batch %s in project %s", batch_id, sanitized)
    return batch


def get_batch(project: str, batch_id: str) -> dict | None:
    """Get a single batch by ID."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    return data.get(batch_id)


def list_batches(
    project: str,
    *,
    status: str | None = None,
    since: timedelta | None = None,
    include_legacy: bool = True,
) -> list[dict]:
    """List batches in a project, sorted newest-first by created_at.

    Args:
        status: filter by 'open' or 'closed'. None = all.
        since: only return batches created within this window.
        include_legacy: if False, hide auto-migrated legacy batches.
    """
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)

    batches = list(data.values())
    if status:
        batches = [b for b in batches if b.get("status") == status]
    if not include_legacy:
        batches = [b for b in batches if not b.get("is_legacy")]
    if since is not None:
        cutoff = datetime.now(timezone.utc) - since
        kept = []
        for b in batches:
            try:
                created = datetime.fromisoformat(b["created_at"].replace("Z", "+00:00"))
                if created >= cutoff:
                    kept.append(b)
            except (KeyError, ValueError):
                continue
        batches = kept

    batches.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return batches


def attach_job(project: str, batch_id: str, stage: str, job_id: str) -> dict:
    """Attach a job to a batch's stage list. Idempotent."""
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage}. Must be one of {VALID_STAGES}")

    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    batch = data.get(batch_id)
    if not batch:
        raise KeyError(f"Batch '{batch_id}' not found in project '{sanitized}'")

    stage_jobs = batch.setdefault("stage_jobs", {stage: [] for stage in VALID_STAGES})
    job_list = stage_jobs.setdefault(stage, [])
    if job_id not in job_list:
        job_list.append(job_id)
        _save(sanitized, data)
    return batch


def detach_job(project: str, batch_id: str, stage: str, job_id: str) -> dict | None:
    """Remove a job from a batch's stage list. No-op if not present."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    batch = data.get(batch_id)
    if not batch:
        return None
    stage_jobs = batch.get("stage_jobs", {})
    job_list = stage_jobs.get(stage, [])
    if job_id in job_list:
        job_list.remove(job_id)
        _save(sanitized, data)
    return batch


def close_batch(project: str, batch_id: str) -> dict | None:
    """Mark a batch as closed."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    batch = data.get(batch_id)
    if not batch:
        return None
    batch["status"] = "closed"
    batch["closed_at"] = _now_iso()
    _save(sanitized, data)
    return batch


def reopen_batch(project: str, batch_id: str) -> dict | None:
    """Reopen a closed batch."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    batch = data.get(batch_id)
    if not batch:
        return None
    batch["status"] = "open"
    batch.pop("closed_at", None)
    _save(sanitized, data)
    return batch


def delete_batch(project: str, batch_id: str) -> bool:
    """Delete a batch entry. Does NOT delete the underlying job files —
    those stay on disk and become unbatched (they get reabsorbed by legacy
    migration if it runs again).
    """
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    if batch_id not in data:
        return False
    del data[batch_id]
    _save(sanitized, data)
    return True


def update_notes(project: str, batch_id: str, notes: str) -> dict | None:
    """Update the free-text notes field on a batch."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    batch = data.get(batch_id)
    if not batch:
        return None
    batch["notes"] = notes or ""
    _save(sanitized, data)
    return batch


def get_or_create_daily_batch(project: str) -> dict:
    """Return today's default batch for a project, creating it if missing.

    The daily batch ID is deterministic ({project}-{YYYYMMDD}-default), so
    repeated calls within the same day return the same batch.
    """
    sanitized = sanitize_project_name(project)
    if not _project_dir(sanitized).exists():
        raise FileNotFoundError(f"Project '{sanitized}' does not exist")

    batch_id = _daily_batch_id(sanitized)
    data = _load(sanitized)
    if batch_id in data:
        return data[batch_id]

    label = f"{sanitized}-{datetime.now().strftime('%Y-%m-%d')}"
    batch = _empty_batch(batch_id, label, sanitized, is_daily_default=True)
    data[batch_id] = batch
    _save(sanitized, data)
    log.info("auto-created daily batch %s", batch_id)
    return batch


def find_batch_for_job(project: str, stage: str, job_id: str) -> dict | None:
    """Find which batch a job belongs to, if any."""
    sanitized = sanitize_project_name(project)
    data = _load(sanitized)
    for batch in data.values():
        if job_id in batch.get("stage_jobs", {}).get(stage, []):
            return batch
    return None


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

def _legacy_batch_id(project: str, date_str: str) -> str:
    """Deterministic legacy batch ID for a given YYYYMMDD."""
    sanitized = sanitize_project_name(project)
    return f"{sanitized}-legacy-{date_str}"


def _date_key(iso_timestamp: str | None) -> str | None:
    """Extract YYYYMMDD from an ISO8601 timestamp."""
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except (ValueError, AttributeError):
        return None


def migrate_legacy_jobs(project: str) -> dict:
    """Idempotently migrate pre-existing jobs into per-day legacy batches.

    Reads jobs.json, groups by created_at date, and ensures each job is
    attached to a legacy batch for that date. Jobs already attached to any
    batch (legacy or otherwise) are skipped.

    Returns a summary: {created_batches, attached_jobs, skipped_jobs}.
    """
    sanitized = sanitize_project_name(project)
    project_path = _project_dir(sanitized)
    if not project_path.exists():
        return {"created_batches": 0, "attached_jobs": 0, "skipped_jobs": 0}

    jobs_path = project_path / "jobs.json"
    if not jobs_path.exists():
        return {"created_batches": 0, "attached_jobs": 0, "skipped_jobs": 0}

    try:
        jobs_data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"created_batches": 0, "attached_jobs": 0, "skipped_jobs": 0}

    if not isinstance(jobs_data, dict):
        return {"created_batches": 0, "attached_jobs": 0, "skipped_jobs": 0}

    batches_data = _load(sanitized)

    # Collect job_ids already attached to ANY batch (skip these)
    already_attached: set[str] = set()
    for b in batches_data.values():
        for jid in b.get("stage_jobs", {}).get("generate", []):
            already_attached.add(jid)

    created_batches = 0
    attached_jobs = 0
    skipped_jobs = 0

    for job_id, job in jobs_data.items():
        if job_id in already_attached:
            skipped_jobs += 1
            continue

        date_key = _date_key(job.get("created_at")) or datetime.now().strftime("%Y%m%d")
        legacy_id = _legacy_batch_id(sanitized, date_key)

        if legacy_id not in batches_data:
            label = f"{sanitized}-legacy-{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
            batch = _empty_batch(legacy_id, label, sanitized, is_legacy=True)
            # Backdate created_at to the day of the jobs so sorting works
            batch["created_at"] = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}T00:00:00+00:00"
            batches_data[legacy_id] = batch
            created_batches += 1

        batches_data[legacy_id]["stage_jobs"]["generate"].append(job_id)
        attached_jobs += 1

    if created_batches > 0 or attached_jobs > 0:
        _save(sanitized, batches_data)

    return {
        "created_batches": created_batches,
        "attached_jobs": attached_jobs,
        "skipped_jobs": skipped_jobs,
    }


# ---------------------------------------------------------------------------
# Manifest hydration
# ---------------------------------------------------------------------------

def hydrate_manifest(project: str, batch_id: str, *, base_url: str = "") -> dict | None:
    """Build an agent-friendly manifest of every output file in the batch.

    Reads jobs.json and burn metadata to resolve file paths and URLs across
    every stage of the batch. Returns None if the batch doesn't exist.

    Args:
        base_url: optional absolute URL prefix (e.g. https://app.com).
                  When set, all URLs in the manifest become absolute.
    """
    sanitized = sanitize_project_name(project)
    batch = get_batch(sanitized, batch_id)
    if not batch:
        return None

    project_path = _project_dir(sanitized)
    manifest = {
        "batch_id": batch_id,
        "label": batch.get("label"),
        "project": sanitized,
        "status": batch.get("status"),
        "created_at": batch.get("created_at"),
        "is_legacy": batch.get("is_legacy", False),
        "is_daily_default": batch.get("is_daily_default", False),
        "notes": batch.get("notes", ""),
        "stages": {},
        "totals": {
            "videos": 0,
            "burns": 0,
            "clips": 0,
            "slideshows": 0,
            "recreates": 0,
            "caption_scrapes": 0,
            "uploads": 0,
        },
    }

    # ── generate stage: resolve from jobs.json
    generate_jobs: list[dict] = []
    jobs_path = project_path / "jobs.json"
    if jobs_path.exists():
        try:
            jobs_data = json.loads(jobs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            jobs_data = {}
        for job_id in batch.get("stage_jobs", {}).get("generate", []):
            job = jobs_data.get(job_id)
            if not job:
                continue
            files = []
            for v in job.get("videos", []):
                if v.get("status") != "done":
                    continue
                if v.get("crops"):
                    for c in v["crops"]:
                        if c.get("file"):
                            files.append({
                                "file": c["file"],
                                "url": _absolute_url(base_url, c.get("url")),
                                "is_crop": True,
                            })
                elif v.get("file"):
                    files.append({
                        "file": v["file"],
                        "url": _absolute_url(base_url, v.get("url")),
                        "is_crop": False,
                    })
            generate_jobs.append({
                "job_id": job_id,
                "prompt": job.get("prompt"),
                "provider": job.get("provider"),
                "created_at": job.get("created_at"),
                "files": files,
            })
            manifest["totals"]["videos"] += len(files)
    manifest["stages"]["generate"] = generate_jobs

    # ── burn stage: resolve burned/{batch_id}/burned_*.mp4 from each burn batch ID
    burn_jobs: list[dict] = []
    burn_dir = project_path / "burned"
    if burn_dir.exists():
        for burn_batch_id in batch.get("stage_jobs", {}).get("burn", []):
            sub = burn_dir / burn_batch_id
            if not sub.exists():
                continue
            files = []
            for f in sorted(sub.glob("burned_*.mp4")):
                rel = f.relative_to(project_path)
                files.append({
                    "file": str(rel.relative_to("burned")) if rel.parts[0] == "burned" else str(rel),
                    "url": _absolute_url(base_url, f"/projects/{sanitized}/burned/{burn_batch_id}/{f.name}"),
                })
            meta_path = sub / "batch_meta.json"
            meta = None
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    meta = None
            burn_jobs.append({
                "burn_batch_id": burn_batch_id,
                "meta": meta,
                "files": files,
            })
            manifest["totals"]["burns"] += len(files)
    manifest["stages"]["burn"] = burn_jobs

    # ── clip stage: hydrate from clipper per-job _state.json files
    clip_jobs_data: list[dict] = []
    clips_root = project_path / "clips"
    for cid in batch.get("stage_jobs", {}).get("clip", []):
        job_dir = clips_root / cid
        state_path = job_dir / "_state.json"
        job = {}
        if state_path.exists():
            try:
                job = json.loads(state_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                job = {}
        files = []
        for c in job.get("clips", []) or []:
            rel = c.get("file") or c.get("path")
            if rel:
                files.append({
                    "file": rel,
                    "url": _absolute_url(base_url, c.get("url") or f"/projects/{sanitized}/clips/{cid}/{Path(rel).name}"),
                    "duration": c.get("duration"),
                })
        clip_jobs_data.append({
            "clip_job_id": cid,
            "status": job.get("status"),
            "total": job.get("total"),
            "ok_count": job.get("ok_count"),
            "files": files,
        })
        manifest["totals"]["clips"] += len(files)
    manifest["stages"]["clip"] = clip_jobs_data

    # ── slideshow stage: hydrate from slideshow persisted state
    slideshow_jobs_data: list[dict] = []
    slideshow_dir = project_path / "videos" / "slideshow"
    for sid in batch.get("stage_jobs", {}).get("slideshow", []):
        files = []
        if slideshow_dir.exists():
            for f in sorted(slideshow_dir.glob(f"*{sid[:8]}*")):
                if f.is_file():
                    files.append({
                        "file": f.name,
                        "url": _absolute_url(base_url, f"/projects/{sanitized}/videos/slideshow/{f.name}"),
                    })
        slideshow_jobs_data.append({
            "slideshow_job_id": sid,
            "files": files,
        })
        manifest["totals"]["slideshows"] += len(files)
    manifest["stages"]["slideshow"] = slideshow_jobs_data

    # ── recreate stage: hydrate from projects/{name}/recreate/{job_id}/
    recreate_data: list[dict] = []
    recreate_dir = project_path / "recreate"
    for rid in batch.get("stage_jobs", {}).get("recreate", []):
        sub = recreate_dir / rid
        files = []
        if sub.exists():
            for f in sorted(sub.iterdir()):
                if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4"}:
                    files.append({
                        "file": f.name,
                        "url": _absolute_url(base_url, f"/projects/{sanitized}/recreate/{rid}/{f.name}"),
                    })
        recreate_data.append({"recreate_job_id": rid, "files": files})
    manifest["stages"]["recreate"] = recreate_data
    manifest["totals"]["recreates"] = sum(len(r["files"]) for r in recreate_data)

    # ── captions stage: hydrate from projects/{name}/captions/{username}/captions.csv
    captions_data: list[dict] = []
    captions_dir = project_path / "captions"
    for cap_id in batch.get("stage_jobs", {}).get("captions", []):
        # Caption scrape job_ids don't directly map to filesystem paths since
        # captions are organized by username — list all available CSVs as a
        # fallback so the agent at least sees what scrapes have landed.
        csv_files = []
        if captions_dir.exists():
            for sub in captions_dir.iterdir():
                if sub.is_dir():
                    csv_path = sub / "captions.csv"
                    if csv_path.exists():
                        csv_files.append({
                            "username": sub.name,
                            "csv": f"captions/{sub.name}/captions.csv",
                            "url": _absolute_url(base_url, f"/projects/{sanitized}/captions/{sub.name}/captions.csv"),
                        })
        captions_data.append({"captions_job_id": cap_id, "csv_files": csv_files})
    manifest["stages"]["captions"] = captions_data
    manifest["totals"]["caption_scrapes"] = len(captions_data)

    # ── upload stage: pull job state from upload service
    upload_data: list[dict] = []
    try:
        from services.upload import get_job as _get_upload_job
        for uid in batch.get("stage_jobs", {}).get("upload", []):
            job = _get_upload_job(uid)
            if job:
                upload_data.append({
                    "upload_job_id": uid,
                    "status": job.get("status"),
                    "account_name": job.get("account_name"),
                    "video_path": job.get("video_path"),
                    "error": job.get("error"),
                })
            else:
                upload_data.append({"upload_job_id": uid, "status": "unknown"})
    except Exception:  # noqa: BLE001
        for uid in batch.get("stage_jobs", {}).get("upload", []):
            upload_data.append({"upload_job_id": uid, "status": "unknown"})
    manifest["stages"]["upload"] = upload_data
    manifest["totals"]["uploads"] = len(upload_data)

    return manifest


def _absolute_url(base_url: str, path: str | None) -> str | None:
    """Prefix a relative URL with base_url if provided."""
    if not path:
        return None
    if not base_url:
        return path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base_url.rstrip('/')}{path}"


# ---------------------------------------------------------------------------
# Default-list filters used by routers
# ---------------------------------------------------------------------------

def parse_since(since: str | None) -> timedelta | None:
    """Parse a 'since' query param like '24h', '7d', '1h', '30m'.

    Returns None for unparsable input or the literal string 'all'.
    """
    if not since or since.lower() == "all":
        return None
    s = since.strip().lower()
    try:
        if s.endswith("h"):
            return timedelta(hours=int(s[:-1]))
        if s.endswith("d"):
            return timedelta(days=int(s[:-1]))
        if s.endswith("m"):
            return timedelta(minutes=int(s[:-1]))
        # Plain integer = hours
        return timedelta(hours=int(s))
    except ValueError:
        return None


def filter_jobs_by_batch_or_since(
    project: str,
    jobs: dict | Iterable[dict],
    *,
    batch_id: str | None = None,
    since: timedelta | None = None,
    return_all: bool = False,
) -> list[dict]:
    """Apply batch_id / since filters to a jobs collection.

    If return_all is True, all filters are bypassed.
    If batch_id is set, only jobs in that batch are returned.
    Otherwise, since is applied (default 24h if neither batch_id nor since
    is provided, but that default is the caller's responsibility — this
    function applies whatever filter is passed in).
    """
    if isinstance(jobs, dict):
        job_list = list(jobs.values())
    else:
        job_list = list(jobs)

    if return_all:
        return job_list

    if batch_id:
        sanitized = sanitize_project_name(project)
        batch = get_batch(sanitized, batch_id)
        if not batch:
            return []
        allowed = set(batch.get("stage_jobs", {}).get("generate", []))
        return [j for j in job_list if j.get("id") in allowed]

    if since is not None:
        cutoff = datetime.now(timezone.utc) - since
        kept = []
        for j in job_list:
            try:
                created = datetime.fromisoformat(
                    j.get("created_at", "").replace("Z", "+00:00")
                )
                if created >= cutoff:
                    kept.append(j)
            except (ValueError, AttributeError):
                continue
        return kept

    return job_list
