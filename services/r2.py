"""R2 (Cloudflare S3-compatible) service layer.

Central place for presigned URLs, uploads, downloads, and key-space conventions.
All Clipper large-file I/O should go through here, never through the Railway edge proxy.

Env vars required:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterable, Optional

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("r2")

_CLIENT: Optional[BaseClient] = None
_CLIENT_LOCK = threading.Lock()

# Presigned URL lifetimes
UPLOAD_TTL = 6 * 3600     # 6h — large files on slow connections
DOWNLOAD_TTL = 24 * 3600  # 24h — team should always be able to re-fetch within a workday


class R2NotConfigured(RuntimeError):
    """Raised when R2 env vars are missing."""


def _bucket() -> str:
    b = os.getenv("R2_BUCKET")
    if not b:
        raise R2NotConfigured("R2_BUCKET env var is not set")
    return b


def client() -> BaseClient:
    """Return a cached boto3 S3 client configured for R2."""
    global _CLIENT
    cached = _CLIENT
    if cached is not None:
        return cached
    with _CLIENT_LOCK:
        cached = _CLIENT
        if cached is not None:
            return cached
        endpoint = os.getenv("R2_ENDPOINT")
        key = os.getenv("R2_ACCESS_KEY_ID")
        secret = os.getenv("R2_SECRET_ACCESS_KEY")
        if not (endpoint and key and secret):
            raise R2NotConfigured("R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY must be set")
        new_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                max_pool_connections=20,
            ),
        )
        _CLIENT = new_client
        return new_client


def is_configured() -> bool:
    try:
        client()
        return True
    except R2NotConfigured:
        return False


# ── Key conventions ─────────────────────────────────────────────────────
# uploads/{project}/{batch_id}/{index:03d}_{filename}  → raw user uploads
# clips/{project}/{job_id}/clip_NNN.mp4                → finished clips
#
# Keeping project in the key path makes it easy to reason about scoping
# and to list everything for a given project.

def upload_key(project: str, batch_id: str, index: int, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"uploads/{project}/{batch_id}/{index:03d}_{safe_name}"


def clip_key(project: str, job_id: str, clip_name: str) -> str:
    return f"clips/{project}/{job_id}/{clip_name}"


# ── Presigned URLs ──────────────────────────────────────────────────────

def presign_put(key: str, content_type: str = "application/octet-stream", ttl: int = UPLOAD_TTL) -> str:
    return client().generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=ttl,
        HttpMethod="PUT",
    )


def presign_get(key: str, ttl: int = DOWNLOAD_TTL, download_as: Optional[str] = None) -> str:
    params = {"Bucket": _bucket(), "Key": key}
    if download_as:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_as}"'
    return client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl,
        HttpMethod="GET",
    )


# ── Server-side I/O (for pulling R2 → local volume for ffmpeg) ──────────

def download_to_path(key: str, dest: Path) -> int:
    """Stream an R2 object to a local path. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    client().download_file(_bucket(), key, str(dest))
    return dest.stat().st_size


def upload_from_path(key: str, src: Path, content_type: str = "video/mp4") -> None:
    """Upload a local file to R2 (multipart under the hood for large files)."""
    extra = {"ContentType": content_type}
    client().upload_file(str(src), _bucket(), key, ExtraArgs=extra)


def head(key: str) -> Optional[dict]:
    """Return object metadata or None if the object doesn't exist."""
    try:
        return client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def delete_many(keys: Iterable[str]) -> int:
    """Delete up to 1000 keys in a single request. Returns count deleted."""
    batch = [{"Key": k} for k in keys]
    if not batch:
        return 0
    resp = client().delete_objects(
        Bucket=_bucket(),
        Delete={"Objects": batch, "Quiet": True},
    )
    errs = resp.get("Errors") or []
    if errs:
        log.warning("r2 delete errors: %s", errs[:5])
    return len(batch) - len(errs)


def delete_prefix(prefix: str) -> int:
    """Delete every object under a prefix. Returns total deleted."""
    c = client()
    total = 0
    token = None
    while True:
        kwargs = {"Bucket": _bucket(), "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = c.list_objects_v2(**kwargs)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        if keys:
            total += delete_many(keys)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return total
