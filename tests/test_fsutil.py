"""Tests for services.fsutil.safe_unlink — the shared temp-cleanup helper."""

import os
from pathlib import Path

from services.fsutil import safe_rmtree, safe_unlink


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


# --- safe_rmtree --------------------------------------------------------------


def test_rmtree_removes_existing_tree(tmp_path):
    d = tmp_path / "stage"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("hi")
    assert d.exists()
    assert safe_rmtree(d) is True
    assert not d.exists()


def test_rmtree_accepts_str_path(tmp_path):
    d = tmp_path / "stage"
    d.mkdir()
    assert safe_rmtree(str(d)) is True
    assert not d.exists()


def test_rmtree_missing_dir_is_swallowed(tmp_path):
    missing = tmp_path / "never-existed"
    # Must not raise — this replaces shutil.rmtree(..., ignore_errors=True).
    assert safe_rmtree(missing) is False


def test_rmtree_on_file_is_swallowed_and_logged(tmp_path, caplog):
    # rmtree on a plain file raises NotADirectoryError (an OSError) — the helper
    # swallows it, reports failure, and logs a warning instead of exploding a
    # finally block.
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x")
    with caplog.at_level("WARNING", logger="fsutil"):
        assert safe_rmtree(f) is False
    assert f.exists()
    assert any("safe_rmtree failed" in r.message for r in caplog.records)


def test_rmtree_double_call_is_idempotent(tmp_path):
    d = tmp_path / "stage"
    d.mkdir()
    assert safe_rmtree(d) is True
    # Second call: dir already gone, no exception, returns False.
    assert safe_rmtree(d) is False


def test_abn_factory_uses_safe_rmtree_in_real_demo():
    """Regression: abn_factory._real_demo's finally block must scrub its temp
    workdir via the shared helper, not the bare
    ``shutil.rmtree(workdir, ignore_errors=True)`` wrapped in a redundant
    ``try/except Exception: pass`` — the exact double-swallowing pattern
    safe_rmtree was created to unify. safe_rmtree logs the OSError instead of
    silently eating it, so orphaned temp workdirs become visible."""
    import inspect

    import services.abn_factory as af

    src = inspect.getsource(af._real_demo)
    # The bare ignore_errors rmtree (and its redundant wrapper) is gone...
    assert "ignore_errors=True" not in src
    # ...replaced by the shared logging helper.
    assert "safe_rmtree(workdir)" in src


def test_clipper_uses_safe_rmtree():
    """Regression: every staging/job cleanup in routers.clipper must go through
    safe_rmtree, not bare shutil.rmtree (including the one site that wrapped it
    in a redundant try/except Exception: pass)."""
    import routers.clipper as clipper

    src = Path(clipper.__file__).read_text()
    assert "shutil.rmtree" not in src
    assert src.count("safe_rmtree(") == 4


def test_slideshow_uses_safe_rmtree():
    """Regression: the three tmp_dir cleanups in routers.slideshow must use the
    shared helper. shutil stays imported only for shutil.move."""
    import routers.slideshow as slideshow

    src = Path(slideshow.__file__).read_text()
    assert "shutil.rmtree" not in src
    assert src.count("safe_rmtree(") == 3


def test_recreate_uses_safe_rmtree():
    """Regression: the delete-job cleanup in routers.recreate must use the
    shared helper."""
    import routers.recreate as recreate

    src = Path(recreate.__file__).read_text()
    assert "shutil.rmtree" not in src
    assert src.count("safe_rmtree(") == 1
