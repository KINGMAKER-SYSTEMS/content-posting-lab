"""
Per-poster content federation.

Historically content was delegated per page (one staging/poster topic per
page, high-water marks keyed by page). This module re-federates that view to
be *per poster*: given a poster, it resolves the set of pages that poster runs
from the Notion Master Pages roster, then aggregates the rendered videos those
pages can draw from.

Source of truth:
  - Which pages a poster runs  → Master Pages (cached in page_roster.json,
    matched via poster_name → registered poster). Manually-assigned page_ids
    on the Telegram poster record are unioned in so nothing already wired up
    stops delivering.
  - Where a page's content lives → roster page's `project` → projects/<project>.

This is a read/aggregation layer only; it does not send anything.
"""

import logging
from typing import Any

from project_manager import sanitize_project_name
from services.poster_router import resolve_poster_for_page
from services.roster import list_all_pages
from services.telegram import get_poster

log = logging.getLogger("services.poster_content")


def pages_for_poster(poster: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the roster pages this poster runs.

    Primary source is the Master Pages roster (each page's `poster_name`
    resolved to a registered poster). Pages explicitly listed on the poster's
    `page_ids` are unioned in as an override so manual assignments still work.
    """
    poster_id = poster.get("poster_id")
    if not poster_id:
        return []

    roster_pages = list_all_pages()
    by_id = {p.get("integration_id"): p for p in roster_pages}

    picked: dict[str, dict[str, Any]] = {}

    # 1) Master-Pages-driven: pages whose poster_name resolves to this poster.
    for page in roster_pages:
        resolved = resolve_poster_for_page(page)
        if resolved and resolved.get("poster_id") == poster_id:
            iid = page.get("integration_id")
            if iid:
                picked[iid] = page

    # 2) Manual override: anything explicitly assigned on the poster record.
    for iid in poster.get("page_ids", []) or []:
        if iid in picked:
            continue
        page = by_id.get(iid)
        if page is not None:
            picked[iid] = page
        else:
            # Assigned but not (yet) in the roster — surface a minimal stub so
            # the poster still sees the page exists.
            picked[iid] = {"integration_id": iid, "name": iid, "source": "assigned"}

    return list(picked.values())


def _static_url_for(project: str, video_path: str) -> str:
    """Build the static-mount URL for a scanned video entry.

    `_scan_project_videos` returns paths relative to the project's videos/
    dir, except clip entries which are already prefixed with "clips/".
    Both resolve under the /projects static mount.
    """
    sanitized = sanitize_project_name(project)
    if video_path.startswith("clips/"):
        return f"/projects/{sanitized}/{video_path}"
    return f"/projects/{sanitized}/videos/{video_path}"


def videos_for_poster(
    poster: dict[str, Any],
    page_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate rendered videos across the poster's pages.

    Returns a dict with `pages` (each with a video_count) and a flat `videos`
    list tagged with the originating page. Optionally filter to one page_id.
    """
    # Imported lazily to avoid a circular import at module load time
    # (routers.burn pulls in a lot of the app).
    from routers.burn import _scan_project_videos

    pages = pages_for_poster(poster)
    if page_id is not None:
        pages = [p for p in pages if p.get("integration_id") == page_id]

    # Scan each distinct project once, then fan the results back out to pages.
    project_cache: dict[str, list[dict[str, Any]]] = {}
    page_summaries: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []

    # Projects whose scan raised — tracked so we don't silently report 0 videos
    # as if the page is simply empty (silence here = missing content / SLA miss).
    failed_projects: set[str] = set()

    for page in pages:
        iid = page.get("integration_id")
        project = page.get("project")
        count = 0
        if project:
            if project not in project_cache:
                try:
                    project_cache[project] = _scan_project_videos(project)
                except Exception:
                    # Don't swallow: log with traceback and flag the page so the
                    # caller can distinguish "scan failed" from "no videos".
                    log.exception(
                        "poster_content: video scan failed for project %r "
                        "(page %r); reporting scan_error to caller",
                        project,
                        iid,
                    )
                    project_cache[project] = []
                    failed_projects.add(project)
            for v in project_cache[project]:
                videos.append(
                    {
                        "page_id": iid,
                        "page_name": page.get("name"),
                        "project": project,
                        "path": v["path"],
                        "name": v["name"],
                        "folder": v.get("folder", ""),
                        "created": v.get("created"),
                        "url": _static_url_for(project, v["path"]),
                    }
                )
                count += 1
        page_summaries.append(
            {
                "integration_id": iid,
                "name": page.get("name"),
                "project": project,
                "provider": page.get("provider"),
                "status": page.get("status"),
                "video_count": count,
                "scan_error": project in failed_projects,
            }
        )

    videos.sort(key=lambda v: v.get("created") or 0, reverse=True)
    return {
        "pages": page_summaries,
        "videos": videos,
        "scan_errors": sorted(failed_projects),
    }


def poster_summary(poster: dict[str, Any]) -> dict[str, Any]:
    """A compact poster profile for the Mini App home screen."""
    pages = pages_for_poster(poster)
    return {
        "poster_id": poster.get("poster_id"),
        "name": poster.get("name"),
        "page_count": len(pages),
        "pages": [
            {
                "integration_id": p.get("integration_id"),
                "name": p.get("name"),
                "project": p.get("project"),
                "provider": p.get("provider"),
                "status": p.get("status"),
            }
            for p in pages
        ],
    }


def get_poster_or_none(poster_id: str) -> dict[str, Any] | None:
    return get_poster(poster_id)
