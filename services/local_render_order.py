"""Order local renders so jobs for the same model run back to back.

A ComfyUI server holds only the model the current job needs (see
``providers.comfyui._release_other_models``): switching costs a reload of tens
of gigabytes, and keeping several families resident got the studio Mac's
server killed by the OS for memory. So instead of first-come-first-served, the
next job is the oldest one for the model that is already loaded, and the
server only switches when that model's queue is empty.

To keep a long batch from starving everyone else, any job that has waited
longer than ``LOCAL_BATCH_MAX_WAIT_SECONDS`` (default 30 minutes) goes next
regardless, and its model becomes the loaded one. A reload takes well under a
minute, so at most one forced switch per window costs a few percent of render
time. ``0`` turns batching off (plain oldest-first).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

DEFAULT_MAX_WAIT_SECONDS = 30 * 60


def max_wait_seconds() -> float:
    raw = os.getenv("LOCAL_BATCH_MAX_WAIT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_MAX_WAIT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_MAX_WAIT_SECONDS)
    return max(0.0, value)


def pick(
    pending: Sequence[tuple[str | None, float]],
    loaded_model: str | None,
    now: float,
    max_wait: float,
) -> int | None:
    """Index of the job to run next, or None when nothing is waiting.

    ``pending`` holds ``(model_id, waiting_since)`` per job, in arrival order.
    ``loaded_model`` is the model the server holds now (None when unknown).
    """
    if not pending:
        return None
    oldest = min(range(len(pending)), key=lambda i: pending[i][1])
    if loaded_model is None:
        return oldest
    same = next((i for i, (model, _) in enumerate(pending) if model == loaded_model), None)
    if same is None:
        return oldest
    if now - pending[oldest][1] >= max_wait:
        return oldest
    return same
