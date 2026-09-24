"""A clipper download that fails must not leave its staging behind.

`download_url` fetches a source video into `<project>/clips/_staging_<id>/`.
When the fetch fails, yt-dlp has usually already written large intermediates --
`.part` files, the separately-fetched video and audio streams, a `.temp` remux
-- and nothing ever collects them: no job record exists for a download that
never produced one, so neither `/api/clipper/jobs` nor the project delete can
see them.

MEASURED on production 2026-09-23: two abandoned attempts from a four-minute
window held 20.7 GB between them, on a volume that was 98.6% full. Every source
import after that failed with "source import storage capacity is unavailable",
so one video the clipper could not fetch stopped content for every page on the
fleet.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from routers import clipper


def _staging_dirs(clipper_dir: Path) -> list[Path]:
    return sorted(p for p in clipper_dir.glob("_staging_*") if p.is_dir())


def test_failed_download_removes_its_staging(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "quick-test" / "clips"
    monkeypatch.setattr(clipper, "_get_clipper_dir", lambda project: clipper_dir)

    async def _fake_download(video_url, dest):
        # What yt-dlp actually leaves when a merge dies part-way: the separate
        # streams and the remux temp, but never the merged `src_000.mp4`.
        dest.parent.mkdir(parents=True, exist_ok=True)
        (dest.parent / "src_000.f401.mp4").write_bytes(b"x" * 4096)
        (dest.parent / "src_000.f140.m4a").write_bytes(b"x" * 512)
        (dest.parent / "src_000.temp.mp4").write_bytes(b"x" * 2048)
        raise RuntimeError("ffmpeg exited with status 1")

    import scraper.frame_extractor as frame_extractor
    monkeypatch.setattr(frame_extractor, "download_video", _fake_download)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(clipper.download_url({"video_url": "https://example.test/v", "project": "quick-test"}))

    assert caught.value.status_code == 500
    assert "Download failed" in str(caught.value.detail)
    leftover = _staging_dirs(clipper_dir)
    assert leftover == [], (
        f"a failed download left {len(leftover)} staging director(y/ies) on disk: "
        + ", ".join(
            f"{d.name} holding {sum(f.stat().st_size for f in d.rglob('*') if f.is_file())} bytes"
            for d in leftover
        )
    )


def test_successful_download_keeps_its_staging(monkeypatch, tmp_path):
    """POSITIVE CONTROL: the cleanup must be scoped to the failure path only.

    Without this, deleting the staging unconditionally would also pass the test
    above while destroying every successful download -- the staged file is what
    the caller trims next, and its URL is what this handler returns.
    """
    clipper_dir = tmp_path / "quick-test" / "clips"
    monkeypatch.setattr(clipper, "_get_clipper_dir", lambda project: clipper_dir)

    async def _fake_download(video_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 8192)

    async def _fake_info(path):
        return {"duration": 12, "width": 1080, "height": 1920}

    async def _fake_thumb(video_path, thumb_path, seek=-1):
        thumb_path.write_bytes(b"jpg")
        return True

    import scraper.frame_extractor as frame_extractor
    monkeypatch.setattr(frame_extractor, "download_video", _fake_download)
    monkeypatch.setattr(clipper, "_get_video_info", _fake_info)
    monkeypatch.setattr(clipper, "_generate_thumbnail", _fake_thumb)

    result = asyncio.run(
        clipper.download_url({"video_url": "https://example.test/v", "project": "quick-test"})
    )

    assert len(_staging_dirs(clipper_dir)) == 1, "a successful download lost its staged file"
    assert Path(result["files"][0]["path"]).exists()
