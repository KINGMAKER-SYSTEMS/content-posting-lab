"""Output endpoint policy for an ordinary prepared post.

The delivery encode resamples to 30 fps and, with no end bound, lets FFmpeg
round the final frame at EOF. A 531-frame 60000/1001 source (8.858850 s) then
becomes 267 frames, 8.900 s, which the 80 ms source-duration tolerance accepts.
When a slot's sound has a measured interval the picture must end inside, the
slot carries this policy: one exact output frame count on the 30 fps delivery
clock, with the approved interval and the digest of the evidence it came from.
The frame count is exact; the container's stored duration may differ from
frames / 30 s by its own millisecond rounding (``duration_meets_endpoint``).

The policy never cuts or pads. It only selects a frame boundary inside the
drift the ordinary contract already accepts (80 ms of the source duration), the
encoder is capped at that many frames, and the final must decode to exactly
that many. A source that cannot supply them is refused.

Authority: the policy must appear, byte-for-value identical, in the immutable
slot payload the request is hashed against (see ``RenderJobSubmission``). A
request field alone is not accepted, and because the job identity is the slot
id plus the slot payload hash, a bounded request is always a new job; it can
never relabel an artifact that was rendered without the bound.
"""
from __future__ import annotations

import hashlib
import json
import re

OUTPUT_ENDPOINT_SCHEMA = "content-lab.post-render-output-endpoint.v1"
# The delivery preset's clock (services/ffmpeg.py TIKTOK_ENCODE_ARGS "-r 30").
DELIVERY_FRAME_RATE = "30/1"
DELIVERY_FPS = 30
# The ordinary final-versus-source tolerance in services/post_render.py.
SOURCE_DURATION_TOLERANCE_MS = 80
MAX_ENDPOINT_MS = 600_000
# MP4 movie-timescale (1/1000 s) rounding of the stored duration.
CONTAINER_ROUNDING_MS = 1
FIELDS = frozenset({"schema", "frame_rate", "frames", "interval_start_ms", "interval_end_ms",
                    "evidence_sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class OutputEndpointError(ValueError):
    """Stable refusal reason for a malformed or unsatisfiable endpoint policy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _plain_int(value) -> bool:
    return type(value) is int


def validate_output_endpoint(policy) -> dict:
    """Return the policy if it is the closed v1 shape and its endpoint is inside its interval."""
    if not isinstance(policy, dict) or set(policy) != FIELDS:
        raise OutputEndpointError("output_endpoint_invalid", "output endpoint must have exactly the v1 fields")
    if policy["schema"] != OUTPUT_ENDPOINT_SCHEMA or policy["frame_rate"] != DELIVERY_FRAME_RATE:
        raise OutputEndpointError("output_endpoint_invalid", "output endpoint schema or frame rate is unsupported")
    frames, start, end = policy["frames"], policy["interval_start_ms"], policy["interval_end_ms"]
    if not (_plain_int(frames) and _plain_int(start) and _plain_int(end)):
        raise OutputEndpointError("output_endpoint_invalid", "output endpoint numbers must be integers")
    if not (frames >= 1 and 0 <= start <= end <= MAX_ENDPOINT_MS):
        raise OutputEndpointError("output_endpoint_invalid", "output endpoint bounds are out of range")
    evidence = policy["evidence_sha256"]
    if not isinstance(evidence, str) or not _SHA256.fullmatch(evidence):
        raise OutputEndpointError("output_endpoint_invalid", "output endpoint evidence digest is invalid")
    # Exact rational comparison of frames / 30 s against the interval in ms.
    if not start * DELIVERY_FPS <= frames * 1000 <= end * DELIVERY_FPS:
        raise OutputEndpointError("output_endpoint_outside_interval",
                                  "output endpoint is not inside its approved interval")
    return policy


def endpoint_frames(policy: dict) -> int:
    return policy["frames"]


def endpoint_duration_ms(policy: dict) -> int:
    """frames / 30 s rounded to the millisecond."""
    return round(policy["frames"] * 1000 / DELIVERY_FPS)


def duration_meets_endpoint(duration_ms, frames: int) -> bool:
    """Whether a probed container duration is the bound endpoint.

    The exact contract is the decoded frame count. The MP4 container stores
    its duration on a millisecond movie clock, so a 266-frame final is written
    as 8.866016 s or 8.866667 s depending on packet order; both are 266 frames
    ending at 266/30 s. One millisecond is that rounding and nothing more.
    """
    return type(duration_ms) is int and abs(duration_ms - round(frames * 1000 / DELIVERY_FPS)) <= CONTAINER_ROUNDING_MS


def endpoint_within_source(policy: dict, source_duration_ms: int) -> bool:
    """Whether the endpoint stays inside the drift an ordinary encode already accepts."""
    return abs(policy["frames"] * 1000 - source_duration_ms * DELIVERY_FPS) <= (
        SOURCE_DURATION_TOLERANCE_MS * DELIVERY_FPS)


def canonical_endpoint(policy) -> str:
    return json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def endpoint_digest(policy: dict) -> str:
    return hashlib.sha256(canonical_endpoint(policy).encode()).hexdigest()
