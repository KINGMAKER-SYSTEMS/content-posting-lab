"""Tiny filesystem helpers shared across routers."""

import os


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
