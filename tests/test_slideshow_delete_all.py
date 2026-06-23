"""Verify delete_all_images survives a locked/raced file mid-loop.

Regression guard: the loop used a bare ``p.unlink()`` which raises on the
first locked/raced file and silently aborts the rest of the deletion,
returning an incomplete count. It now uses ``safe_unlink`` so the loop
continues past failures, logs each one, and counts only real deletions.
"""

import pytest


@pytest.mark.asyncio
async def test_delete_all_removes_valid_images(tmp_path, monkeypatch):
    from routers import slideshow

    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: tmp_path
    )
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")  # non-image, ignored

    result = await slideshow.delete_all_images(project="p")

    assert result["deleted"] == 2
    assert not list(tmp_path.glob("*.jpg"))
    assert not list(tmp_path.glob("*.png"))
    assert (tmp_path / "notes.txt").exists()


@pytest.mark.asyncio
async def test_delete_all_continues_past_locked_file(tmp_path, monkeypatch):
    """A failure on one file must not abort deletion of the rest."""
    from routers import slideshow

    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: tmp_path
    )
    (tmp_path / "a.jpg").write_bytes(b"x")
    locked = tmp_path / "b.jpg"
    locked.write_bytes(b"x")
    (tmp_path / "c.jpg").write_bytes(b"x")

    real_unlink = slideshow.safe_unlink

    def flaky_unlink(p):
        if str(p) == str(locked):
            return False  # safe_unlink swallowed an OSError (locked file)
        return real_unlink(p)

    monkeypatch.setattr(slideshow, "safe_unlink", flaky_unlink)

    result = await slideshow.delete_all_images(project="p")

    # only the two deletable files are counted; loop did not abort on b.jpg
    assert result["deleted"] == 2
    assert not (tmp_path / "a.jpg").exists()
    assert not (tmp_path / "c.jpg").exists()
    assert locked.exists()


@pytest.mark.asyncio
async def test_delete_all_missing_dir(tmp_path, monkeypatch):
    from routers import slideshow

    missing = tmp_path / "nope"
    monkeypatch.setattr(
        slideshow, "get_project_slideshow_images_dir", lambda p: missing
    )

    result = await slideshow.delete_all_images(project="p")
    assert result == {"deleted": 0}
