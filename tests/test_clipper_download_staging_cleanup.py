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


def test_failed_download_only_removes_its_staging(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "quick-test" / "clips"
    monkeypatch.setattr(clipper, "_get_clipper_dir", lambda project: clipper_dir)
    # A concurrent request/job owns a different staging directory. Cleanup must
    # be scoped to the failed download's UUID, never the shared clips parent.
    sibling_staging = clipper_dir / "_staging_other-job"
    sibling_staging.mkdir(parents=True)
    sibling_payload = sibling_staging / "still-uploading.part"
    sibling_payload.write_bytes(b"other job bytes")

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
    assert len(leftover) == 1 and leftover[0] == sibling_staging, (
        f"a failed download left {len(leftover)} staging director(y/ies) on disk: "
        + ", ".join(
            f"{d.name} holding {sum(f.stat().st_size for f in d.rglob('*') if f.is_file())} bytes"
            for d in leftover
        )
    )
    assert sibling_payload.read_bytes() == b"other job bytes"


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


@pytest.mark.parametrize("handler", ["r2", "batch_job"])
def test_traversal_batch_id_is_rejected_before_staging_cleanup(monkeypatch, tmp_path, handler):
    clipper_dir = tmp_path / "quick-test" / "clips"
    clipper_dir.mkdir(parents=True)
    sentinel = clipper_dir.parent / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("survive")
    monkeypatch.setattr(clipper, "_get_clipper_dir", lambda project: clipper_dir)
    calls = {"r2": 0, "mkdir": 0}
    import services.r2 as r2
    monkeypatch.setattr(r2, "is_configured", lambda: True)
    monkeypatch.setattr(r2, "download_to_path", lambda *args, **kwargs: calls.__setitem__("r2", calls["r2"] + 1))
    original_mkdir = Path.mkdir

    def tracked_mkdir(path, *args, **kwargs):
        calls["mkdir"] += 1
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracked_mkdir)

    if handler == "r2":
        operation = clipper.r2_upload_complete({
            "project": "quick-test", "batch_id": "x/../..",
            "items": [{"index": 0, "filename": "bad.mp4", "key": "key"}],
        })
    else:
        operation = clipper.process_batch({
            "project": "quick-test", "batch_id": "x/../..",
            "sources": [{"path": str(tmp_path / "missing"), "trim_start": 0, "trim_end": 0}],
        })

    with pytest.raises(HTTPException) as caught:
        asyncio.run(operation)
    assert caught.value.status_code == 400
    if handler == "r2":
        assert caught.value.detail == "batch_id must be 12 lowercase hexadecimal characters"
        assert calls == {"r2": 0, "mkdir": 0}, "invalid batch id reached storage or filesystem work"
    assert (sentinel / "keep.txt").read_text() == "survive"


@pytest.mark.parametrize("batch_id", ["x/../..", "../sentinel", ".."])
def test_staging_cleanup_rejects_traversal_ids_and_preserves_outside_sentinel(
    monkeypatch, tmp_path, batch_id
):
    clipper_dir = tmp_path / "page" / "clips"
    clipper_dir.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    payload = sentinel / "keep.txt"
    payload.write_text("survive")
    rmtree_calls = []
    monkeypatch.setattr(clipper, "safe_rmtree", lambda path: rmtree_calls.append(path) or True)
    assert clipper._delete_staging_dir(clipper_dir, batch_id) is False
    assert rmtree_calls == [], "invalid ID reached recursive deletion"
    assert payload.read_text() == "survive"


def test_staging_cleanup_refuses_symlink_without_touching_link_or_target(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "page" / "clips"
    clipper_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    payload = target / "keep.txt"
    payload.write_text("survive")
    link = clipper_dir / "_staging_012345abcdef"
    link.symlink_to(target, target_is_directory=True)
    rmtree_calls = []
    monkeypatch.setattr(clipper, "safe_rmtree", lambda path: rmtree_calls.append(path) or False)
    assert clipper._delete_staging_dir(clipper_dir, "012345abcdef") is False
    assert rmtree_calls == [], "symlink reached recursive deletion"
    assert link.is_symlink() and link.resolve() == target
    assert payload.read_text() == "survive"


def test_staging_cleanup_removes_only_valid_real_staging_dir(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "page" / "clips"
    clipper_dir.mkdir(parents=True)
    own = clipper_dir / "_staging_fedcba654321"
    own.mkdir()
    (own / "partial.part").write_bytes(b"partial")
    sibling = clipper_dir / "_staging_000000000000"
    sibling.mkdir()
    (sibling / "keep.part").write_bytes(b"sibling")
    rmtree_calls = []
    real_safe_rmtree = clipper.safe_rmtree

    def check_resolved_target(path):
        rmtree_calls.append(Path(path))
        assert Path(path) == own.resolve(), "guard must pass its verified resolved child"
        return real_safe_rmtree(path)

    monkeypatch.setattr(clipper, "safe_rmtree", check_resolved_target)
    assert clipper._delete_staging_dir(clipper_dir, "fedcba654321") is True
    assert rmtree_calls == [own.resolve()]
    assert not own.exists()
    assert (sibling / "keep.part").read_bytes() == b"sibling"
