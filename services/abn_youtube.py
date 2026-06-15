"""YouTube publish layer for ABN episodes — stdlib-only, dormant until OAuth creds exist.

This is the thin module that ``routers/agenticnews.py`` (the /publish + /publish-package
endpoints) and ``services/abn_factory.py`` (the auto-publish loop) import. Both call sites
gate every real network action behind :func:`is_configured`, so with no creds set this module
is inert — ``is_configured()`` returns ``False`` and nobody ever calls :func:`upload`.

Config comes from three env vars (a standard installed-app OAuth refresh-token flow):

    YT_CLIENT_ID       OAuth client id
    YT_CLIENT_SECRET   OAuth client secret
    YT_REFRESH_TOKEN   long-lived refresh token for the @agenticbuildernews channel

No third-party SDK: the refresh + resumable upload run over ``urllib`` against Google's HTTP
APIs. Keeping it dependency-free means the import never breaks the endpoints even before the
channel/creds exist (the original bug: the module was simply missing, 500-ing every caller).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)


def _creds() -> tuple[str, str, str]:
    return (
        os.environ.get("YT_CLIENT_ID", ""),
        os.environ.get("YT_CLIENT_SECRET", ""),
        os.environ.get("YT_REFRESH_TOKEN", ""),
    )


def is_configured() -> bool:
    """True only when all three OAuth env vars are present. Both callers gate on this."""
    return all(_creds())


def _access_token() -> str:
    """Exchange the refresh token for a short-lived access token (stdlib urllib)."""
    cid, secret, refresh = _creds()
    data = urllib.parse.urlencode(
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        tok = json.loads(resp.read().decode())
    return tok["access_token"]


def upload(pkg: dict, privacy: str = "private") -> dict:
    """Upload a publish-package's rendered video to YouTube.

    ``pkg`` is the dict built by ``publish_package`` (video_path, title, description, ...).
    Returns ``{"ok": True, "url", "video_id"}`` on success, ``{"ok": False, "error"}`` otherwise.
    Only ever invoked when :func:`is_configured` is True (guarded by both call sites).
    """
    if not is_configured():
        return {"ok": False, "error": "YouTube OAuth not configured"}
    video_path = pkg.get("video_path")
    if not video_path or not os.path.exists(video_path):
        return {"ok": False, "error": f"video not found: {video_path!r}"}
    if privacy not in ("private", "unlisted", "public"):
        privacy = "private"

    try:
        token = _access_token()
        metadata = {
            "snippet": {
                "title": (pkg.get("title") or "Agentic Builder News")[:100],
                "description": pkg.get("description", ""),
                "categoryId": "28",  # Science & Technology
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        size = os.path.getsize(video_path)
        meta_bytes = json.dumps(metadata).encode()

        # Step 1: open a resumable session, get the upload URL.
        init = urllib.request.Request(
            _UPLOAD_URL,
            data=meta_bytes,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/*",
                "X-Upload-Content-Length": str(size),
            },
        )
        with urllib.request.urlopen(init, timeout=120) as resp:
            session_url = resp.headers.get("Location")
        if not session_url:
            return {"ok": False, "error": "no resumable session URL returned"}

        # Step 2: send the bytes in one PUT (resumable session, single chunk).
        with open(video_path, "rb") as fh:
            body = fh.read()
        put = urllib.request.Request(
            session_url,
            data=body,
            method="PUT",
            headers={"Content-Type": "video/*", "Content-Length": str(size)},
        )
        with urllib.request.urlopen(put, timeout=3600) as resp:
            result = json.loads(resp.read().decode())

        vid = result.get("id")
        if not vid:
            return {"ok": False, "error": f"upload returned no video id: {result}"}
        return {"ok": True, "video_id": vid, "url": f"https://youtu.be/{vid}"}
    except urllib.error.HTTPError as e:  # pragma: no cover - network path
        detail = e.read().decode(errors="replace") if hasattr(e, "read") else str(e)
        return {"ok": False, "error": f"HTTP {e.code}: {detail}"}
    except Exception as e:  # pragma: no cover - network path
        return {"ok": False, "error": str(e)}
