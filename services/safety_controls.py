"""Bounded read-only view of selected effective safety controls.

No provider or service initialization, storage, or raw configuration is exposed.
"""
from __future__ import annotations

import math
import os
import sys

from services import moderation_retry, post_render, post_render_jobs, control_plane_source_imports

_MAX_JSON_INTEGER = 2**53 - 1


def _number(raw, default, *, kind=int, minimum=1, maximum=None, invalid_value=None, positive=False):
    fallback = default if invalid_value is None else invalid_value
    if raw is None or not raw.strip():
        return {"state": "default", "value": default}
    try:
        value = kind(raw.strip())
    except (ValueError, OverflowError):
        return {"state": "invalid", "value": fallback}
    if kind is float and not math.isfinite(value):
        # NaN falls back in the consumer; +inf is accepted but cannot be a JSON number.
        return {"state": "invalid", "value": None if value > 0 else default}
    if (value <= 0 if positive else value < minimum) or (maximum is not None and value > maximum):
        return {"state": "invalid", "value": fallback}
    if value > _MAX_JSON_INTEGER:
        return {"state": "unknown", "value": None}
    return {"state": "configured", "value": value}


def snapshot(started_service):
    """Snapshot this process, without constructing a manager or touching storage."""
    worker_config = _number(os.getenv("CONTENT_LAB_POST_RENDER_WORKERS"),
                            post_render_jobs.DEFAULT_WORKER_COUNT,
                            minimum=post_render_jobs.MIN_WORKER_COUNT,
                            maximum=post_render_jobs.MAX_WORKER_COUNT)
    active_workers = None if started_service is None else getattr(started_service, "worker_count", None)
    if type(active_workers) is not int or not post_render_jobs.MIN_WORKER_COUNT <= active_workers <= post_render_jobs.MAX_WORKER_COUNT:
        active_workers = None
    min_free = _number(os.getenv("CONTENT_LAB_POST_RENDER_MIN_FREE_BYTES"),
                       post_render_jobs.DEFAULT_MIN_FREE_BYTES)
    moderation = _number(os.getenv(moderation_retry.DAILY_BUDGET_ENV),
                         moderation_retry.DEFAULT_DAILY_BUDGET, minimum=0, invalid_value=0)
    floor = None
    if active_workers is not None and min_free["value"] is not None:
        floor = min_free["value"] + post_render_jobs.PEAK_RENDER_WORKSPACE_BYTES * active_workers
        if floor > _MAX_JSON_INTEGER:
            floor = None
    # Importing providers.base has unrelated filesystem and dotenv side effects.
    # Only an already-loaded module can supply its actual process constants.
    provider = sys.modules.get("providers.base")
    if provider is None:
        download_bytes = download_seconds = {"state": "unknown", "value": None}
    else:
        download_bytes = _number(os.getenv("CONTENT_LAB_PROVIDER_DOWNLOAD_BYTES"),
                                 provider._DEFAULT_PROVIDER_DOWNLOAD_BYTES)
        download_seconds = _number(os.getenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS"),
                                   provider._DEFAULT_PROVIDER_DOWNLOAD_SECONDS, kind=float, positive=True)
        # This pure consumer parser is the authority for the effective value;
        # a successor may reject nonfinite settings that older versions accepted.
        effective = provider._provider_download_deadline()
        download_seconds["value"] = (effective if math.isfinite(effective)
                                     and abs(effective) <= _MAX_JSON_INTEGER else None)
    return {
        "schema": "content-lab.safety-controls.v1",
        "scope": "this_process_configuration_observation",
        "render": {
            "configured_workers": worker_config,
            "started_worker_count": active_workers,
            "minimum_free_bytes": min_free,
            "claim_floor_bytes": floor,
            "max_attempts": post_render_jobs.MAX_ATTEMPTS,
            "source_bytes": post_render.MAX_SOURCE_BYTES,
            "final_bytes": post_render.MAX_FINAL_BYTES,
            "qa_bytes": post_render.MAX_QA_BYTES,
            "overlay_bytes": post_render.MAX_OVERLAY_BYTES,
        },
        "moderation_retry_daily_per_page": moderation,
        "source_import": {
            "source_bytes": control_plane_source_imports.MAX_SOURCE_IMPORT_BYTES,
            "workspace_bytes": control_plane_source_imports.MAX_SOURCE_IMPORT_WORKSPACE_BYTES,
            "normalized_bytes": control_plane_source_imports.MAX_NORMALIZED_SOURCE_BYTES,
            "concurrent": control_plane_source_imports.MAX_CONCURRENT_SOURCE_IMPORTS,
        },
        "provider_download": {"bytes": download_bytes, "seconds": download_seconds},
    }
