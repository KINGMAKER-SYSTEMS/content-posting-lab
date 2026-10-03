"""Shared validation for page-scoped sourced-video controls."""

from __future__ import annotations

from typing import Any


MAX_SOURCE_START_MS = 2 * 60 * 60 * 1_000
SOURCE_START_STEP_MS = 1_000


def source_start_ms(value: Any) -> int | None:
    """Return a bounded whole-second source floor, or ``None`` if invalid."""
    if (
        type(value) is not int
        or not 0 <= value <= MAX_SOURCE_START_MS
        or value % SOURCE_START_STEP_MS != 0
    ):
        return None
    return value
