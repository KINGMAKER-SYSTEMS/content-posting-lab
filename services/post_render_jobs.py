"""Durable pre-lease render jobs. No phone, schedule mutation, or caller-selected URL."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.post_render import (
    Hash, Identity, MAX_SOURCE_BYTES, MAX_FINAL_BYTES, MAX_QA_BYTES,
    PostRenderError, PostRenderRequest, RenderedPost, render_post, sha256, source_visual_matches,
)

log = logging.getLogger("content_lab.post_render_jobs")

JOB_SCHEMA = "content-lab.post-render-job.v1"
MAX_ATTEMPTS = 3
RETRY_CODES = {"source_unavailable", "source_timeout", "process_timeout", "process_unavailable"}


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
        return cls(os.getenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "").rstrip("/"),
                   os.getenv("CONTROL_PLANE_SERVICE_ID", ""), os.getenv("CONTROL_PLANE_SERVICE_SECRET", ""))


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
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000):
        if not root.is_absolute():
            raise RenderJobError("job_root_must_be_absolute")
        self.root, self.renderer, self.clock_ms = root, renderer, clock_ms
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
                db.execute("INSERT INTO jobs(id,slot_id,slot_hash,request_hash,submission_json,state,available_at_ms,created_at_ms,updated_at_ms,error_code) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (job_id, request.slot_id, request.slot_payload_sha256, request_hash, payload, state, now, now, now, reason))
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
        result = {key: row[key] for key in ("id", "slot_id", "slot_hash", "state", "attempts", "created_at_ms", "updated_at_ms", "error_code")}
        result["schema"] = "content-lab.post-render-status.v1"
        if row["state"] == "regeneration_needed":
            result["next_action"] = "regenerate_source_with_treatment_provenance"
        result["retryable"] = row["state"] == "failed" and row["attempts"] < MAX_ATTEMPTS
        if row["state"] == "succeeded":
            result["receipt_sha256"] = row["receipt_sha256"]
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

    def _verify_output(self, row) -> str:
        """Recover only a completed hash-bound renderer result; partial directories never serve."""
        from services.post_render import _file_hash
        request = RenderJobSubmission.model_validate_json(row["submission_json"]).request
        output = self._output(row)
        with (output / "receipt.json").open("rb") as file:
            receipt_bytes = file.read(64 * 1024 + 1)
        if len(receipt_bytes) > 64 * 1024:
            raise RenderJobError("receipt_invalid")
        receipt = json.loads(receipt_bytes)
        if receipt.get("schema") != "posting-prepared-artifact/v1":
            raise RenderJobError("receipt_invalid")
        for field in ("slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial", "account",
                      "source_sha256", "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version"):
            if receipt.get(field) != getattr(request, field):
                raise RenderJobError("receipt_binding_mismatch")
        final_sha, final_length = _file_hash(output / "final.mp4", MAX_FINAL_BYTES)
        qa_sha, _ = _file_hash(output / "qa.jpg", MAX_QA_BYTES)
        if (receipt.get("final_sha256") != final_sha or receipt.get("final_byte_length") != final_length
                or receipt.get("qa_frame_sha256") != qa_sha or final_sha == request.source_sha256
                or receipt.get("final_object_key") != f"posting/final/{final_sha}.mp4"
                or receipt.get("mime_type") != "video/mp4" or receipt.get("width") != 1080 or receipt.get("height") != 1920
                or not request.created_at_ms <= receipt.get("completed_at_ms", -1) <= self.clock_ms()):
            raise RenderJobError("artifact_binding_mismatch")
        with (output / "decode.json").open("rb") as file:
            evidence_bytes = file.read(64 * 1024 + 1)
        if len(evidence_bytes) > 64 * 1024:
            raise RenderJobError("decode_evidence_invalid")
        evidence = json.loads(evidence_bytes)
        if (evidence.get("final_sha256") != final_sha or evidence.get("qa_frame_sha256") != qa_sha
                or evidence.get("decoded_video_frames", 0) <= 0
                or evidence.get("final_probe", {}).get("duration_ms") != receipt.get("duration_ms")
                or receipt.get("duration_ms", 0) <= 0):
            raise RenderJobError("decode_evidence_missing")
        receipt_sha = sha256(receipt_bytes)
        if row["receipt_sha256"] and row["receipt_sha256"] != receipt_sha:
            raise RenderJobError("receipt_changed")
        return receipt_sha

    def artifact(self, job_id: str, kind: str) -> Path:
        names = {"final": "final.mp4", "qa": "qa.jpg", "receipt": "receipt.json"}
        if kind not in names:
            raise RenderJobError("artifact_not_found")
        row = self._row(job_id)
        if row["state"] != "succeeded":
            raise RenderJobError("artifact_not_ready")
        self._verify_output(row)
        return self._output(row) / names[kind]

    def _finish(self, row, receipt_sha: str):
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
        with self._db() as db:
            changed = db.execute("UPDATE jobs SET state='succeeded',receipt_sha256=?,updated_at_ms=?,error_code=NULL WHERE id=? AND attempt_id=? AND state='running'",
                                 (receipt_sha, now, row["id"], row["attempt_id"])).rowcount
            if changed != 1:
                raise RenderJobError("attempt_fenced")
            db.execute("UPDATE attempts SET state='succeeded',ended_at_ms=? WHERE id=?", (now, row["attempt_id"]))

    def run_one(self) -> bool:
        permit = next((lock for index in range(2) if (lock := _locked(self.root / "locks" / f"worker-{index}.lock")) is not None), None)
        if permit is None:
            return False
        with permit:
            with self._db() as db:
                rows = db.execute("SELECT * FROM jobs WHERE state IN ('queued','running') AND available_at_ms<=? ORDER BY created_at_ms LIMIT 100", (self.clock_ms(),)).fetchall()
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
        try:
            submission = RenderJobSubmission.model_validate_json(row["submission_json"])
            source = attempt_root / "source.mp4"
            self.fetcher(submission.request, source)
            self.renderer(source, self._output(row), submission.request, clock_ms=self.clock_ms)
            receipt_sha = self._verify_output(row)
            self._finish(row, receipt_sha)
            source.unlink(missing_ok=True)
        except Exception as error:
            code = getattr(error, "code", "render_failed")
            log.warning("post render attempt failed job=%s attempt=%s reason=%s", row["id"], attempt_id, code)
            now = self.clock_ms()
            state = "queued" if code in RETRY_CODES and row["attempts"] < MAX_ATTEMPTS else "failed"
            with self._db() as db:
                db.execute("UPDATE jobs SET state=?,error_code=?,available_at_ms=?,updated_at_ms=? WHERE id=? AND attempt_id=? AND state='running'",
                           (state, code, now + 5000 * row["attempts"], now, row["id"], attempt_id))
                db.execute("UPDATE attempts SET state='failed',error_code=?,ended_at_ms=? WHERE id=?", (code, now, attempt_id))

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
        self._threads = [threading.Thread(target=worker, name=f"post-render-{index}", daemon=True) for index in range(2)]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
