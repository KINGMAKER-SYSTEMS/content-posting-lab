"""Source-duration repair: the disjoint Lab-side consumer + encoder helper.

This is the ONLY Lab module the adapter seat owns. It consumes the
Worker-minted repair command (rt.source-duration-repair.v1) and makes a real
shorter final: it trims the already-treated source to the attested boundary and
re-encodes it through the REAL prepared-post delivery path.

Page look must never drift from the prepared-post encoder, so the delivery
encode is NOT vendored here: this module imports and calls the real
``delivery_encode_args`` (services/ffmpeg.py), ``letterbox_filter``
(services/page_frame.py) and ``_encode_final``/``_probe``/``MediaProbe``/
``RenderTools`` (services/post_render.py). The near-9:16 scale/crop, the
``setsar=1`` step, the ``normalize_pair_timing`` fps=30 step, the letterbox,
the caption overlay and the two-attempt bitrate-budget fallback are therefore
all the single-source-of-truth code the ordinary prepared-post renderer uses.

The canonical command JSON and its SHA-256 digest are byte-identical to
src/scheduler/sourceDurationRepair.js on the Worker side (see the vector test in
tests/test_source_duration_repair.py).

The incumbent Lab owner wires this into services/post_render.py by:
  1. adding `source_duration_repair` to PostRenderRequest (a validated dict
     field), and
  2. after `_copy_verified_source` + `_probe`, calling
     `render_shorter(...)` instead of the stock final encode when the command
     is present (absent = the legacy path, byte-identical).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The REAL prepared-post functions. These are the single source of truth for the
# delivery preset and the page look; they are imported (never copied) so the
# shorter final cannot drift from the ordinary prepared-post encoder.
from services.ffmpeg import delivery_encode_args
from services.page_frame import CANVAS_HEIGHT, CANVAS_WIDTH, letterbox_filter
from services.post_render import (
    MAX_DURATION_MS,
    MAX_FINAL_BYTES,
    MAX_OVERLAY_BYTES,
    MAX_SOURCE_BYTES,
    MediaProbe,
    PostRenderError,
    RenderTools,
    _encode_final,
    _probe as post_render_probe,
)

SOURCE_DURATION_REPAIR_SCHEMA = "rt.source-duration-repair.v1"
SOURCE_DURATION_REPAIR_DELIVERY_FPS = 30
RECEIPT_SCHEMA = "source-duration-repair-receipt.v1"

# Mirrors SOURCE_DURATION_REPAIR_KEYS on the Worker side, lexicographic order.
# The three *_evidence_end_ms keys carry the MEASURED evidence value so the Lab
# can re-verify that the commanded cut equals the evidence, not just that the
# evidence token is present.
SOURCE_DURATION_REPAIR_KEYS: Tuple[str, ...] = (
    "account", "approved_music_id", "compressed_reference_sha256",
    "cue_evidence_end_ms", "cue_evidence_id", "cue_evidence_sha256",
    "decoded_reference_sha256", "device_serial", "expected_output_duration_ms",
    "expected_output_frames", "font_id", "font_sha256", "format_id",
    "measured_quantization_ms", "native_official_start_ms", "official_end_ms",
    "official_start_ms", "opening_evidence_end_ms", "opening_evidence_id",
    "opening_evidence_sha256", "owner_id", "page_id", "program_id", "recipe_id",
    "recipe_version", "renderer_id", "renderer_version",
    "retimed_lyric_cue_sha256", "sample_clock_hz", "schema", "slot_id",
    "slot_payload_sha256", "source_sha256", "source_trim_end_ms",
    "source_trim_start_ms", "tail_evidence_end_ms", "tail_evidence_id",
    "tail_evidence_sha256",
)
NULLABLE_KEYS = frozenset({
    "opening_evidence_id", "opening_evidence_sha256", "opening_evidence_end_ms",
    "tail_evidence_id", "tail_evidence_sha256", "tail_evidence_end_ms",
    "cue_evidence_id", "cue_evidence_sha256", "cue_evidence_end_ms",
    "retimed_lyric_cue_sha256",
})
SHA_KEYS = frozenset({
    "slot_payload_sha256", "source_sha256", "compressed_reference_sha256",
    "decoded_reference_sha256", "font_sha256",
    "opening_evidence_sha256", "tail_evidence_sha256", "cue_evidence_sha256",
})
IDENTITY_KEYS = frozenset({
    "owner_id", "slot_id", "page_id", "program_id", "device_serial", "account",
})
EVIDENCE_ID_KEYS = frozenset({"opening_evidence_id", "tail_evidence_id", "cue_evidence_id"})
EVIDENCE_END_KEYS = frozenset({"opening_evidence_end_ms", "tail_evidence_end_ms", "cue_evidence_end_ms"})
TOKEN_KEYS = frozenset({
    "recipe_id", "recipe_version", "format_id", "font_id",
    "renderer_id", "renderer_version",
})
NONNULL_SHA_KEYS = frozenset({"slot_payload_sha256", "source_sha256",
                              "compressed_reference_sha256", "decoded_reference_sha256", "font_sha256"})
INT_KEYS = frozenset({
    "source_trim_start_ms", "source_trim_end_ms", "official_start_ms",
    "official_end_ms", "native_official_start_ms", "measured_quantization_ms",
    "expected_output_frames", "expected_output_duration_ms", "sample_clock_hz",
})

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


class RepairError(ValueError):
    """Stable failure reason; the caller decides whether a job may retry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_identity(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 512
            and value == value.strip() and not any(ord(c) < 32 for c in value))


def _is_token(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 200:
        return False
    return all(c.isascii() and (c.isalnum() or c in "._:-") for c in value)


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_nullable_int(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(command: Dict) -> str:
    """Compact, key-sorted JSON identical to the Worker's canonical form."""
    ordered = {key: command[key] for key in SOURCE_DURATION_REPAIR_KEYS}
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def command_digest(command: Dict) -> str:
    return sha256(canonical_json(command).encode("utf-8"))


def validate_command(value: object) -> Dict:
    """Strict shape/type validation; returns a normalized dict or raises.

    Unknown fields, missing fields and out-of-contract values are refused, never
    dropped — the same rule as the Worker-side normalizer. The evidence VALUE is
    re-verified here too: a bound evidence must carry its measured end and the
    commanded cut must equal that end (a made-up token/sha pair or an arbitrary
    cut point is refused by the Lab, not only by the Worker).
    """
    if not isinstance(value, dict):
        raise RepairError("repair_command_invalid", "repair command must be an object")
    keys = list(value.keys())
    if len(keys) != len(SOURCE_DURATION_REPAIR_KEYS) or any(k not in SOURCE_DURATION_REPAIR_KEYS for k in keys):
        raise RepairError("repair_command_invalid", "repair command carries an unknown or missing field")
    if value.get("schema") != SOURCE_DURATION_REPAIR_SCHEMA:
        raise RepairError("repair_command_invalid", "unsupported repair schema")
    for key in sorted(IDENTITY_KEYS):
        if not _is_identity(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} is not a valid identity")
    for key in sorted(TOKEN_KEYS):
        if not _is_token(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} is not a valid token")
    for key in sorted(EVIDENCE_ID_KEYS):
        if value.get(key) is not None and not _is_token(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} is not a valid token")
    for key in sorted(SHA_KEYS):
        if value.get(key) is None:
            continue
        if not _is_sha(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} is not a lowercase sha256")
    for key in sorted(NONNULL_SHA_KEYS):
        if not _is_sha(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} is not a lowercase sha256")
    for key in sorted(EVIDENCE_END_KEYS):
        if not _is_nullable_int(value.get(key)):
            raise RepairError("repair_command_invalid", f"repair {key} must be null or a nonnegative integer")
    music = value.get("approved_music_id")
    if not isinstance(music, str) or not (6 <= len(music) <= 20) or not music.isascii() or not music.isdigit():
        raise RepairError("repair_command_invalid", "approved_music_id must be 6-20 ASCII digits")
    for key in sorted(INT_KEYS):
        v = value.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise RepairError("repair_command_invalid", f"repair {key} must be a nonnegative integer")
    if not (8000 <= value["sample_clock_hz"] <= 384000):
        raise RepairError("repair_command_invalid", "sample_clock_hz is out of range")
    if value["source_trim_end_ms"] <= value["source_trim_start_ms"]:
        raise RepairError("repair_command_invalid", "source trim end must exceed start")
    if value["official_end_ms"] <= value["official_start_ms"]:
        raise RepairError("repair_command_invalid", "official interval end must exceed start")
    if value["expected_output_duration_ms"] < 1 or value["expected_output_frames"] < 1:
        raise RepairError("repair_command_invalid", "expected output must be positive")
    if value["expected_output_duration_ms"] != value["source_trim_end_ms"] - value["source_trim_start_ms"]:
        raise RepairError("repair_command_invalid", "expected duration must equal the trimmed span")
    for base in ("opening_evidence", "tail_evidence", "cue_evidence"):
        vid, vsha, vend = value.get(base + "_id"), value.get(base + "_sha256"), value.get(base + "_end_ms")
        if (vid is None) != (vsha is None):
            raise RepairError("repair_command_invalid", f"repair {base} id and sha256 must be both set or both null")
        if vsha is not None:
            # A bound evidence sha must be the decoded reference of the approved
            # sound — a made-up token/sha pair can never authorize a cut.
            if vsha != value["decoded_reference_sha256"]:
                raise RepairError("repair_evidence_mismatch",
                                  f"repair {base}_sha256 must match the decoded reference")
            # Bound evidence must carry its measured value.
            if not isinstance(vend, int) or isinstance(vend, bool) or vend < 0:
                raise RepairError("repair_evidence_mismatch",
                                  f"repair {base} is bound but has no measured {base}_end_ms")
        elif vend is not None:
            raise RepairError("repair_command_invalid", f"repair {base}_end_ms must be null when {base} is absent")
    # The cut must EQUAL the evidence measured value (re-verified by the Lab, not
    # only by the Worker). A start trim binds the opening end (first audible
    # sample); an end trim binds the tail/cue end (the clean phrase end).
    if value["tail_evidence_sha256"] is not None and value["source_trim_end_ms"] != value["tail_evidence_end_ms"]:
        raise RepairError("repair_evidence_mismatch", "source_trim_end_ms must equal the measured tail_evidence_end_ms")
    if value["cue_evidence_sha256"] is not None and value["source_trim_end_ms"] != value["cue_evidence_end_ms"]:
        raise RepairError("repair_evidence_mismatch", "source_trim_end_ms must equal the measured cue_evidence_end_ms")
    if value["opening_evidence_sha256"] is not None and value["source_trim_start_ms"] != value["opening_evidence_end_ms"]:
        raise RepairError("repair_evidence_mismatch", "source_trim_start_ms must equal the measured opening_evidence_end_ms")
    if value["source_trim_start_ms"] > 0 and value["opening_evidence_sha256"] is None:
        raise RepairError("repair_evidence_unbound", "a start trim requires bound opening evidence")
    rlc = value.get("retimed_lyric_cue_sha256")
    if rlc is not None and not _is_sha(rlc):
        raise RepairError("repair_command_invalid", "retimed_lyric_cue_sha256 must be null or sha256")
    return {key: value[key] for key in SOURCE_DURATION_REPAIR_KEYS}


def _run(args: List[str], timeout_s: float = 120.0, output_limit: int = 1024 * 1024,
         output_file: Optional[Path] = None, max_bytes: Optional[int] = None) -> bytes:
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout_s, start_new_session=True)
    except subprocess.TimeoutExpired as exc:
        raise RepairError("process_timeout", "ffmpeg/ffprobe exceeded its deadline") from exc
    if output_file is not None and max_bytes is not None:
        if output_file.exists() and output_file.stat().st_size > max_bytes:
            raise RepairError("artifact_too_large", "render output exceeded its byte limit")
    if sum((len(proc.stdout), len(proc.stderr))) > output_limit:
        raise RepairError("process_output_limit", "subprocess exceeded its output limit")
    if proc.returncode:
        tail = proc.stderr[-1000:].decode(errors="replace")
        raise RepairError("process_failed", "subprocess failed: " + tail)
    return proc.stdout


def _parse_rate(value: object):
    """Parse an ffprobe rate string like '30/1' or '30000/1001' into (num, den)."""
    if not isinstance(value, str) or "/" not in value:
        return None
    num_s, den_s = value.split("/", 1)
    try:
        num, den = int(num_s), int(den_s)
    except ValueError:
        return None
    if num <= 0 or den <= 0:
        return None
    return num, den


def probe(path: Path, ffprobe_bin: Optional[str] = None) -> Dict:
    raw = _run([ffprobe_bin or FFPROBE, "-v", "error", "-protocol_whitelist", "file,pipe",
                "-f", "mov", "-show_streams", "-show_format", "-of", "json", str(path)])
    try:
        data = json.loads(raw)
        videos = [row for row in data["streams"] if row.get("codec_type") == "video"]
        if len(videos) != 1:
            raise ValueError("one video stream required")
        video = videos[0]
        duration = float(video.get("duration", data["format"]["duration"]))
        if not math.isfinite(duration) or not 1 <= round(duration * 1000) <= MAX_DURATION_MS:
            raise ValueError("invalid duration")
        rate = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
        audio = [row for row in data["streams"] if row.get("codec_type") == "audio"]
        sample_rate = int(audio[0]["sample_rate"]) if audio and audio[0].get("sample_rate") else None
        channels = int(audio[0]["channels"]) if audio and audio[0].get("channels") else None
        container_duration = float(data["format"]["duration"])
        audio_duration = float(audio[0]["duration"]) if audio and audio[0].get("duration") else None
        return {
            "width": int(video["width"]), "height": int(video["height"]),
            "duration_ms": round(duration * 1000), "codec_name": video["codec_name"],
            "pix_fmt": video["pix_fmt"],
            "fps_num": rate[0] if rate else 1,
            "fps_den": rate[1] if rate else 1,
            "audio_sample_rate": sample_rate,
            "audio_channels": channels,
            "has_audio": bool(audio and sample_rate),
            "container_duration_ms": round(container_duration * 1000),
            "audio_duration_ms": round(audio_duration * 1000) if audio_duration is not None else None,
        }
    except (KeyError, ValueError, TypeError) as exc:
        raise RepairError("media_probe_invalid", "media probe did not prove bounded video facts") from exc


def _post_probe(path: Path, tools: RenderTools) -> MediaProbe:
    """The REAL prepared-post probe (MediaProbe), used for the near-9:16 gate and
    as the input to the real `_encode_final`. Its PostRenderError is re-raised in
    this module's stable RepairError shape."""
    try:
        return post_render_probe(path, tools)
    except PostRenderError as error:
        raise RepairError(error.code, str(error)) from error


def _floor_at_rate(ms: int, num: int, den: int) -> int:
    """floor(ms * num / (1000 * den)) — an exact frame/sample boundary, never up."""
    return (ms * num) // (1000 * den)


def packet_ends(path: Path, ffprobe_bin: Optional[str] = None) -> Dict:
    """Packet-level start/end times per elementary stream (seconds) + packet counts.

    This is the real "where do the bytes begin and end" proof: the container
    duration can be rounded to a whole frame, so it cannot prove that a cut never
    runs past a word. The last packet's pts+duration is the end of that stream;
    the first packet's pts is where playback actually starts.
    """
    raw = _run([ffprobe_bin or FFPROBE, "-v", "error", "-show_packets", "-of", "json", str(path)])
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RepairError("media_probe_invalid", "packet probe did not return valid JSON") from exc
    ends: Dict[str, float] = {}
    starts: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for packet in data.get("packets", []):
        codec_type = packet.get("codec_type")
        if codec_type not in ("video", "audio"):
            continue
        try:
            start = float(packet.get("pts_time", 0))
            end = start + float(packet.get("duration_time", 0))
        except (TypeError, ValueError):
            continue
        ends[codec_type] = max(ends.get(codec_type, -1.0), end)
        starts[codec_type] = min(starts.get(codec_type, float("inf")), start)
        counts[codec_type] = counts.get(codec_type, 0) + 1
    return {"ends_s": ends, "starts_s": starts, "counts": counts}


def decoded_audio_facts(path: Path, ffmpeg_bin: Optional[str] = None,
                        channels: Optional[int] = None,
                        sample_rate: Optional[int] = None) -> Dict:
    """Decode the audio to raw PCM and count the true decoded samples.

    AAC carries encoder priming/padding; packet pts+duration can therefore differ
    from the samples a player actually decodes. This decodes to s16le and counts,
    which is the authoritative "how much audio actually plays" fact.
    """
    raw = _run([ffmpeg_bin or FFMPEG, "-nostdin", "-v", "error",
                "-i", str(path), "-map", "0:a:0", "-f", "s16le",
                "-acodec", "pcm_s16le", "pipe:1"],
               output_limit=MAX_SOURCE_BYTES)
    ch = channels or 1
    total = len(raw) // 2  # 16-bit
    per_channel = total // ch if ch else 0
    return {
        "decoded_sample_count": per_channel,
        "decoded_channels": ch,
        "decoded_sample_rate": sample_rate,
        "decoded_duration_ms": round(per_channel * 1000 / sample_rate) if sample_rate else None,
    }


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _frame_boundary_in_interval(start_ms: int, end_ms: int, num: int, den: int) -> int:
    """Return a video frame boundary (in frames) whose timestamp lies in the
    closed interval [start_ms, end_ms], or -1 when none exists.

    frame k's timestamp is k * frame_duration = k * den/num seconds
    = k * 1000*den/num ms, so k must satisfy
    start_ms <= k*1000*den/num <= end_ms  =>  ceil(start_ms*num/(1000*den)) <= k
    <= floor(end_ms*num/(1000*den)).
    """
    if num <= 0 or den <= 0:
        return -1
    k_min = _ceil_div(start_ms * num, 1000 * den)
    k_max = (end_ms * num) // (1000 * den)
    return k_min if k_min <= k_max else -1


def file_hash(path: Path, limit: int) -> Tuple[str, int]:
    if not 0 < path.stat().st_size <= limit:
        raise RepairError("artifact_too_large", "artifact is empty or exceeds byte limit")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RepairError("artifact_too_large", "artifact exceeds byte limit")
            digest.update(chunk)
    return digest.hexdigest(), total


def trim_source(source: Path, out: Path, start_ms: int, end_ms: int, *,
                ffmpeg_bin: Optional[str] = None, ffprobe_bin: Optional[str] = None) -> Dict:
    """Cut the source to an EXACT frame and audio-sample boundary, never past it.

    The video end is floor(end_ms * fps / 1000) frames (frames are dropped, never
    rounded up), and the audio end is floor(end_ms * sample_rate / 1000) samples.
    The container `-t` flag is deliberately not used: it rounds the video up to
    the next frame, which is exactly the 33 ms overshoot this seat is fixing.
    """
    source_facts = probe(source, ffprobe_bin)
    num, den = source_facts["fps_num"], source_facts["fps_den"]
    sr = source_facts["audio_sample_rate"]
    v_start = _floor_at_rate(start_ms, num, den)
    v_end = _floor_at_rate(end_ms, num, den)
    if v_end <= v_start:
        raise RepairError("trim_quantization", "commanded trim is shorter than one source video frame")
    args = [ffmpeg_bin or FFMPEG, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-noautorotate", "-f", "mov", "-i", str(source)]
    audio_filter = None
    if sr:
        a_start = (start_ms * sr) // 1000
        a_end = (end_ms * sr) // 1000
        if a_end <= a_start:
            raise RepairError("trim_quantization", "commanded trim is shorter than one source audio sample")
        audio_filter = f"atrim=start_sample={a_start}:end_sample={a_end},asetpts=N/SR/TB"
    video_filter = f"trim=start_frame={v_start}:end_frame={v_end},setpts=PTS-STARTPTS"
    if audio_filter:
        args += ["-filter_complex",
                 f"[0:v:0]{video_filter}[v];[0:a:0]{audio_filter}[a]",
                 "-map", "[v]", "-map", "[a]"]
    else:
        args += ["-vf", video_filter, "-map", "0:v:0"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if audio_filter:
        args += ["-c:a", "aac", "-b:a", "192k"]
    args += ["-movflags", "+faststart", "-threads", "2", str(out)]
    _run(args, output_file=out, max_bytes=MAX_FINAL_BYTES)
    # Enforce the clean-end bound at the packet level, inside the encoder itself.
    ends = packet_ends(out, ffprobe_bin)["ends_s"]
    for stream, label in (("video", "video"), ("audio", "audio")):
        if stream in ends and ends[stream] > end_ms / 1000.0 + 1e-6:
            raise RepairError("trim_overshoot", f"{label} ends after the commanded cut")
    facts = probe(out, ffprobe_bin)
    return facts


def encode_shorter_final(source: Path, out: Path, *,
                         overlay_path: Optional[Path] = None,
                         frame_height: Optional[int] = None,
                         normalize_pair_timing: bool = False,
                         ffmpeg_bin: Optional[str] = None,
                         ffprobe_bin: Optional[str] = None) -> Dict:
    """Delivery-encode the trimmed source through the REAL `_encode_final`.

    The real path owns the near-9:16 scale/crop, the `setsar=1` step, the
    `normalize_pair_timing` fps=30 step, the letterbox, the caption overlay and
    the two-attempt bitrate-budget fallback — nothing here re-implements page
    look. With no overlay the source is delivery-only (in_app/lyric); its own
    audio is still mapped through.
    """
    tools = RenderTools(ffmpeg=ffmpeg_bin or FFMPEG, ffprobe=ffprobe_bin or FFPROBE)
    source_probe = _post_probe(source, tools)
    in_app = overlay_path is None
    if overlay_path is not None:
        overlay_path = Path(overlay_path)
        if not 0 < overlay_path.stat().st_size <= MAX_OVERLAY_BYTES:
            raise RepairError("overlay_invalid", "caption overlay is empty or exceeds the byte limit")
    try:
        _encode_final(source, overlay_path, out, source_probe, tools, frame_height,
                      in_app=in_app, normalize_pair_timing=normalize_pair_timing)
    except PostRenderError as error:
        raise RepairError(error.code, str(error)) from error
    if out.stat().st_size > MAX_FINAL_BYTES:
        raise RepairError("delivery_budget_exceeded", "encoded final exceeded the byte budget")
    facts = probe(out, ffprobe_bin)
    if (facts["width"], facts["height"], facts["codec_name"], facts["pix_fmt"]) != (1080, 1920, "h264", "yuv420p"):
        raise RepairError("final_probe_mismatch", "encoded final facts do not match the delivery contract")
    return facts


def render_shorter(source_path: Path, output_directory: Path, command: Dict, *,
                   overlay_path: Optional[Path] = None,
                   frame_height: Optional[int] = None,
                   normalize_pair_timing: bool = False,
                   ffmpeg_bin: Optional[str] = None, ffprobe_bin: Optional[str] = None) -> Dict:
    """Validate the command, verify the source, trim it and encode a shorter final.

    Returns the receipt dict. The source must be upright square-pixel near-9:16
    treated media (the same rule as `_render_post_single`). ``overlay_path`` is
    the typed caption overlay the real `_encode_final` burns (None for in_app /
    lyric delivery); ``frame_height`` is the page frame's letterbox band (None
    for full-bleed 9:16); ``normalize_pair_timing`` mirrors the prepared-post
    ``paired_caption`` fps=30 step. All three ride the REAL encode path.
    """
    command = validate_command(command)
    source_path = Path(source_path).absolute()
    output_directory = Path(output_directory).absolute()
    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    tools = RenderTools(ffmpeg=ffmpeg_bin or FFMPEG, ffprobe=ffprobe_bin or FFPROBE)
    try:
        actual_source_sha, _ = file_hash(source_path, MAX_SOURCE_BYTES)
        if actual_source_sha != command["source_sha256"]:
            raise RepairError("source_sha256_mismatch", "source bytes do not match the immutable command")
        source_facts = probe(source_path, ffprobe_bin)
        if command["source_trim_end_ms"] > source_facts["duration_ms"]:
            raise RepairError("trim_out_of_bounds", "ordered trim exceeds the source duration")
        # The SAME near-9:16 acceptance the prepared-post renderer applies; the
        # real `_encode_final` then owns the scale/crop to 1080x1920.
        source_probe = _post_probe(source_path, tools)
        target_width = source_probe.height * 9 / 16
        near_vertical = abs(source_probe.width - target_width) <= target_width * 0.03
        if (source_probe.height < 768 or source_probe.sample_aspect_ratio not in {"1:1", "", "N/A", "0:1"}
                or source_probe.rotation != 0 or not near_vertical):
            raise RepairError("regeneration_required",
                              "source must be upright square-pixel near-9:16 video at least 768 pixels high")
        # Impossible/minimum-duration interval: when the authoritative clean
        # allowed interval [official_start_ms, official_end_ms] = [complete final
        # word end, next word onset] contains NO video frame boundary, there is
        # no cut that both keeps the held note and avoids the next word. Refuse
        # with the normal same-sound regeneration remedy; never clamp or fade.
        if _frame_boundary_in_interval(command["official_start_ms"], command["official_end_ms"],
                                       source_facts["fps_num"], source_facts["fps_den"]) < 0:
            raise RepairError("boundary_regeneration_needed",
                              "no output frame boundary lies after the complete final word and "
                              "before the next onset; regenerate the source/boundary under the "
                              "same approved sound")
        trimmed_path = output_directory / "trimmed.mp4"
        trimmed_facts = trim_source(source_path, trimmed_path,
                                    command["source_trim_start_ms"], command["source_trim_end_ms"],
                                    ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
        trimmed_sha, _ = file_hash(trimmed_path, MAX_FINAL_BYTES)
        final_path = output_directory / "final.mp4"
        final_facts = encode_shorter_final(trimmed_path, final_path,
                                           overlay_path=overlay_path, frame_height=frame_height,
                                           normalize_pair_timing=normalize_pair_timing,
                                           ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
        final_sha, final_length = file_hash(final_path, MAX_FINAL_BYTES)
        # The clean-end bound must hold on the DELIVERED bytes: the container,
        # the audio stream and the video stream must all end at or before the
        # commanded cut. AAC priming/padding can make the container and audio
        # stream report a few ms longer than the video stream, so all are
        # checked, never only the video frame count.
        end_ms = command["source_trim_end_ms"]
        final_packet = packet_ends(final_path, ffprobe_bin)
        final_ends = final_packet["ends_s"]
        final_starts = final_packet["starts_s"]
        for stream, label in (("video", "video"), ("audio", "audio")):
            if stream in final_ends and final_ends[stream] > end_ms / 1000.0 + 1e-6:
                raise RepairError("trim_overshoot", f"delivered {label} ends after the commanded cut")
            if stream in final_starts and final_starts[stream] > 1e-6:
                raise RepairError("trim_undershoot", f"delivered {label} starts after the commanded cut")
        if final_facts["container_duration_ms"] > end_ms:
            raise RepairError("trim_overshoot", "delivered container ends after the commanded cut")
        if final_facts["audio_duration_ms"] is not None and final_facts["audio_duration_ms"] > end_ms:
            raise RepairError("trim_overshoot", "delivered audio stream ends after the commanded cut")
        # Enforce the DECLARED frame count, not just the end time: an extra frame
        # is exactly the word the old 336-frame output exposed, so a frame count
        # above the command's expected_output_frames cannot pass.
        if final_packet["counts"].get("video", 0) > command["expected_output_frames"]:
            raise RepairError("trim_overshoot",
                              "delivered video has more frames than the commanded expected_output_frames")
        # AAC priming/padding: measure the DECODED audio coverage, not just the
        # packet pts. A packet-end that fits while decoded audio still runs into
        # the next word is a real overshoot.
        decoded = decoded_audio_facts(final_path, ffmpeg_bin,
                                      channels=final_facts["audio_channels"],
                                      sample_rate=final_facts["audio_sample_rate"])
        if decoded["decoded_duration_ms"] is not None \
                and decoded["decoded_duration_ms"] > command["source_trim_end_ms"]:
            raise RepairError("trim_overshoot",
                              "decoded audio coverage extends past the commanded cut")
        if final_sha == command["source_sha256"]:
            raise RepairError("final_is_source", "shorter final must be distinct from the original source")
        if final_sha == trimmed_sha:
            raise RepairError("final_is_trim", "delivery encode must produce new bytes")
        if abs(final_facts["duration_ms"] - trimmed_facts["duration_ms"]) > 80:
            raise RepairError("duration_changed", "delivery encode changed playback duration")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "command_digest": command_digest(command),
            "source_sha256": command["source_sha256"],
            "trimmed_source_sha256": trimmed_sha,
            "final_sha256": final_sha,
            "final_byte_length": final_length,
            "source_duration_ms": source_facts["duration_ms"],
            "trimmed_duration_ms": trimmed_facts["duration_ms"],
            "final_duration_ms": final_facts["duration_ms"],
            "source_trim_start_ms": command["source_trim_start_ms"],
            "source_trim_end_ms": command["source_trim_end_ms"],
            "native_official_start_ms": command["native_official_start_ms"],
            "measured_quantization_ms": command["measured_quantization_ms"],
            "approved_music_id": command["approved_music_id"],
            "width": final_facts["width"], "height": final_facts["height"],
            "codec": final_facts["codec_name"], "pix_fmt": final_facts["pix_fmt"],
            "final_container_duration_ms": final_facts["container_duration_ms"],
            "final_audio_duration_ms": final_facts["audio_duration_ms"],
            "ffprobe_source": source_facts, "ffprobe_final": final_facts,
            "final_packet_start_ms": {k: round(v * 1000, 2) for k, v in final_starts.items()},
            "final_packet_end_ms": {k: round(v * 1000, 2) for k, v in final_ends.items()},
            "final_packet_counts": final_packet["counts"],
            "decoded_audio": decoded,
        }
        (output_directory / "receipt.json").write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
        return receipt
    except Exception:
        shutil.rmtree(output_directory, ignore_errors=True)
        raise
