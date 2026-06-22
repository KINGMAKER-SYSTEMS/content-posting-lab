"""Tests for poster_content.videos_for_poster scan-error surfacing.

A failed _scan_project_videos must NOT be silently swallowed into an empty
result: the page summary should carry scan_error=True and the top-level result
should list the failed project, so callers can tell "scan crashed" apart from
"page has no videos" (silence here = missing content / SLA miss).
"""

import logging

import services.poster_content as pc


def _patch_pages(monkeypatch, pages):
    monkeypatch.setattr(pc, "pages_for_poster", lambda poster: pages)
    # _static_url_for sanitizes via project_manager; keep it real, it's pure.


def _patch_scan(monkeypatch, fn):
    import routers.burn as burn

    monkeypatch.setattr(burn, "_scan_project_videos", fn)


def test_scan_failure_is_surfaced_not_swallowed(monkeypatch, caplog):
    pages = [
        {"integration_id": "p1", "name": "Page One", "project": "proj_boom"},
    ]
    _patch_pages(monkeypatch, pages)

    def boom(project):
        raise RuntimeError("scan timed out")

    _patch_scan(monkeypatch, boom)

    with caplog.at_level(logging.ERROR, logger="services.poster_content"):
        result = pc.videos_for_poster({"poster_id": "x"})

    # The page is flagged, the project is listed, and nothing was hidden.
    assert result["scan_errors"] == ["proj_boom"]
    assert result["pages"][0]["scan_error"] is True
    assert result["pages"][0]["video_count"] == 0
    assert result["videos"] == []
    # And the failure was actually logged (with traceback).
    assert any("scan failed" in r.message for r in caplog.records)


def test_successful_scan_has_no_scan_error(monkeypatch):
    pages = [
        {"integration_id": "p1", "name": "Page One", "project": "proj_ok"},
    ]
    _patch_pages(monkeypatch, pages)

    _patch_scan(
        monkeypatch,
        lambda project: [
            {"path": "a.mp4", "name": "a.mp4", "folder": "", "created": 1},
        ],
    )

    result = pc.videos_for_poster({"poster_id": "x"})

    assert result["scan_errors"] == []
    assert result["pages"][0]["scan_error"] is False
    assert result["pages"][0]["video_count"] == 1
    assert len(result["videos"]) == 1


def test_mixed_pages_isolate_the_failure(monkeypatch):
    pages = [
        {"integration_id": "ok", "name": "OK", "project": "proj_ok"},
        {"integration_id": "bad", "name": "Bad", "project": "proj_boom"},
    ]
    _patch_pages(monkeypatch, pages)

    def scan(project):
        if project == "proj_boom":
            raise ValueError("kaboom")
        return [{"path": "a.mp4", "name": "a.mp4", "folder": "", "created": 1}]

    _patch_scan(monkeypatch, scan)

    result = pc.videos_for_poster({"poster_id": "x"})

    summaries = {p["integration_id"]: p for p in result["pages"]}
    assert summaries["ok"]["scan_error"] is False
    assert summaries["ok"]["video_count"] == 1
    assert summaries["bad"]["scan_error"] is True
    assert summaries["bad"]["video_count"] == 0
    assert result["scan_errors"] == ["proj_boom"]
