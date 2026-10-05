"""Durable pre-lease render jobs. No phone, schedule mutation, or caller-selected URL."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.post_render import (
    Hash, Identity, MAX_SOURCE_BYTES, MAX_FINAL_BYTES, MAX_OVERLAY_BYTES, MAX_QA_BYTES,
    PostRenderError, PostRenderRequest, RenderedPost, render_post, sha256, source_visual_matches,
)

log = logging.getLogger("content_lab.post_render_jobs")

JOB_SCHEMA = "content-lab.post-render-job.v1"
OUTPUT_AUTHORITY_SCHEMA = "content-lab.post-render-output-authority.v1"
MAX_ATTEMPTS = 3
RETRY_CODES = {"source_unavailable", "source_timeout", "process_timeout", "process_unavailable"}
DEFAULT_WORKER_COUNT = 2
MIN_WORKER_COUNT = 1
MAX_WORKER_COUNT = 4
# Retirement (lab #9): an acknowledged, succeeded render may have its media
# removed after this safety window; its jobs/idempotency rows stay as compact
# tombstones so hashes and idempotency remain authoritative. The free-space
# floor stops the worker from claiming new renders when the volume is tight.
DEFAULT_RETIRE_AFTER_MS = 24 * 60 * 60 * 1000
DEFAULT_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024
# One attempt holds the fetched source and render_post's verified copy at once.
# At peak it also holds the bounded overlay, a JSON copy with base64 expansion,
# one final, one QA frame, and bounded request/receipt/decode metadata. Encoding
# retries unlink the first final before writing the second, so only one final is
# counted. This is a disk reservation, not an estimate of typical output size.
MAX_RENDER_METADATA_BYTES = 1 * 1024 * 1024
MAX_CAPTION_RENDER_JSON_BYTES = 4 * ((MAX_OVERLAY_BYTES + 2) // 3) + MAX_RENDER_METADATA_BYTES
PEAK_RENDER_WORKSPACE_BYTES = (
    2 * MAX_SOURCE_BYTES
    + MAX_OVERLAY_BYTES
    + MAX_CAPTION_RENDER_JSON_BYTES
    + MAX_FINAL_BYTES + 1
    + MAX_QA_BYTES + 1
    + MAX_RENDER_METADATA_BYTES
)
# Failed/interrupted attempt bytes are diagnostic only; bound them separately
# from the permanent request/output authority in jobs and idempotency.
FAILED_MEDIA_RETENTION_MS = 24 * 60 * 60 * 1000
# attempts and provenance_updates are auxiliary audit history. The jobs row
# (including request, receipt/final/QA hashes and counters) and every
# idempotency row are no-repeat authority and are never deleted by this GC.
AUXILIARY_HISTORY_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
# Delivered-media age-out. The Worker does not call /acknowledge, so without
# this sweep every succeeded render's media stayed on the volume forever. A
# succeeded, unretired job is retired once (a) its window has passed since it
# succeeded and since its planned slot time (so the slot's 1 h admission-retry
# window is over) and (b) the Control Plane confirms that exact final is
# admitted: its /prepared-video door answers 206 only for a `ready` post
# artifact with that final_object_key whose R2 object exists. An unconfirmed
# job is never deleted; it is re-checked later.
#
# The sweep acts on volume pressure by itself instead of waiting for a person:
#   normal  free >= 25% of the volume        delivered window 72 h, every 5 min
#   tight   free <  25% of the volume        delivered window 24 h, every minute
#   floor   free below the render claim floor: emergency pass, safest first --
#           failed/interrupted attempt media of any age, then confirmed
#           delivered media whose slot is 2 h past -- before a claim is refused.
TIGHT_FREE_FRACTION = 0.25
DELIVERED_RETIRE_AFTER_MS = {"normal": 72 * 60 * 60 * 1000, "tight": 24 * 60 * 60 * 1000,
                             "floor": 2 * 60 * 60 * 1000}
AGE_OUT_INTERVAL_MS = {"normal": 5 * 60 * 1000, "tight": 60 * 1000, "floor": 60 * 1000}
AGE_OUT_RECHECK_MS = {"normal": 6 * 60 * 60 * 1000, "tight": 60 * 60 * 1000, "floor": 15 * 60 * 1000}
AGE_OUT_BATCH = 120
AGE_OUT_TIME_BUDGET_MS = 60 * 1000


def render_claim_floor_bytes(worker_count: int | None = None) -> int:
    """Free bytes below which no new render is claimed."""
    return _min_free_bytes() + PEAK_RENDER_WORKSPACE_BYTES * (worker_count or _worker_count())


def volume_pressure(free: int, total: int, floor: int) -> str:
    if free < floor:
        return "floor"
    if free < total * TIGHT_FREE_FRACTION:
        return "tight"
    return "normal"


def _retire_after_ms() -> int:
    raw = os.getenv("CONTENT_LAB_POST_RENDER_RETIRE_AFTER_MS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_RETIRE_AFTER_MS


def _min_free_bytes() -> int:
    raw = os.getenv("CONTENT_LAB_POST_RENDER_MIN_FREE_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_MIN_FREE_BYTES


def _worker_count() -> int:
    """Concurrent render workers. Default 2, clamped 1..4; invalid falls back to 2."""
    raw = os.getenv("CONTENT_LAB_POST_RENDER_WORKERS", "").strip()
    if not raw:
        return DEFAULT_WORKER_COUNT
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid CONTENT_LAB_POST_RENDER_WORKERS=%r, falling back to %d", raw, DEFAULT_WORKER_COUNT)
        return DEFAULT_WORKER_COUNT
    if not MIN_WORKER_COUNT <= value <= MAX_WORKER_COUNT:
        log.warning("CONTENT_LAB_POST_RENDER_WORKERS=%d out of range [%d,%d], falling back to %d",
                    value, MIN_WORKER_COUNT, MAX_WORKER_COUNT, DEFAULT_WORKER_COUNT)
        return DEFAULT_WORKER_COUNT
    return value


def _planned_at_ms(slot_payload_json: str) -> int | None:
    """Extract the slot's planned posting time (ms epoch) from the payload, if present.

    The slot payload is `cp_schedule_slots.payload_json` as the Worker sends it (see
    docs/post-render-setup.md). Its top-level `planned_at` is an ISO 8601 string with
    a trailing `Z`, e.g. "2026-09-25T18:20:00.000Z" — that is the real field and is
    parsed into epoch ms. `planned_at_ms` (a plain integer) is accepted as a fallback
    when `planned_at` is absent or unparseable. Anything malformed returns None, in
    which case claim order falls back to created_at_ms for that job.
    """
    try:
        slot = json.loads(slot_payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(slot, dict):
        return None
    planned_at = slot.get("planned_at")
    if isinstance(planned_at, str):
        try:
            parsed = datetime.fromisoformat(planned_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            try:
                value = round(parsed.timestamp() * 1000)
            except (OverflowError, OSError, ValueError):
                value = None
            if value is not None and value >= 0:
                return value
    value = slot.get("planned_at_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _tree_bytes(path: Path) -> int:
    total = 0
    for directory, _subdirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


class RenderJobError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_: Literal["posting-source-treatment/v1"] = Field(alias="schema")
    source_sha256: Hash
    source_visual_treatment_sha256: Hash
    provenance_id: Identity


class RenderJobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_: Literal[JOB_SCHEMA] = Field(alias="schema")
    request: PostRenderRequest
    slot_payload_json: str = Field(min_length=2, max_length=256 * 1024)
    source_provenance: SourceProvenance | None = None

    @model_validator(mode="after")
    def bind_slot_and_provenance(self):
        request = self.request
        if sha256(self.slot_payload_json.encode()) != request.slot_payload_sha256:
            raise ValueError("exact slot payload hash mismatch")
        slot = json.loads(self.slot_payload_json)
        if not isinstance(slot, dict) or any(not isinstance(slot.get(field), dict) for field in ("device_hint", "asset", "caption")):
            raise ValueError("invalid immutable slot payload")
        required = (slot.get("slot_id") == request.slot_id and slot.get("page_id") == request.page_id
                    and slot.get("handle") == request.account
                    and slot.get("device_hint", {}).get("device_serial") == request.device_serial
                    and slot.get("asset", {}).get("sha256") == request.source_sha256
                    and slot.get("caption", {}).get("text") == request.caption)
        if not required:
            raise ValueError("render request does not match immutable slot")
        if "program_id" in slot and slot["program_id"] != request.program_id:
            raise ValueError("slot program mismatch")
        # Structural equality only. Cross-language treatment hashing uses original request bytes.
        supplied = json.loads(request.render_treatment_json)
        if json.dumps(slot.get("render_treatment"), sort_keys=True, allow_nan=False) != json.dumps(supplied, sort_keys=True, allow_nan=False):
            raise ValueError("slot treatment does not match render request")
        provenance = self.source_provenance
        if provenance is not None and (provenance.source_sha256 != request.source_sha256
                or provenance.source_visual_treatment_sha256 != request.source_visual_treatment_sha256):
            raise ValueError("regeneration_required: missing or different source treatment provenance")
        return self


@dataclass(frozen=True, repr=False)
class SourceSettings:
    origin: str
    client_id: str
    client_secret: str

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment
                or not self.client_id or not self.client_secret):
            raise RenderJobError("source_service_not_configured")

    @classmethod
    def from_environment(cls):
        # Prepared source grants use the machine ingress. Page-vault reads keep
        # their separate CONTENT_LAB_CONTROL_PLANE_ORIGIN browser ingress.
        return cls(os.getenv("CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN", "").rstrip("/"),
                   os.getenv("CONTROL_PLANE_SERVICE_ID", ""), os.getenv("CONTROL_PLANE_SERVICE_SECRET", ""))


def confirm_admitted_final(final_sha256: str, final_byte_length: int, *,
                           transport: httpx.BaseTransport | None = None) -> bool | None:
    """Ask the Control Plane whether this exact final is admitted to R2.

    True only for a 206 whose Content-Range total equals the final's length.
    False for a definite 404 (no ready post artifact holds this final).
    None when unconfigured or the answer is anything else, so callers keep media.
    """
    origin = os.getenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "").strip().rstrip("/")
    client_id = os.getenv("CONTROL_PLANE_SERVICE_ID", "")
    client_secret = os.getenv("CONTROL_PLANE_SERVICE_SECRET", "")
    url = urlsplit(origin)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment or not client_id or not client_secret):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", final_sha256 or "") or not isinstance(final_byte_length, int):
        return None
    headers = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret,
               "Range": "bytes=0-0", "Accept-Encoding": "identity"}
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(10, connect=5), headers=headers) as client:
            response = client.get(f"{origin}/prepared-video/{final_sha256}.mp4")
    except httpx.HTTPError:
        return None
    if response.status_code == 404:
        return False
    if response.status_code != 206:
        return None
    total = response.headers.get("content-range", "").rpartition("/")[2]
    return total.isdigit() and int(total) == final_byte_length


def fetch_source(request: PostRenderRequest, destination: Path, settings: SourceSettings,
                 *, transport: httpx.BaseTransport | None = None) -> None:
    """Fetch only the grant derived from slot identity at a configured service origin."""
    url = (settings.origin.rstrip("/") + "/api/control-plane/v1/service/posting/v1/artifacts/"
           + quote(request.slot_id, safe="") + "/source")
    headers = {"CF-Access-Client-Id": settings.client_id, "CF-Access-Client-Secret": settings.client_secret,
               "Accept": "video/mp4", "Accept-Encoding": "identity"}
    deadline = time.monotonic() + 120
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(10, connect=5), headers=headers) as client:
            with client.stream("GET", url, params={"slot_payload_sha256": request.slot_payload_sha256}) as response:
                if response.status_code in {403, 404}:
                    raise RenderJobError("source_authorization_rejected")
                if response.status_code != 200:
                    raise RenderJobError("source_unavailable" if response.status_code >= 500 else "source_response_rejected")
                declared = response.headers.get("content-length", "")
                if (not declared.isdigit() or not 0 < int(declared) <= MAX_SOURCE_BYTES
                        or response.headers.get("x-source-sha256") != request.source_sha256
                        or response.headers.get("content-type", "").split(";", 1)[0] != "video/mp4"
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise RenderJobError("source_response_binding_mismatch")
                digest, total = hashlib.sha256(), 0
                with destination.open("xb") as file:
                    for chunk in response.iter_raw(64 * 1024):
                        if time.monotonic() > deadline:
                            raise RenderJobError("source_timeout")
                        total += len(chunk)
                        if total > int(declared) or total > MAX_SOURCE_BYTES:
                            raise RenderJobError("source_size_mismatch")
                        digest.update(chunk)
                        file.write(chunk)
                    file.flush()
                    os.fsync(file.fileno())
                if total != int(declared) or digest.hexdigest() != request.source_sha256:
                    raise RenderJobError("source_sha256_mismatch")
    except httpx.TimeoutException as error:
        destination.unlink(missing_ok=True)
        raise RenderJobError("source_timeout") from error
    except httpx.HTTPError as error:
        destination.unlink(missing_ok=True)
        raise RenderJobError("source_unavailable") from error
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _locked(path: Path):
    file = path.open("a+b")
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ProcessLock(file)
    except BlockingIOError:
        file.close()
        return None


class ProcessLock:
    def __init__(self, file):
        self.file = file

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        try:
            # Explicit unlock releases ownership even if a fork inherited the open description.
            fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close()


class PostRenderJobs:
    """SQLite owns job truth; OS locks fence live writers and clear when a process dies."""
    def __init__(self, root: Path, *, fetcher: Callable | None = None, renderer: Callable = render_post,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 confirm_final: Callable[[str, int], bool | None] = confirm_admitted_final):
        if not root.is_absolute():
            raise RenderJobError("job_root_must_be_absolute")
        self.root, self.renderer, self.clock_ms = root, renderer, clock_ms
        self.confirm_final = confirm_final
        self._age_out_lock = threading.Lock()
        self._next_age_out_ms = 0
        self._last_age_out_ms = -AGE_OUT_INTERVAL_MS["floor"]
        self._age_out_thread: threading.Thread | None = None
        self.worker_count = _worker_count()
        self.fetcher = fetcher or (lambda request, path: fetch_source(request, path, SourceSettings.from_environment()))
        for directory in (root, root / "locks", root / "attempts"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._stop = threading.Event()
        self._threads = []
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, slot_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL, submission_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','regeneration_needed')),
                    attempt_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    available_at_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL, error_code TEXT, receipt_sha256 TEXT,
                    planned_at_ms INTEGER,
                    acknowledged_at_ms INTEGER,
                    retired_at_ms INTEGER,
                    final_sha256 TEXT,
                    qa_frame_sha256 TEXT,
                    final_byte_length INTEGER,
                    output_authority_json TEXT,
                    age_out_checked_at_ms INTEGER,
                    UNIQUE(slot_id,slot_hash));
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), request_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provenance_updates (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    old_submission_json TEXT NOT NULL, new_request_hash TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    state TEXT NOT NULL, started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER, error_code TEXT);
            """)
            # Idempotent migration for databases created before planned_at_ms
            # (or the retirement columns) existed.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
            integer_columns = (
                "planned_at_ms", "acknowledged_at_ms", "retired_at_ms",
                "final_byte_length", "age_out_checked_at_ms",
            )
            text_columns = ("final_sha256", "qa_frame_sha256", "output_authority_json")
            for name in integer_columns:
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} INTEGER")
            for name in text_columns:
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_final_sha256_authority "
                "ON jobs(final_sha256) WHERE final_sha256 IS NOT NULL"
            )
            # One-time backfill, safe to run on every init: only NULL rows in states that
            # still matter for claim order are touched, so already-backfilled or terminal
            # rows are never revisited. Without this, jobs already queued/running at
            # deploy would sort after every newly enqueued job with a real planned_at.
            for row in db.execute("""
                SELECT id, submission_json FROM jobs
                WHERE planned_at_ms IS NULL AND state IN ('queued','running','failed','regeneration_needed')
            """).fetchall():
                try:
                    planned = _planned_at_ms(RenderJobSubmission.model_validate_json(row["submission_json"]).slot_payload_json)
                except Exception:
                    continue
                if planned is not None:
                    db.execute("UPDATE jobs SET planned_at_ms=? WHERE id=?", (planned, row["id"]))

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.root / "jobs.sqlite", timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, submission: RenderJobSubmission, key: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", key):
            raise RenderJobError("invalid_idempotency_key")
        submission = RenderJobSubmission.model_validate(submission.model_dump(by_alias=True))
        payload = submission.model_dump_json(by_alias=True)
        request_hash = sha256(payload.encode())
        request, now = submission.request, self.clock_ms()
        if request.created_at_ms > now:
            raise RenderJobError("request_future_dated")
        job_id = "render_" + sha256((request.slot_id + "\0" + request.slot_payload_sha256).encode())[:32]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_key = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing_key and existing_key["request_hash"] == request_hash:
                return self.status(existing_key["job_id"])
            if ((existing_key and existing_key["request_hash"] != request_hash)
                    or (existing and existing["request_hash"] != request_hash)):
                raise RenderJobError("idempotency_conflict")
            if not existing:
                has_provenance = submission.source_provenance is not None
                matches = has_provenance and source_visual_matches(request)
                state = "queued" if matches else "regeneration_needed"
                reason = None if matches else "source_treatment_mismatch" if has_provenance else "source_treatment_provenance_missing"
                planned_at_ms = _planned_at_ms(submission.slot_payload_json)
                db.execute("INSERT INTO jobs(id,slot_id,slot_hash,request_hash,submission_json,state,available_at_ms,created_at_ms,updated_at_ms,error_code,planned_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, request.slot_id, request.slot_payload_sha256, request_hash, payload, state, now, now, now, reason, planned_at_ms))
            elif (existing["state"] == "failed" and existing["error_code"] == "source_response_rejected"
                    and existing["attempts"] >= MAX_ATTEMPTS):
                # A fresh key is an explicit Control Plane recovery request. The
                # key is stored below, so replaying it returns the current job
                # instead of repeatedly resetting a persistent source failure.
                # request_hash matched above, so submission.slot_payload_json is the
                # same payload already stored; backfill planned_at_ms here too, in case
                # this row predates the column and the schema-init backfill hasn't run.
                planned_at_ms = existing["planned_at_ms"]
                if planned_at_ms is None:
                    planned_at_ms = _planned_at_ms(submission.slot_payload_json)
                db.execute("UPDATE jobs SET state='queued',attempt_id=NULL,attempts=0,available_at_ms=?,updated_at_ms=?,error_code=NULL,receipt_sha256=NULL,planned_at_ms=? WHERE id=?",
                           (now, now, planned_at_ms, job_id))
            db.execute("INSERT OR IGNORE INTO idempotency(key,job_id,request_hash) VALUES(?,?,?)", (key, job_id, request_hash))
        return self.status(job_id)

    def _row(self, job_id: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RenderJobError("job_not_found")
        return row

    def require_page(self, job_id: str, page_id: str):
        request = RenderJobSubmission.model_validate_json(self._row(job_id)["submission_json"]).request
        if request.page_id != page_id:
            raise RenderJobError("job_not_found")

    def status(self, job_id: str) -> dict:
        row = self._row(job_id)
        result = {key: row[key] for key in ("id", "slot_id", "slot_hash", "state", "attempts", "created_at_ms", "updated_at_ms", "error_code", "acknowledged_at_ms", "retired_at_ms")}
        result["schema"] = "content-lab.post-render-status.v1"
        if row["state"] == "regeneration_needed":
            result["next_action"] = "regenerate_source_with_treatment_provenance"
        result["retryable"] = row["state"] == "failed" and row["attempts"] < MAX_ATTEMPTS
        if row["state"] == "succeeded":
            result["receipt_sha256"] = row["receipt_sha256"]
            if row["final_sha256"] is not None:
                result["final_sha256"] = row["final_sha256"]
                result["qa_frame_sha256"] = row["qa_frame_sha256"]
            result["artifacts"] = {kind: f"/api/control-plane/v1/post-renders/{job_id}/artifacts/{kind}" for kind in ("final", "qa", "receipt")}
        return result

    def retry(self, job_id: str) -> dict:
        with self._db() as db:
            changed = db.execute("UPDATE jobs SET state='queued',available_at_ms=?,updated_at_ms=?,error_code=NULL WHERE id=? AND state='failed' AND attempts<?",
                                 (self.clock_ms(), self.clock_ms(), job_id, MAX_ATTEMPTS)).rowcount
        if not changed:
            self._row(job_id)
            raise RenderJobError("job_not_retryable")
        return self.status(job_id)

    def provide_provenance(self, job_id: str, submission: RenderJobSubmission) -> dict:
        submission = RenderJobSubmission.model_validate(submission.model_dump(by_alias=True))
        payload = submission.model_dump_json(by_alias=True)
        request_hash = sha256(payload.encode())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise RenderJobError("job_not_found")
            if row["request_hash"] == request_hash:
                return self.status(job_id)
            old = RenderJobSubmission.model_validate_json(row["submission_json"]).model_dump(by_alias=True)
            new = submission.model_dump(by_alias=True)
            for value in (old, new):
                value.pop("source_provenance")
                value["request"].pop("source_visual_treatment_json")
                value["request"].pop("source_visual_treatment_sha256")
            if old != new or row["state"] != "regeneration_needed" or row["attempts"] != 0:
                raise RenderJobError("provenance_revision_conflict")
            if submission.source_provenance is None:
                raise RenderJobError("source_treatment_provenance_missing")
            matches = source_visual_matches(submission.request)
            now = self.clock_ms()
            db.execute("INSERT INTO provenance_updates(job_id,old_submission_json,new_request_hash,updated_at_ms) VALUES(?,?,?,?)",
                       (job_id, row["submission_json"], request_hash, now))
            db.execute("UPDATE jobs SET request_hash=?,submission_json=?,state=?,error_code=?,available_at_ms=?,updated_at_ms=? WHERE id=?",
                       (request_hash, payload, "queued" if matches else "regeneration_needed",
                        None if matches else "source_treatment_mismatch", now, now, job_id))
        return self.status(job_id)

    def _output(self, row) -> Path:
        return self.root / "attempts" / row["id"] / row["attempt_id"] / "render"

    def _validate_output_authority(self, row, authority: dict) -> dict:
        """Validate durable output facts against the immutable request and job row."""
        if authority.get("schema") != OUTPUT_AUTHORITY_SCHEMA:
            raise RenderJobError("output_authority_invalid")
        receipt = authority.get("receipt")
        evidence = authority.get("decode")
        if not isinstance(receipt, dict) or not isinstance(evidence, dict):
            raise RenderJobError("output_authority_invalid")
        request = RenderJobSubmission.model_validate_json(row["submission_json"]).request
        for field in ("slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial", "account",
                      "source_sha256", "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version"):
            if receipt.get(field) != getattr(request, field):
                raise RenderJobError("receipt_binding_mismatch")
        final_sha = authority.get("final_sha256")
        qa_sha = authority.get("qa_frame_sha256")
        final_length = authority.get("final_byte_length")
        if (not isinstance(final_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", final_sha)
                or not isinstance(qa_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", qa_sha)
                or not isinstance(final_length, int) or isinstance(final_length, bool) or final_length <= 0
                or receipt.get("final_sha256") != final_sha
                or receipt.get("final_byte_length") != final_length
                or receipt.get("qa_frame_sha256") != qa_sha or final_sha == request.source_sha256
                or receipt.get("final_object_key") != f"posting/final/{final_sha}.mp4"
                or receipt.get("mime_type") != "video/mp4" or receipt.get("width") != 1080 or receipt.get("height") != 1920
                or not request.created_at_ms <= receipt.get("completed_at_ms", -1) <= self.clock_ms()):
            raise RenderJobError("artifact_binding_mismatch")
        if (evidence.get("final_sha256") != final_sha or evidence.get("qa_frame_sha256") != qa_sha
                or evidence.get("decoded_video_frames", 0) <= 0
                or evidence.get("final_probe", {}).get("duration_ms") != receipt.get("duration_ms")
                or receipt.get("duration_ms", 0) <= 0):
            raise RenderJobError("decode_evidence_missing")
        receipt_sha = authority.get("receipt_sha256")
        if (not isinstance(receipt_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha)
                or row["receipt_sha256"] and row["receipt_sha256"] != receipt_sha
                or row["final_sha256"] and row["final_sha256"] != final_sha
                or row["qa_frame_sha256"] and row["qa_frame_sha256"] != qa_sha
                or row["final_byte_length"] and row["final_byte_length"] != final_length):
            raise RenderJobError("output_authority_changed")
        return authority

    def _verify_output(self, row) -> dict:
        """Return verified output facts from media, or its durable tombstone once removed."""
        from services.post_render import _file_hash
        output = self._output(row)
        required = tuple(output / name for name in ("receipt.json", "final.mp4", "qa.jpg", "decode.json"))
        if not output.is_dir() or not all(path.is_file() for path in required):
            encoded = row["output_authority_json"]
            if not encoded:
                raise RenderJobError("output_authority_missing")
            try:
                authority = json.loads(encoded)
            except (TypeError, ValueError) as error:
                raise RenderJobError("output_authority_invalid") from error
            if not isinstance(authority, dict):
                raise RenderJobError("output_authority_invalid")
            return self._validate_output_authority(row, authority)
        with (output / "receipt.json").open("rb") as file:
            receipt_bytes = file.read(64 * 1024 + 1)
        if len(receipt_bytes) > 64 * 1024:
            raise RenderJobError("receipt_invalid")
        receipt = json.loads(receipt_bytes)
        if receipt.get("schema") != "posting-prepared-artifact/v1":
            raise RenderJobError("receipt_invalid")
        final_sha, final_length = _file_hash(output / "final.mp4", MAX_FINAL_BYTES)
        qa_sha, _ = _file_hash(output / "qa.jpg", MAX_QA_BYTES)
        with (output / "decode.json").open("rb") as file:
            evidence_bytes = file.read(64 * 1024 + 1)
        if len(evidence_bytes) > 64 * 1024:
            raise RenderJobError("decode_evidence_invalid")
        evidence = json.loads(evidence_bytes)
        receipt_sha = sha256(receipt_bytes)
        authority = {
            "schema": OUTPUT_AUTHORITY_SCHEMA,
            "receipt_sha256": receipt_sha,
            "final_sha256": final_sha,
            "qa_frame_sha256": qa_sha,
            "final_byte_length": final_length,
            "receipt": receipt,
            "decode": evidence,
        }
        return self._validate_output_authority(row, authority)

    @staticmethod
    def _authority_values(authority: dict) -> tuple[str, str, str, int, str]:
        return (
            authority["receipt_sha256"],
            authority["final_sha256"],
            authority["qa_frame_sha256"],
            authority["final_byte_length"],
            json.dumps(authority, sort_keys=True, separators=(",", ":")),
        )

    def _persist_output_authority(self, row, authority: dict) -> None:
        values = self._authority_values(authority)
        try:
            with self._db() as db:
                duplicate = db.execute(
                    "SELECT id FROM jobs WHERE final_sha256=? AND id<>?",
                    (authority["final_sha256"], row["id"]),
                ).fetchone()
                if duplicate:
                    raise RenderJobError("duplicate_output")
                db.execute(
                    "UPDATE jobs SET receipt_sha256=?,final_sha256=?,qa_frame_sha256=?,"
                    "final_byte_length=?,output_authority_json=? WHERE id=?",
                    (*values, row["id"]),
                )
        except sqlite3.IntegrityError as error:
            raise RenderJobError("duplicate_output") from error

    def artifact(self, job_id: str, kind: str) -> Path:
        names = {"final": "final.mp4", "qa": "qa.jpg", "receipt": "receipt.json"}
        if kind not in names:
            raise RenderJobError("artifact_not_found")
        row = self._row(job_id)
        if row["state"] != "succeeded" or row["retired_at_ms"] is not None:
            raise RenderJobError("artifact_not_ready")
        output = self._output(row)
        if not all((output / name).is_file() for name in ("receipt.json", "final.mp4", "qa.jpg", "decode.json")):
            raise RenderJobError("artifact_not_ready")
        self._verify_output(row)
        return self._output(row) / names[kind]

    def acknowledge(self, job_id: str) -> dict:
        """Record downstream admission. Only a succeeded job may be acknowledged.

        Idempotent: a replay after acknowledgment (including after retirement)
        returns the current status without re-verifying deleted media.
        """
        row = self._row(job_id)
        if row["state"] != "succeeded":
            raise RenderJobError("acknowledge_not_ready")
        if row["acknowledged_at_ms"]:
            return self.status(job_id)
        authority = self._verify_output(row)
        self._persist_output_authority(row, authority)
        now = self.clock_ms()
        with self._db() as db:
            changed = db.execute(
                "UPDATE jobs SET acknowledged_at_ms=? WHERE id=? AND acknowledged_at_ms IS NULL",
                (now, job_id),
            ).rowcount
        return self.status(job_id)

    def retire(self, job_id: str) -> dict:
        """Remove media for an acknowledged, succeeded job, keeping its tombstone.

        Receipt/hash/idempotency rows are retained; only the attempt directory
        is deleted. Unacknowledged or non-succeeded jobs are never touched.
        """
        row = self._row(job_id)
        if row["state"] != "succeeded" or not row["acknowledged_at_ms"]:
            raise RenderJobError("retire_not_ready")
        if row["retired_at_ms"]:
            return self.status(job_id)
        if not self._retire_row(row):
            raise RenderJobError("retirement_failed")
        return self.status(job_id)

    def _persist_shared_output_authority(self, row, authority: dict) -> None:
        """Tombstone a job whose final bytes another job already owns.

        jobs.final_sha256 is UNIQUE, so the owning job keeps that column; this
        row keeps the full output authority JSON (which names the same final
        hash), the receipt and QA hashes, and the byte length.
        """
        receipt_sha, _final_sha, qa_sha, length, encoded = self._authority_values(authority)
        with self._db() as db:
            db.execute(
                "UPDATE jobs SET receipt_sha256=?,qa_frame_sha256=?,final_byte_length=?,"
                "output_authority_json=? WHERE id=?",
                (receipt_sha, qa_sha, length, encoded, row["id"]),
            )

    def _retire_row(self, row, *, authority: dict | None = None, allow_shared_final: bool = False) -> bool:
        """Persist output authority, then remove media and finally mark retired."""
        authority = authority or self._verify_output(row)
        try:
            self._persist_output_authority(row, authority)
        except RenderJobError as error:
            if error.code != "duplicate_output" or not allow_shared_final:
                raise
            self._persist_shared_output_authority(row, authority)
        attempt_dir = self._output(row).parent.parent
        try:
            shutil.rmtree(attempt_dir)
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("post render retirement deletion failed job=%s", row["id"])
            return False
        if attempt_dir.exists():
            log.error("post render retirement deletion failed job=%s directory remains", row["id"])
            return False
        with self._db() as db:
            db.execute(
                "UPDATE jobs SET retired_at_ms=? WHERE id=? AND retired_at_ms IS NULL",
                (self.clock_ms(), row["id"]),
            )
            db.execute("UPDATE attempts SET state='retired' WHERE job_id=?", (row["id"],))
        return True

    def _gc(self) -> None:
        """Retire acknowledged, succeeded media after the safety window.

        Best-effort, called from the worker loop. Rows become tombstones
        (receipt/hash/idempotency retained); the attempt directory is removed.
        """
        cutoff = self.clock_ms() - _retire_after_ms()
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE state='succeeded' AND acknowledged_at_ms IS NOT NULL "
                "AND retired_at_ms IS NULL AND acknowledged_at_ms<=?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            try:
                self._retire_row(row)
            except (OSError, ValueError, TypeError):
                log.exception("post render retirement failed job=%s", row["id"])
        self._gc_failed_attempt_media()
        if self._gc_auxiliary_history():
            self._checkpoint_wal()

    def _maybe_age_out(self, *, force: bool = False) -> dict | None:
        """One age-out pass when due (or forced); tightens itself under volume pressure."""
        now = self.clock_ms()
        due = self._next_age_out_ms
        if force:  # a refused claim may pull the next pass forward, at most once per floor interval
            due = min(due, self._last_age_out_ms + AGE_OUT_INTERVAL_MS["floor"])
        if now < due or not self._age_out_lock.acquire(blocking=False):
            return None
        try:
            self._last_age_out_ms = now
            pressure = self._volume_pressure()
            summary = {"pressure": pressure, "failed_media_dirs": 0}
            if pressure == "floor":
                # Safest class first: failed attempt bytes are diagnostic only.
                summary["failed_media_dirs"] = self._gc_failed_attempt_media(retention_ms=0)
            summary.update(self._age_out_delivered(pressure))
            if pressure != "normal":
                log.warning("post render age-out acted on volume pressure=%s retired=%d bytes_freed=%d "
                            "failed_media_dirs=%d", pressure, summary["retired"], summary["bytes_freed"],
                            summary["failed_media_dirs"])
            self._next_age_out_ms = self.clock_ms() + AGE_OUT_INTERVAL_MS[pressure]
            return summary
        finally:
            self._age_out_lock.release()

    def _volume_pressure(self) -> str:
        """Log used/free on every pass and classify the volume. Unknown usage changes nothing."""
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            log.error("post render volume usage unavailable root=%s; age-out keeps normal windows", self.root)
            return "normal"
        floor = render_claim_floor_bytes(self.worker_count)
        pressure = volume_pressure(usage.free, usage.total, floor)
        log.info("post render volume used_bytes=%d free_bytes=%d total_bytes=%d floor_bytes=%d pressure=%s",
                 usage.used, usage.free, usage.total, floor, pressure)
        return pressure

    def _age_out_delivered(self, pressure: str = "normal") -> dict:
        """Retire succeeded media the Control Plane confirms is admitted to R2 (bounded per run)."""
        started = self.clock_ms()
        cutoff = started - DELIVERED_RETIRE_AFTER_MS[pressure]
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE state='succeeded' AND retired_at_ms IS NULL AND updated_at_ms<=? "
                "AND (planned_at_ms IS NULL OR planned_at_ms<=?) "
                "AND (age_out_checked_at_ms IS NULL OR age_out_checked_at_ms<=?) "
                "ORDER BY COALESCE(age_out_checked_at_ms,0), updated_at_ms LIMIT ?",
                (cutoff, cutoff, started - AGE_OUT_RECHECK_MS[pressure], AGE_OUT_BATCH),
            ).fetchall()
        summary = {"retired": 0, "unconfirmed": 0, "failed": 0, "bytes_freed": 0}
        for row in rows:
            if self.clock_ms() - started > AGE_OUT_TIME_BUDGET_MS:
                break
            try:
                authority = self._verify_output(row)
                confirmed = self.confirm_final(authority["final_sha256"], authority["final_byte_length"])
                if confirmed is not True:
                    summary["unconfirmed"] += 1
                    with self._db() as db:
                        db.execute("UPDATE jobs SET age_out_checked_at_ms=? WHERE id=?", (self.clock_ms(), row["id"]))
                    continue
                size = _tree_bytes(self.root / "attempts" / row["id"])
                if self._retire_row(row, authority=authority, allow_shared_final=True):
                    summary["retired"] += 1
                    summary["bytes_freed"] += size
                else:
                    summary["failed"] += 1
            except (OSError, ValueError, TypeError):
                summary["failed"] += 1
                log.exception("post render age-out failed job=%s", row["id"])
        if rows:
            log.info("post render age-out pressure=%s retired=%d unconfirmed=%d failed=%d bytes_freed=%d "
                     "candidates=%d", pressure, summary["retired"], summary["unconfirmed"], summary["failed"],
                     summary["bytes_freed"], len(rows))
        return summary

    def _gc_failed_attempt_media(self, *, retention_ms: int = FAILED_MEDIA_RETENTION_MS) -> int:
        cutoff = self.clock_ms() - retention_ms
        removed = 0
        with self._db() as db:
            rows = db.execute(
                "SELECT id,job_id FROM attempts WHERE state IN ('failed','interrupted') "
                "AND ended_at_ms IS NOT NULL AND ended_at_ms<=?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            attempt_dir = self.root / "attempts" / row["job_id"] / row["id"]
            if not attempt_dir.exists():
                continue
            try:
                shutil.rmtree(attempt_dir)
            except FileNotFoundError:
                pass
            except OSError:
                log.exception(
                    "post render failed-attempt cleanup failed job=%s attempt=%s",
                    row["job_id"], row["id"],
                )
                continue
            if attempt_dir.exists():
                log.error(
                    "post render failed-attempt cleanup failed job=%s attempt=%s directory remains",
                    row["job_id"], row["id"],
                )
                continue
            removed += 1
            try:
                attempt_dir.parent.rmdir()
            except OSError:
                pass
        return removed

    def _gc_auxiliary_history(self) -> bool:
        cutoff = self.clock_ms() - AUXILIARY_HISTORY_RETENTION_MS
        with self._db() as db:
            rows = db.execute(
                "SELECT id,job_id FROM attempts WHERE state IN ('failed','interrupted','retired') "
                "AND ended_at_ms IS NOT NULL AND ended_at_ms<=?",
                (cutoff,),
            ).fetchall()
            removable = [
                row["id"] for row in rows
                if not (self.root / "attempts" / row["job_id"] / row["id"]).exists()
            ]
            removed = 0
            for attempt_id in removable:
                removed += db.execute("DELETE FROM attempts WHERE id=?", (attempt_id,)).rowcount
            removed += db.execute(
                "DELETE FROM provenance_updates WHERE updated_at_ms<=?", (cutoff,)
            ).rowcount
        return bool(removed)

    def _checkpoint_wal(self) -> None:
        try:
            db = sqlite3.connect(self.root / "jobs.sqlite", timeout=5, isolation_level=None)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                db.close()
        except sqlite3.Error:
            log.exception("post render SQLite WAL checkpoint failed")

    def _free_bytes(self) -> int | None:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            log.exception("post render capacity check failed reason=post_render_free_space_unavailable")
            return None

    def _has_workspace_capacity(self, *, job_id: str | None = None) -> bool:
        free = self._free_bytes()
        if free is None:
            return False
        required = render_claim_floor_bytes(self.worker_count)
        if free < required:
            # Act before refusing: one bounded emergency pass, then look again.
            if self._maybe_age_out(force=True) is not None:
                free = self._free_bytes()
                if free is not None and free >= required:
                    return True
                free = free if free is not None else 0
            log.warning(
                "post render claim deferred reason=post_render_workspace_capacity_insufficient "
                "job=%s free_bytes=%d required_bytes=%d workers=%d",
                job_id, free, required, self.worker_count,
            )
            return False
        return True

    def _finish(self, row, authority: dict):
        output = self._output(row)
        for path in output.iterdir():
            if path.is_file():
                with path.open("rb") as file:
                    os.fsync(file.fileno())
        for directory in (output, output.parent, output.parent.parent, self.root / "attempts", self.root):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        now = self.clock_ms()
        values = self._authority_values(authority)
        try:
            with self._db() as db:
                duplicate = db.execute(
                    "SELECT id FROM jobs WHERE final_sha256=? AND id<>?",
                    (authority["final_sha256"], row["id"]),
                ).fetchone()
                if duplicate:
                    raise RenderJobError("duplicate_output")
                changed = db.execute(
                    "UPDATE jobs SET state='succeeded',receipt_sha256=?,final_sha256=?,"
                    "qa_frame_sha256=?,final_byte_length=?,output_authority_json=?,updated_at_ms=?,"
                    "error_code=NULL WHERE id=? AND attempt_id=? AND state='running'",
                    (*values, now, row["id"], row["attempt_id"]),
                ).rowcount
                if changed != 1:
                    raise RenderJobError("attempt_fenced")
                db.execute("UPDATE attempts SET state='succeeded',ended_at_ms=? WHERE id=?", (now, row["attempt_id"]))
        except sqlite3.IntegrityError as error:
            raise RenderJobError("duplicate_output") from error

    def run_one(self) -> bool:
        self._gc()
        permit = next((lock for index in range(self.worker_count) if (lock := _locked(self.root / "locks" / f"worker-{index}.lock")) is not None), None)
        if permit is None:
            return False
        with permit:
            if not self._has_workspace_capacity():
                return False
            with self._db() as db:
                # Running rows (restart recovery) stay at least as prioritized as before by
                # always sorting ahead of queued rows. Among the rest, earliest planned
                # posting time first; rows with no planned_at_ms (the slot payload carried
                # no parseable planned_at/planned_at_ms, e.g. legacy rows) sort last and
                # fall back to created_at_ms.
                rows = db.execute("""
                    SELECT * FROM jobs WHERE state IN ('queued','running') AND available_at_ms<=?
                    ORDER BY
                        CASE WHEN state = 'running' THEN 0 ELSE 1 END,
                        CASE WHEN planned_at_ms IS NULL THEN 1 ELSE 0 END,
                        planned_at_ms,
                        created_at_ms
                    LIMIT 100
                """, (self.clock_ms(),)).fetchall()
            for row in rows:
                lock = _locked(self.root / "locks" / (row["id"] + ".lock"))
                if lock is None:
                    continue
                with lock:
                    row = self._row(row["id"])
                    if row["state"] not in {"queued", "running"}:
                        continue
                    if row["state"] == "running":
                        try:
                            self._finish(row, self._verify_output(row))
                            return True
                        except (OSError, ValueError, TypeError):
                            pass
                    self._execute(row)
                    return True
        return False

    def _execute(self, old):
        if not self._has_workspace_capacity(job_id=old["id"]):
            return
        now, attempt_id = self.clock_ms(), uuid.uuid4().hex
        with self._db() as db:
            if old["attempts"] >= MAX_ATTEMPTS:
                db.execute("UPDATE jobs SET state='failed',error_code='retry_budget_exhausted',updated_at_ms=? WHERE id=?", (now, old["id"]))
                return
            if old["state"] == "running":
                db.execute("UPDATE attempts SET state='interrupted',ended_at_ms=?,error_code='incomplete_after_restart' WHERE id=?", (now, old["attempt_id"]))
            db.execute("UPDATE jobs SET state='running',attempt_id=?,attempts=attempts+1,updated_at_ms=?,error_code=NULL,receipt_sha256=NULL WHERE id=?", (attempt_id, now, old["id"]))
            db.execute("INSERT INTO attempts(id,job_id,state,started_at_ms) VALUES(?,?,'running',?)", (attempt_id, old["id"], now))
        row = self._row(old["id"])
        attempt_root = self._output(row).parent
        attempt_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        source = attempt_root / "source.mp4"
        try:
            submission = RenderJobSubmission.model_validate_json(row["submission_json"])
            self.fetcher(submission.request, source)
            self.renderer(source, self._output(row), submission.request, clock_ms=self.clock_ms)
            authority = self._verify_output(row)
            self._finish(row, authority)
        except Exception as error:
            code = getattr(error, "code", "render_failed")
            log.warning("post render attempt failed job=%s attempt=%s reason=%s", row["id"], attempt_id, code)
            now = self.clock_ms()
            state = "queued" if code in RETRY_CODES and row["attempts"] < MAX_ATTEMPTS else "failed"
            with self._db() as db:
                db.execute("UPDATE jobs SET state=?,error_code=?,available_at_ms=?,updated_at_ms=? WHERE id=? AND attempt_id=? AND state='running'",
                           (state, code, now + 5000 * row["attempts"], now, row["id"], attempt_id))
                db.execute("UPDATE attempts SET state='failed',error_code=?,ended_at_ms=? WHERE id=?", (code, now, attempt_id))
        finally:
            # This authenticated download is scratch, not an artifact or receipt.
            # Failed attempts must not fill the persistent render volume.
            try:
                source.unlink(missing_ok=True)
            except OSError:
                log.exception("post render source cleanup failed job=%s attempt=%s", row["id"], attempt_id)

    def workers_alive(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def queue_age_ms(self) -> int:
        with self._db() as db:
            row = db.execute(
                "SELECT MIN(created_at_ms) FROM jobs WHERE state IN ('queued','running')",
            ).fetchone()
        if row is None or row[0] is None:
            return 0
        return max(0, self.clock_ms() - row[0])

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        def worker():
            while not self._stop.is_set():
                try:
                    worked = self.run_one()
                except Exception:
                    log.exception("post render worker iteration failed")
                    worked = False
                if not worked:
                    self._stop.wait(1)
        self._threads = [threading.Thread(target=worker, name=f"post-render-{index}", daemon=True) for index in range(self.worker_count)]
        for thread in self._threads:
            thread.start()

        # Age-out has its own thread so a sweep never delays a render claim.
        def age_out():
            while not self._stop.is_set():
                try:
                    self._maybe_age_out()
                except Exception:
                    log.exception("post render age-out iteration failed")
                self._stop.wait(30)
        self._age_out_thread = threading.Thread(target=age_out, name="post-render-age-out", daemon=True)
        self._age_out_thread.start()

    def stop(self):
        self._stop.set()
        for thread in [*self._threads, self._age_out_thread]:
            if thread is not None:
                thread.join(timeout=2)
