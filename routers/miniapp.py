"""
Telegram Mini App API.

The lightweight client posters open from Telegram. Endpoints under
/api/miniapp/* are authenticated via Telegram `initData` (validated against the
bot token) and scoped to the poster that the calling Telegram user acts as:

  GET  /api/miniapp/me            → poster profile + the pages they run
  GET  /api/miniapp/videos        → rendered videos across those pages
  GET  /api/miniapp/requests      → the poster's own content requests
  POST /api/miniapp/requests      → file a new content request

Agent-facing endpoints (gated by X-Agent-Key when MINIAPP_AGENT_KEY is set)
let the external content agent read/work the request queue:

  GET   /api/miniapp/agent/requests
  PATCH /api/miniapp/agent/requests/{request_id}
"""

import os

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from services import content_requests
from services.miniapp_auth import AuthError, resolve_poster_from_request
from services.poster_content import poster_summary, videos_for_poster

router = APIRouter()


# ── Auth helper ──────────────────────────────────────────────────────


def _require_poster(request: Request) -> dict:
    """Resolve the authenticated poster or raise an HTTP error.

    initData is read from the `X-Telegram-Init-Data` header (preferred) or an
    `Authorization: tma <initData>` header. The dev bypass reads
    `X-Dev-Poster-Id` (only honored when MINIAPP_DEV_AUTH is enabled).
    """
    init_data = request.headers.get("x-telegram-init-data")
    if not init_data:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("tma "):
            init_data = auth[4:].strip()
    dev_poster_id = request.headers.get("x-dev-poster-id")

    try:
        return resolve_poster_from_request(init_data, dev_poster_id=dev_poster_id)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


def _require_agent_key(x_agent_key: str | None) -> None:
    """Gate agent endpoints behind MINIAPP_AGENT_KEY when it is configured."""
    expected = os.getenv("MINIAPP_AGENT_KEY", "").strip()
    if not expected:
        return  # not configured → open, consistent with the rest of the app
    if not x_agent_key or x_agent_key.strip() != expected:
        raise HTTPException(status_code=401, detail="invalid or missing agent key")


# ── Request bodies ───────────────────────────────────────────────────


class ContentRequestBody(BaseModel):
    text: str
    page_id: str | None = None
    quantity: int | None = None


class AgentUpdateBody(BaseModel):
    status: str | None = None
    agent_note: str | None = None


# ── Poster-facing endpoints ──────────────────────────────────────────


@router.get("/me")
async def me(request: Request):
    """Poster profile plus the pages they run (from Master Pages)."""
    poster = _require_poster(request)
    return poster_summary(poster)


@router.get("/videos")
async def my_videos(request: Request, page_id: str | None = Query(default=None)):
    """Rendered videos aggregated across the poster's pages."""
    poster = _require_poster(request)
    result = videos_for_poster(poster, page_id=page_id)
    return {"poster_id": poster.get("poster_id"), **result}


@router.get("/requests")
async def my_requests(request: Request, status: str | None = Query(default=None)):
    """List the calling poster's content requests."""
    poster = _require_poster(request)
    return {
        "requests": content_requests.list_requests(
            poster_id=poster.get("poster_id"), status=status
        )
    }


@router.post("/requests", status_code=201)
async def create_request(request: Request, body: ContentRequestBody):
    """File a content request for the calling poster."""
    poster = _require_poster(request)
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="text is required")

    page_name = None
    if body.page_id:
        for p in poster_summary(poster)["pages"]:
            if p["integration_id"] == body.page_id:
                page_name = p["name"]
                break

    entry = content_requests.add_request(
        poster_id=poster.get("poster_id"),
        poster_name=poster.get("name"),
        text=body.text,
        page_id=body.page_id,
        page_name=page_name,
        quantity=body.quantity,
        source="miniapp",
    )
    return entry


# ── Agent-facing endpoints ───────────────────────────────────────────


@router.get("/agent/requests")
async def agent_list_requests(
    status: str | None = Query(default="open"),
    poster_id: str | None = Query(default=None),
    x_agent_key: str | None = Header(default=None),
):
    """The external agent polls this for requests to work."""
    _require_agent_key(x_agent_key)
    # status="" (empty) means "all"
    status_filter = status or None
    return {
        "requests": content_requests.list_requests(
            poster_id=poster_id, status=status_filter
        )
    }


@router.patch("/agent/requests/{request_id}")
async def agent_update_request(
    request_id: str,
    body: AgentUpdateBody,
    x_agent_key: str | None = Header(default=None),
):
    """The agent marks a request in_progress / fulfilled / cancelled."""
    _require_agent_key(x_agent_key)
    try:
        updated = content_requests.update_request(
            request_id, status=body.status, agent_note=body.agent_note
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="request not found")
    return updated
