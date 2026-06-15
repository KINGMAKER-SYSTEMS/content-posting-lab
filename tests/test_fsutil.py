"""Tests for services.fsutil.safe_unlink — the shared temp-cleanup helper."""

import os
from pathlib import Path

from services.fsutil import safe_unlink


def test_removes_existing_file(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    assert f.exists()
    assert safe_unlink(f) is True
    assert not f.exists()


def test_accepts_str_path(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    assert safe_unlink(str(f)) is True
    assert not f.exists()


def test_missing_file_is_swallowed(tmp_path):
    missing = tmp_path / "never-existed.txt"
    # Must not raise (this is the whole point of the helper).
    assert safe_unlink(missing) is False


def test_directory_is_swallowed(tmp_path):
    # os.unlink on a dir raises OSError (IsADirectory/PermissionError) —
    # safe_unlink swallows it and reports failure rather than blowing up
    # a finally block.
    d = tmp_path / "adir"
    d.mkdir()
    assert safe_unlink(d) is False
    assert d.exists()


def test_double_unlink_is_idempotent(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    assert safe_unlink(f) is True
    # Second call: file already gone, no exception, returns False.
    assert safe_unlink(f) is False


def test_abn_factory_uses_safe_unlink_in_screenshot():
    """Regression: abn_factory._screenshot must use the shared helper, not the
    bare ``try: out.unlink() / except Exception: pass`` cleanup blocks that
    safe_unlink was created to replace (the two bot-wall/near-blank discards)."""
    import inspect

    import services.abn_factory as af

    src = inspect.getsource(af._screenshot)
    # The bare pattern is gone...
    assert ".unlink()" not in src
    assert "except Exception: pass" not in src
    # ...replaced by the shared helper on the rejected screenshot.
    assert src.count("safe_unlink(out)") == 2


def test_video_download_handlers_use_safe_unlink():
    """Regression: the zip-download handlers in routers.video must clean up
    their temp .zip via safe_unlink, not a bare os.unlink that crashes the
    request if the temp file is already gone/locked in concurrent download
    paths. Covers download_all (1 cleanup) and bulk_download (2 cleanups)."""
    import inspect

    import routers.video as video

    for fn in (video.download_all, video.bulk_download):
        src = inspect.getsource(fn)
        # No bare os.unlink survives in the cleanup blocks...
        assert "os.unlink(" not in src, f"{fn.__name__} still uses bare os.unlink"
        # ...every temp cleanup goes through the swallowing helper.
        assert "safe_unlink(tmp_path)" in src


def test_burn_download_zip_uses_safe_unlink():
    """Regression: the ZIP-build exception handler in burn.download_burn_zip
    must clean up its tempfile via the shared helper, not a bare os.unlink
    that explodes if the temp file is already gone. The streaming finally
    block already uses safe_unlink — the except branch must match it."""
    import inspect

    import routers.burn as burn

    src = inspect.getsource(burn.download_burn_zip)
    # The bare unlink on the tmp ZIP is gone...
    assert "os.unlink(tmp_path)" not in src
    # ...and both cleanup paths (except + finally) go through the helper.
    assert src.count("safe_unlink(tmp_path)") == 2
