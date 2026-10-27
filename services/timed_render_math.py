"""Pure, dependency-free duration math for the timed lyric renderer.

The timed renderer derives three delivery quantities from the timing master's
``durationMs`` (data), never from a compiled-in page constant:

* the per-phrase PNG ``-t`` input duration,
* the second-pass bitrate budget that keeps the encode inside the byte cap,
* the last decoded ``frame=`` line a full 30fps decode must reach.

These derivations live here (no PIL/pydantic/ffmpeg) so the reduced review host
can lock the duration-becomes-data change without the image stack. The renderer
imports and applies them; an integration test in
``tests/test_post_render_timed.py`` guards that wiring end-to-end.
"""
from __future__ import annotations

# The retry pass budgets against 90% of the delivery byte cap (see
# ``services/post_render.py:MAX_FINAL_BYTES``) to leave headroom for container
# overhead after a CRF attempt exceeds it. This fraction is a delivery-QA
# policy, not a page value.
RETRY_BUDGET_FRACTION = 0.90
TIMED_FPS = 30


def phrase_input_seconds(duration_ms: int) -> float:
    """Seconds each looping phrase PNG ``-t`` input is capped to."""
    return duration_ms / 1000


def retry_bitrate_bps(max_final_bytes: int, duration_ms: int) -> int:
    """Average video bitrate (b/s) for the retry pass, fitted to ``max_final_bytes``."""
    return int(max_final_bytes * 8 * RETRY_BUDGET_FRACTION * 1000 / duration_ms)


def expected_decoded_frames(duration_ms: int) -> int:
    """Last ``frame=`` line a full 30fps decode must reach for this duration."""
    return duration_ms * TIMED_FPS // 1000
