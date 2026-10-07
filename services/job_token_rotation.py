"""Operator-only, single-job signed URL credential rotation.

This module is intentionally outside the HTTP router. It never reads a
production path implicitly and never returns or prints a download token.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.generation_recovery import store_lock
from services.json_store import atomic_save

JOB_ID_RE = re.compile(r"cpl-[0-9a-f]{16}\Z")
REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
TERMINAL = frozenset({"completed", "failed", "cancelled"})
TOKEN_BYTES = 24  # Match routers.control_plane.JOB_TOKEN_BYTES without importing the router.
MAX_STORE_BYTES = 512 * 1024 * 1024


class RotationRefused(Exception):
    """A bounded, secret-free operator diagnostic."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _check_store_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RotationRefused("store_unavailable")
    try:
        if path.stat().st_size > MAX_STORE_BYTES:
            raise RotationRefused("store_too_large")
    except OSError as exc:
        raise RotationRefused("store_unavailable") from exc


def _load_strict(path: Path) -> dict[str, Any]:
    _check_store_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_STORE_BYTES:
                raise RotationRefused("store_unavailable")
            store = json.load(stream)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RotationRefused("store_unreadable") from exc
    if not isinstance(store, dict) or not isinstance(store.get("jobs"), dict):
        raise RotationRefused("store_invalid")
    return store


def _revision(job: dict[str, Any]) -> str:
    encoded = json.dumps(job, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _metadata(store: dict[str, Any], job_id: str) -> dict[str, Any]:
    job = store["jobs"].get(job_id)
    if not isinstance(job, dict):
        return {"status": "not_found_or_archived", "jobId": job_id}
    if job.get("jobId") != job_id:
        raise RotationRefused("job_identity_invalid")
    status = job.get("status")
    if status not in TERMINAL or not isinstance(job.get("token"), str) or not job["token"]:
        return {"status": "not_rotatable", "jobId": job_id}
    return {
        "status": "ready", "jobId": job_id, "jobStatus": status,
        "jobRevisionSha256": _revision(job),
        "artifactCount": len(job.get("clips") or []),
    }


def inspect_job(store_path: Path, job_id: str) -> dict[str, Any]:
    """Dry-run metadata only; no lock file, token, or store write."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise RotationRefused("job_id_invalid")
    return _metadata(_load_strict(store_path), job_id)


def rotate_job(
    store_path: Path, job_id: str, *, expected_job_sha256: str,
    reason: str = "exposed_signed_url",
) -> dict[str, Any]:
    """Compare-and-set one terminal job's token under the existing store lock."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise RotationRefused("job_id_invalid")
    if not REVISION_RE.fullmatch(expected_job_sha256):
        raise RotationRefused("expected_revision_invalid")
    if reason not in {"exposed_signed_url", "scheduled_credential_rotation"}:
        raise RotationRefused("reason_invalid")
    _check_store_path(store_path)
    with store_lock(store_path):
        store = _load_strict(store_path)
        metadata = _metadata(store, job_id)
        if metadata["status"] != "ready":
            return metadata
        if not secrets.compare_digest(metadata["jobRevisionSha256"], expected_job_sha256):
            raise RotationRefused("job_changed_retry_dry_run")
        job = store["jobs"][job_id]
        previous_audit = job.get("signedUrlTokenRotation")
        prior_count = previous_audit.get("count") if isinstance(previous_audit, dict) else 0
        if not isinstance(prior_count, int) or isinstance(prior_count, bool) or not 0 <= prior_count < 1_000_000:
            raise RotationRefused("rotation_audit_invalid")
        replacement = secrets.token_urlsafe(TOKEN_BYTES)
        if secrets.compare_digest(replacement, job["token"]):
            raise RotationRefused("token_generation_collision")
        job["token"] = replacement
        job["signedUrlTokenRotation"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "count": prior_count + 1,
        }
        # This is a standalone operator process; a private umask keeps the
        # shared atomic_save temporary file and replacement private as well.
        previous_umask = os.umask(0o077)
        try:
            try:
                atomic_save(store_path, store)
            except Exception as exc:
                raise RotationRefused("store_write_failed") from exc
        finally:
            os.umask(previous_umask)
        return {
            "status": "rotated", "jobId": job_id,
            "jobRevisionSha256": _revision(job),
            "rotatedAt": job["signedUrlTokenRotation"]["at"],
        }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint included in the production image's services/ copy."""
    import argparse

    parser = argparse.ArgumentParser(description="Inspect or rotate one job's signed URL credential")
    parser.add_argument("--store", type=Path, required=True, help="absolute existing job-store JSON path")
    parser.add_argument("--job-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show target metadata only (default)")
    mode.add_argument("--apply", action="store_true", help="perform the compare-and-set rotation")
    parser.add_argument("--expected-job-sha256", help="job revision from the dry-run output")
    parser.add_argument("--reason", choices=("exposed_signed_url", "scheduled_credential_rotation"),
                        default="exposed_signed_url")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if not args.expected_job_sha256:
                parser.error("--apply requires --expected-job-sha256 from a fresh dry-run")
            result = rotate_job(
                args.store, args.job_id,
                expected_job_sha256=args.expected_job_sha256, reason=args.reason,
            )
        else:
            if args.expected_job_sha256:
                parser.error("--expected-job-sha256 is only used with --apply")
            result = inspect_job(args.store, args.job_id)
    except RotationRefused as exc:
        print(json.dumps({"status": "refused", "reason": exc.code}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] != "not_rotatable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
