"""Verify upload_images cleans up temp files via safe_unlink, not bare unlink.

Regression guard for the slideshow router: raw images are removed on
successful crop, and both raw + partial cropped files are removed when
crop_to_916 raises (mirrors the clipper/agenticnews safe_unlink precedent).
"""

import pytest


class _FakeUpload:
    def __init__(self, filename: str, content: bytes = b"data"):
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


@pytest.mark.asyncio
async def test_upload_removes_raw_on_success(tmp_path, monkeypatch):
    from routers import slideshow

    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: tmp_path
    )

    def fake_crop(raw, cropped):
        cropped.write_bytes(b"cropped")

    monkeypatch.setattr(slideshow, "crop_to_916", fake_crop)

    result = await slideshow.upload_images(
        project="p", files=[_FakeUpload("a.jpg")]
    )

    assert result["saved"] == 1
    # raw_* temp file removed, only the cropped output remains
    assert not list(tmp_path.glob("raw_*"))
    assert len(list(tmp_path.glob("*.jpg"))) == 1


@pytest.mark.asyncio
async def test_upload_cleans_up_on_crop_failure(tmp_path, monkeypatch):
    from routers import slideshow

    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: tmp_path
    )

    def boom_crop(raw, cropped):
        cropped.write_bytes(b"partial")
        raise RuntimeError("crop failed")

    monkeypatch.setattr(slideshow, "crop_to_916", boom_crop)

    result = await slideshow.upload_images(
        project="p", files=[_FakeUpload("a.jpg")]
    )

    assert result["saved"] == 0
    assert result["results"][0]["error"] == "crop failed"
    # both raw and partial cropped image files cleaned up
    assert not list(tmp_path.glob("raw_*"))
    assert not list(tmp_path.glob("*.jpg"))


@pytest.mark.asyncio
async def test_upload_missing_raw_does_not_raise(tmp_path, monkeypatch):
    """safe_unlink swallows a missing file (bare unlink(missing_ok) parity)."""
    from routers import slideshow

    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: tmp_path
    )

    def crop_and_delete_raw(raw, cropped):
        cropped.write_bytes(b"cropped")
        raw.unlink()  # crop already consumed the raw file

    monkeypatch.setattr(slideshow, "crop_to_916", crop_and_delete_raw)

    result = await slideshow.upload_images(
        project="p", files=[_FakeUpload("a.jpg")]
    )

    assert result["saved"] == 1
