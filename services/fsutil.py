"""Tiny filesystem helpers shared across routers."""

import os
import shutil


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


def safe_rmtree(path) -> bool:
    """Best-effort recursive delete of ``path``; never raises.

    Sibling to ``safe_unlink`` for directory trees (e.g. _real_demo's throwaway
    clone workdir). Swallows a missing path and any per-entry OSError so a finally
    block can clean up without re-raising and masking the original control flow.
    Accepts str or os.PathLike. Returns True if the tree existed and was removed.
    """
    if not os.path.exists(path):
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
        return True
    except OSError:
        return False
