"""Tests for the R2 direct-upload init/complete expiry contract in clipper.py.

The init→complete handshake is implicit: /r2/upload-init hands out presigned PUT
URLs with a TTL, and /r2/upload-complete pulls them back from R2. If the URLs
expire between the two calls, the fetch fails with a confusing S3 error. These
tests cover the explicit deadline contract added to make that failure clear:

  - /r2/upload-init returns an `expires_at` epoch-seconds deadline.
  - /r2/upload-complete rejects an already-expired batch with HTTP 409 *before*
    attempting any R2 fetch, telling the client to re-initialize.
"""

import time

import pytest
from fastapi import HTTPException

from routers import clipper
import services.r2 as r2


# ── /r2/upload-init returns an explicit deadline ─────────────────────


async def test_upload_init_returns_future_expiry(monkeypatch):
    monkeypatch.setattr(r2, "is_configured", lambda: True)
    monkeypatch.setattr(r2, "upload_key", lambda *a, **k: "some/key.mp4")
    monkeypatch.setattr(r2, "presign_put", lambda *a, **k: "https://r2.example/put")
    monkeypatch.setattr(r2, "UPLOAD_TTL", 3600)

    before = int(time.time())
    resp = await clipper.r2_upload_init({
        "project": "quick-test",
        "files": [{"filename": "a.mp4", "content_type": "video/mp4"}],
    })
    after = int(time.time())

    assert "expires_at" in resp
    # deadline is roughly now + TTL
    assert before + 3600 <= resp["expires_at"] <= after + 3600
    assert resp["items"][0]["put_url"] == "https://r2.example/put"


# ── /r2/upload-complete enforces the deadline ────────────────────────


async def test_upload_complete_rejects_expired_batch(monkeypatch):
    monkeypatch.setattr(r2, "is_configured", lambda: True)
    # download_to_path must NOT be called when the batch is already expired.
    def _boom(*a, **k):
        raise AssertionError("R2 fetch attempted on an expired batch")
    monkeypatch.setattr(r2, "download_to_path", _boom)

    with pytest.raises(HTTPException) as exc:
        await clipper.r2_upload_complete({
            "project": "quick-test",
            "batch_id": "deadbeef0000",
            "items": [{"index": 0, "filename": "a.mp4", "key": "some/key.mp4"}],
            "expires_at": int(time.time()) - 5,  # already passed
        })
    assert exc.value.status_code == 409
    assert "re-initialize" in exc.value.detail


async def test_upload_complete_ignores_missing_or_malformed_expiry(monkeypatch):
    """Without a deadline (or a garbage one) the call must fall through to the
    normal fetch path rather than 409 — backwards compatible with old clients."""
    monkeypatch.setattr(r2, "is_configured", lambda: True)

    fetched: list[str] = []

    def fake_download(key, path):  # sync — runs via asyncio.to_thread
        fetched.append(key)
        return 0  # 0 bytes → treated as empty, no ffmpeg/probe invoked

    monkeypatch.setattr(r2, "download_to_path", fake_download)

    for body_extra in ({}, {"expires_at": "not-a-number"}):
        fetched.clear()
        body = {
            "project": "quick-test",
            "batch_id": "deadbeef0000",
            "items": [{"index": 0, "filename": "a.mp4", "key": "some/key.mp4"}],
            **body_extra,
        }
        # All items "empty" → endpoint raises 400 (not 409); the point is that
        # it reached the fetch path instead of short-circuiting on expiry.
        with pytest.raises(HTTPException) as exc:
            await clipper.r2_upload_complete(body)
        assert exc.value.status_code == 400
        assert fetched == ["some/key.mp4"]
