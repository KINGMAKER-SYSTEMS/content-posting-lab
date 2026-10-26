"""Timed-lyric prepared final rendering for the Content Lab.

A narrow sibling of ``services/post_render.py`` (v1). It reuses the v1 source
copy/verify, treatment-provenance, probe, and caption-render machinery, but
instead of a single full-canvas caption it overlays seven immutable, non-
overlapping lyric phrases — each rendered as its own transparent 1080x1920 PNG
through the existing typed caption renderer — at their exact time windows.

The output is a silent 1080x1920 H.264/yuv420p 30fps video; the official sound
is attached downstream. This module owns only the timed render path: the parent
owns manifest, loader, catalog, and durable job integration.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import json
import math
import shutil
import time
from pathlib import Path
from typing import Callable, Literal

from PIL import Image, ImageChops, ImageFilter, ImageStat
from pydantic import Field, field_validator, model_validator

from burn_quality_gate import overlay_geometry_reasons
from services.caption_render import CaptionRenderRequest, CaptionStyle, render_caption_overlay
from services.ffmpeg import delivery_encode_args
from services.page_frame import frame_band_height
from services.post_render import (
    CAPTION_DELIVERY_IN_APP,
    MAX_FINAL_BYTES,
    MAX_OVERLAY_BYTES,
    MAX_QA_BYTES,
    Hash,
    Identity,
    MediaProbe,
    PostRenderError,
    PostRenderRequest,
    RenderTools,
    RenderedPost,
    _caption_delivery,
    _file_hash,
    _frame_band_height,
    _probe,
    _run,
    _slot_treatment,
    sha256,
    source_visual_matches,
)

REQUEST_SCHEMA_V2 = "content-lab.post-render-request.v2"
RECEIPT_SCHEMA_V2 = "posting-prepared-artifact/v2"
RENDERER_ID = "content-lab.prepared-post"
RENDERER_VERSION_V2 = "2"
# The production timed set is an exact 20 second master.
TARGET_DURATION_MS = 20_000
DURATION_TOLERANCE_MS = 34

TIMING_FIELDS = frozenset({"id", "pageId", "account", "soundId", "durationMs",
                           "phrases", "canonicalCaption", "sha256"})
PHRASE_FIELDS = frozenset({"startMs", "endMs", "text"})

from services.lyric_timing import resolve_lyric_timing, LyricTimingError


class TimedPostRenderRequest(PostRenderRequest):
    """v2 prepared-post request: v1 immutable scalars plus a lyric timing binding.

    ``caption`` is the canonical full caption and must equal the resolver's
    ``canonicalCaption``; the per-phrase PNGs are derived from it at render time.
    Only full-bleed 9:16 delivery is supported initially.
    """
    schema_: Literal[REQUEST_SCHEMA_V2] = Field(alias="schema")
    renderer_version: Literal[RENDERER_VERSION_V2]
    lyric_timing_set_id: Identity
    lyric_timing_sha256: Hash
    sound_id: Identity
    source_program_id: Identity
    source_program_sha256: Hash

    @field_validator("lyric_timing_set_id", "sound_id")
    @classmethod
    def clean_timed_identity(cls, value: str) -> str:
        if value.strip() != value or not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid timed identity")
        return value

    @model_validator(mode="after")
    def timed_frame_is_full_vertical(self) -> "TimedPostRenderRequest":
        if _frame_band_height(self) is not None:
            raise ValueError("timed lyric render supports only full 9:16 vertical delivery")
        return self


def _resolve_timing(request: TimedPostRenderRequest) -> tuple[dict, list[dict]]:
    try:
        timing = resolve_lyric_timing(
            request.lyric_timing_set_id, page_id=request.page_id, account=request.account,
            sound_id=request.sound_id, expected_sha256=request.lyric_timing_sha256,
        )
    except LyricTimingError as error:
        raise PostRenderError(error.code, str(error)) from error
    if not isinstance(timing, dict) or not TIMING_FIELDS.issubset(timing):
        raise PostRenderError("lyric_timing_invalid", "lyric timing resolver returned an unrecognized record")
    if timing["id"] != request.lyric_timing_set_id:
        raise PostRenderError("lyric_timing_set_mismatch", "lyric timing set id does not match the request")
    if timing["pageId"] != request.page_id:
        raise PostRenderError("lyric_timing_page_mismatch", "lyric timing belongs to another page")
    if timing["account"] != request.account:
        raise PostRenderError("lyric_timing_account_mismatch", "lyric timing belongs to another account")
    if str(timing["soundId"]) != str(request.sound_id):
        raise PostRenderError("lyric_timing_sound_mismatch", "lyric timing belongs to another sound")
    if timing["sha256"] != request.lyric_timing_sha256:
        raise PostRenderError("lyric_timing_sha256_mismatch", "lyric timing bytes do not match the request")
    if timing["canonicalCaption"] != request.caption:
        raise PostRenderError("lyric_caption_mismatch", "request caption is not the timing's canonical caption")
    if (isinstance(timing["durationMs"], bool) or not isinstance(timing["durationMs"], int)
            or timing["durationMs"] != TARGET_DURATION_MS):
        raise PostRenderError("lyric_duration_invalid", "lyric timing master must be exactly 20 seconds")
    if (timing.get("sourceProgramId"), timing.get("sourceProgramSha256")) != (request.source_program_id, request.source_program_sha256):
        raise PostRenderError("lyric_source_program_mismatch", "lyric timing and source program revisions differ")
    return timing, _validate_phrases(timing["phrases"], timing["durationMs"])


def _validate_phrases(phrases, duration_ms: int) -> list[dict]:
    if not isinstance(phrases, list) or len(phrases) != 7:
        raise PostRenderError("lyric_phrases_invalid", "lyric timing must carry exactly seven phrases")
    validated = []
    previous_end = -1
    for phrase in phrases:
        if not isinstance(phrase, dict) or set(phrase) != PHRASE_FIELDS:
            raise PostRenderError("lyric_phrases_invalid", "each phrase must carry only startMs, endMs and text")
        start, end, text = phrase["startMs"], phrase["endMs"], phrase["text"]
        if (isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)):
            raise PostRenderError("lyric_phrases_invalid", "phrase timings must be whole milliseconds")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1000 or any(ord(c) < 32 for c in text):
            raise PostRenderError("lyric_phrases_invalid", "phrase text must be visible text")
        if not 0 <= start < end <= duration_ms:
            raise PostRenderError("lyric_phrases_invalid", "phrase timings must be ordered and inside the master duration")
        if start < previous_end:
            raise PostRenderError("lyric_phrases_overlap", "lyric phrases must not overlap")
        previous_end = end
        validated.append({"startMs": start, "endMs": end, "text": text})
    return validated



# The page-approved 20s clean background is bounded to the delivery byte cap.
# This keeps seven PNG+JSON pairs and four cue frames inside the existing
# conservative durable-worker disk reservation without changing v1 capacity.
def _copy_timed_source(source: Path, destination: Path, expected_sha: str) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as reader:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FINAL_BYTES:
            raise PostRenderError("source_invalid", "source must be a bounded regular file")
        digest, total = hashlib.sha256(), 0
        with destination.open("xb") as writer:
            while chunk := reader.read(min(64 * 1024, MAX_FINAL_BYTES + 1 - total)):
                total += len(chunk)
                if total > MAX_FINAL_BYTES:
                    raise PostRenderError("source_too_large", "source exceeds byte limit")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
        if total != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PostRenderError("source_changed", "source changed while being copied")
        if digest.hexdigest() != expected_sha:
            raise PostRenderError("source_sha256_mismatch", "source bytes do not match the immutable render request")


def _streams(path: Path, tools: RenderTools) -> list[dict]:
    raw = _run([tools.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
                "-show_streams", "-of", "json", str(path)], tools)
    try:
        data = json.loads(raw)
        return data.get("streams", [])
    except (ValueError, TypeError) as error:
        raise PostRenderError("media_probe_invalid", "media probe did not prove stream facts") from error


def _timed_graph(source_probe: MediaProbe, phrases: list[dict]) -> str:
    parts = []
    current = "[0:v:0]"
    for index, phrase in enumerate(phrases, start=1):
        label = f"o{index}"
        # Half-open intervals prevent two phrases at a shared boundary.
        start_s, end_s = phrase["startMs"] / 1000, phrase["endMs"] / 1000
        parts.append(f"{current}[{index}:v]overlay=0:0:format=auto:"
                     f"enable='gte(t,{start_s:.3f})*lt(t,{end_s:.3f})'[{label}]")
        current = f"[{label}]"
    parts.append(f"{current}format=yuv420p[v]")
    return ";".join(parts)


def _delivery_video_args() -> list[str]:
    args = list(delivery_encode_args("tiktok_delivery_v1"))
    for option in ("-c:a", "-b:a"):
        if option in args:
            index = args.index(option)
            del args[index:index + 2]
    return args


def _encode_timed_final(source: Path, phrase_pngs: list[Path], phrases: list[dict],
                        final: Path, source_probe: MediaProbe, tools: RenderTools) -> MediaProbe:
    graph = _timed_graph(source_probe, phrases)
    base = [tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-noautorotate", "-f", "mov", "-i", str(source)]
    for png in phrase_pngs:
        base += ["-protocol_whitelist", "file,pipe", "-framerate", "30", "-loop", "1",
                 "-t", f"{TARGET_DURATION_MS / 1000:.3f}", "-i", str(png)]
    base += ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[v]", "-an", "-map_metadata", "-1", "-map_chapters", "-1"]
    for attempt in range(2):
        encode = _delivery_video_args()
        if attempt:
            bitrate = int(MAX_FINAL_BYTES * 8 * 0.90 * 1000 / TARGET_DURATION_MS)
            if bitrate <= 0:
                raise PostRenderError("delivery_budget_impossible", "duration cannot fit the delivery byte budget")
            for option in ("-crf", "-minrate", "-maxrate", "-bufsize"):
                index = encode.index(option)
                del encode[index:index + 2]
            encode += ["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2)]
        try:
            _run([*base, *encode, "-threads", "2", "-fs", str(MAX_FINAL_BYTES + 1), str(final)],
                 tools, output_file=final)
            probe = _probe(final, tools)
            if (final.stat().st_size <= MAX_FINAL_BYTES
                    and abs(probe.duration_ms - TARGET_DURATION_MS) <= DURATION_TOLERANCE_MS):
                return probe
        except PostRenderError as error:
            if error.code != "artifact_too_large":
                raise
        final.unlink(missing_ok=True)
    raise PostRenderError("delivery_budget_exceeded", "both bounded encodes exceeded the byte or duration contract")


def _verify_edge_phrase_frames(final: Path, pngs: list[Path], phrases: list[dict], output: Path, tools: RenderTools) -> list[dict]:
    """Check actual decoded first/last phrase frames against opaque PNG pixels.

    This validates glyph presence at the canonical 30fps sample times. It does
    not claim phoneme-level alignment with the official downstream sound.
    """
    evidence = []
    for ordinal in sorted({0, len(phrases) - 1}):
        phrase = phrases[ordinal]
        frames = sorted({math.ceil(phrase["startMs"] * 30 / 1000), math.ceil(phrase["endMs"] * 30 / 1000) - 1})
        with Image.open(pngs[ordinal]) as image:
            overlay = image.convert("RGBA")
        # Erode antialiased boundaries to compare solid glyph pixels only.
        mask = overlay.getchannel("A").point(lambda x: 255 if x >= 250 else 0).filter(ImageFilter.MinFilter(3))
        if sum(mask.histogram()[1:]) < 50:
            raise PostRenderError("phrase_frame_evidence_invalid", "phrase has no stable opaque glyph evidence")
        for frame in frames:
            path = output / f"phrase-{ordinal + 1}-frame-{frame}.png"
            _run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                  "-f", "mov", "-i", str(final), "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1", "-update", "1", str(path)],
                 tools, output_file=path, output_limit=MAX_OVERLAY_BYTES)
            with Image.open(path) as decoded:
                difference = ImageChops.difference(decoded.convert("RGB"), overlay.convert("RGB"))
                error = max(ImageStat.Stat(difference, mask).mean)
            if error > 35:
                raise PostRenderError("phrase_frame_evidence_invalid", "decoded phrase pixels do not match their typed overlay")
            evidence.append({"phraseIndex": ordinal, "frame": frame, "maxMeanGlyphError": round(error, 3), "sha256": _file_hash(path, MAX_OVERLAY_BYTES)[0]})
    return evidence


def render_timed_post(source_path: Path, output_directory: Path, request: TimedPostRenderRequest, *,
                      font_dir: Path | None = None, tools: RenderTools = RenderTools(),
                      clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> RenderedPost:
    """Render one timed-lyric prepared artifact; create a new output directory or leave none on failure.

    The source must already be upright, silent, 1080x1920 video with the exact
    requested visual treatment. This renderer only verifies/copies that source,
    renders the immutable lyric phrases as transparent PNGs at their exact cue
    windows, and delivery-encodes a silent 20s 1080x1920 video. It never
    regrades, re-speeds, re-crops, or fabricates operator/generation receipts.
    """
    request = TimedPostRenderRequest.model_validate(request.model_dump(by_alias=True))
    if not source_visual_matches(request):
        raise PostRenderError("regeneration_required", "source treatment provenance is unknown or differs from the requested treatment")
    if request.created_at_ms > clock_ms():
        raise PostRenderError("request_future_dated", "render request is future-dated")
    if _caption_delivery(request) == CAPTION_DELIVERY_IN_APP:
        raise PostRenderError("in_app_timed_unsupported", "in_app caption delivery is not supported for timed lyric rendering")
    timing, phrases = _resolve_timing(request)
    from services.control_plane_lyric_video import lyric_treatment_matches
    if not lyric_treatment_matches(_slot_treatment(request)):
        raise PostRenderError("lyric_source_treatment_mismatch", "timed lyric source must retain the approved once-graded 20-second program")

    source_path, output_directory = Path(source_path).absolute(), Path(output_directory).absolute()
    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        source = output_directory / "source.mp4"
        _copy_timed_source(source_path, source, request.source_sha256)
        source_probe = _probe(source, tools)
        if any(stream.get("codec_type") == "audio" for stream in _streams(source, tools)):
            raise PostRenderError("source_has_audio", "timed lyric source must be silent")
        target_width = source_probe.height * 9 / 16
        near_vertical = abs(source_probe.width - target_width) <= target_width * 0.03
        if ((source_probe.width, source_probe.height) != (1080, 1920) or source_probe.sample_aspect_ratio not in {"1:1", "", "N/A", "0:1"}
                or source_probe.rotation != 0 or not near_vertical):
            raise PostRenderError("regeneration_required", "source must be upright square-pixel 1080x1920 video")
        if abs(source_probe.duration_ms - TARGET_DURATION_MS) > DURATION_TOLERANCE_MS:
            raise PostRenderError("source_duration_invalid", "timed lyric source must be a 20 second master")
        if frame_band_height(_slot_treatment(request)) is not None:
            raise PostRenderError("unsupported_frame_treatment", "timed lyric render supports only full 9:16 vertical delivery")

        style = CaptionStyle.model_validate(_slot_treatment(request)["captionStyle"])
        phrase_pngs: list[Path] = []
        phrase_records: list[dict] = []
        for index, phrase in enumerate(phrases, start=1):
            caption_request = CaptionRenderRequest.model_validate({
                "schema": "content-lab.caption-render-request.v1",
                "caption": phrase["text"],
                "style": style.model_dump(),
            })
            overlay = render_caption_overlay(caption_request,
                                             font_dir=font_dir or Path(__file__).parents[1] / "fonts",
                                             fit_rows=None)
            overlay_bytes = base64.b64decode(overlay.overlay.base64, validate=True)
            if (not 0 < len(overlay_bytes) <= MAX_OVERLAY_BYTES
                    or sha256(overlay_bytes) != overlay.overlay.sha256.removeprefix("sha256:")
                    or overlay.caption_sha256 != "sha256:" + sha256(phrase["text"].encode())):
                raise PostRenderError("caption_evidence_mismatch", "typed caption renderer evidence does not match phrase bytes")
            if overlay_geometry_reasons(overlay.overlay.base64,
                                        caption_request.style.model_dump(exclude_none=True), band_rows=None):
                raise PostRenderError("caption_geometry_invalid", "typed caption overlay failed the existing geometry check")
            png_path = output_directory / f"phrase-{index}.png"
            png_path.write_bytes(overlay_bytes)
            (output_directory / f"phrase-{index}-caption-render.json").write_text(overlay.model_dump_json(by_alias=True))
            phrase_pngs.append(png_path)
            phrase_records.append({"startMs": phrase["startMs"], "endMs": phrase["endMs"],
                                   "text": phrase["text"], "pngSha256": sha256(overlay_bytes)})

        _resolve_timing(request)
        final_path = output_directory / "final.mp4"
        final_probe = _encode_timed_final(source, phrase_pngs, phrases, final_path, source_probe, tools)
        if (final_probe.width, final_probe.height, final_probe.video_codec, final_probe.pixel_format,
            final_probe.sample_aspect_ratio, final_probe.rotation) != (1080, 1920, "h264", "yuv420p", "1:1", 0):
            raise PostRenderError("final_probe_mismatch", "encoded final media facts do not match delivery contract")
        if abs(final_probe.duration_ms - TARGET_DURATION_MS) > DURATION_TOLERANCE_MS:
            raise PostRenderError("duration_changed", "timed lyric delivery is not a 20 second video")
        if any(stream.get("codec_type") == "audio" for stream in _streams(final_path, tools)):
            raise PostRenderError("final_has_audio", "timed lyric delivery must be silent")

        progress = _run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                         "-f", "mov", "-i", str(final_path), "-map", "0:v:0",
                         "-progress", "pipe:1", "-nostats", "-f", "null", "-"], tools).decode("ascii")
        frames = [int(line.partition("=")[2]) for line in progress.splitlines() if line.startswith("frame=")]
        if not frames or frames[-1] != 600 or "progress=end" not in progress:
            raise PostRenderError("final_decode_failed", "full final-video decode did not complete")
        phrase_frame_evidence = _verify_edge_phrase_frames(final_path, phrase_pngs, phrases, output_directory, tools)
        qa_at_ms = final_probe.duration_ms // 2
        qa_path = output_directory / "qa.jpg"
        _run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
              "-ss", f"{qa_at_ms / 1000:.3f}", "-f", "mov", "-i", str(final_path),
              "-frames:v", "1", "-q:v", "3", "-update", "1", str(qa_path)], tools,
             output_file=qa_path, output_limit=MAX_QA_BYTES)
        with Image.open(qa_path) as frame:
            frame.load()
            if frame.size != (1080, 1920) or frame.format != "JPEG":
                raise PostRenderError("qa_frame_invalid", "final QA frame is not a decodable 1080x1920 JPEG")

        final_sha, final_length = _file_hash(final_path, MAX_FINAL_BYTES)
        qa_sha, _ = _file_hash(qa_path, MAX_QA_BYTES)
        if final_sha == request.source_sha256:
            raise PostRenderError("final_is_source", "prepared final must be distinct from source")
        completed = clock_ms()
        if completed < request.created_at_ms:
            raise PostRenderError("clock_regressed", "render completion precedes request creation")

        receipt = {"schema": RECEIPT_SCHEMA_V2, **{key: getattr(request, key) for key in (
            "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial", "account", "source_sha256",
            "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version")},
            "lyric_timing_set_id": request.lyric_timing_set_id,
            "lyric_timing_sha256": request.lyric_timing_sha256,
            "sound_id": request.sound_id,
            "source_program_id": request.source_program_id,
            "source_program_sha256": request.source_program_sha256,
            "final_sha256": final_sha, "final_byte_length": final_length,
            "final_object_key": f"posting/final/{final_sha}.mp4", "mime_type": "video/mp4",
            "width": final_probe.width, "height": final_probe.height, "duration_ms": final_probe.duration_ms,
            "qa_frame_sha256": qa_sha, "completed_at_ms": completed,
            "phrases": phrase_records, "phrase_frame_evidence": phrase_frame_evidence}
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        receipt_path = output_directory / "receipt.json"
        receipt_path.write_bytes(receipt_json)
        (output_directory / "request.json").write_text(request.model_dump_json(by_alias=True))
        (output_directory / "timing.json").write_text(json.dumps(timing, sort_keys=True, ensure_ascii=False))
        (output_directory / "decode.json").write_text(json.dumps({
            "source_probe": source_probe.__dict__, "final_probe": final_probe.__dict__,
            "decoded_video_frames": frames[-1], "qa_at_ms": qa_at_ms,
            "final_sha256": final_sha, "qa_frame_sha256": qa_sha,
        }, sort_keys=True))
        source.unlink()
        return RenderedPost(final_path, qa_path, receipt_path, receipt_json, sha256(receipt_json),
                            source_probe, final_probe, frames[-1], qa_at_ms)
    except BaseException:
        shutil.rmtree(output_directory)
        raise
