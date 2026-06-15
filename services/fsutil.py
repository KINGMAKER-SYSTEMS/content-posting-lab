"""Tiny filesystem helpers shared across routers."""

import logging
import os
import shutil

log = logging.getLogger("fsutil")


def safe_rmtree(path) -> bool:
    """Best-effort recursive delete of directory ``path``; never raises.

    Unifies the copy-pasted ``shutil.rmtree(d, ignore_errors=True)`` cleanup
    scattered across the clipper/slideshow/recreate routers (8 call sites, one
    of which wrapped it in a redundant ``try/except Exception: pass``). Unlike
    bare ``ignore_errors=True``, this logs on failure instead of silently
    eating the error, so a stuck/locked staging dir is at least visible.
    Accepts str or os.PathLike. Returns True if the tree was removed.
    """
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("safe_rmtree failed to remove %s: %s", path, e)
        return False


def safe_unlink(path) -> bool:
    """Best-effort delete of ``path``; swallow OSError (missing/locked).

    Replaces the copy-pasted ``try: os.unlink(p) / except OSError: pass``
    cleanup blocks scattered across the video/burn/slideshow/clipper routers.
    Accepts str or os.PathLike. Returns True if a file was removed.
    """
    try:
        os.unlink(path)
        return True
    except OSError:
        return False
