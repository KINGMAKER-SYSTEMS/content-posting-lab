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

# The retry pass budgets against 85% of the delivery byte cap (see
# ``services/post_render.py:MAX_FINAL_BYTES``) to leave headroom after a CRF
# attempt exceeds it. Single-pass x264 at a fixed -b:v can land about 11% over
# its own target on short, busy clips. Glossy body 3dd39340 (10 s): a 90% retry
# came out over the cap (23,091,288 bytes on ffmpeg 5.1.9, 23,072,843 on 9.0.2),
# and an 85% retry fit (21,802,228 and 21,810,620 bytes). So the retry needs more
# than 10% headroom. Both the prepared-post
# and timed lyric renderers use this one fraction. It is a delivery-QA policy,
# not a page value.
RETRY_BUDGET_FRACTION = 0.85
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
