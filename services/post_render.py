"""Prepare immutable final post bytes from already treated source media.

This is a local service function, not an HTTP endpoint or scheduler. The caller
owns source authorization, durable jobs, retries, and final-object admission.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from burn_quality_gate import overlay_geometry_reasons
from services.caption_render import CaptionRenderRequest, CaptionStyle, render_caption_overlay
from services.ffmpeg import delivery_encode_args

REQUEST_SCHEMA = "content-lab.post-render-request.v1"
RECEIPT_SCHEMA = "posting-prepared-artifact/v1"
RENDERER_ID = "content-lab.prepared-post"
RENDERER_VERSION = "1"
MAX_FINAL_BYTES = 22 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_QA_BYTES = 2 * 1024 * 1024
MAX_DURATION_MS = 600_000
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(min_length=1, max_length=512)]


class PostRenderError(ValueError):
    """Stable failure reason; the caller decides whether a job may retry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PostRenderRequest(BaseModel):
    """Only immutable scalar fields cross this local rendering boundary.

    render_treatment_json is the exact coordinator-produced stableStringify
    byte representation. It is hashed as received, never reserialized here.
    """
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_: Literal[REQUEST_SCHEMA] = Field(alias="schema")
    slot_id: Identity
    slot_payload_sha256: Hash
    page_id: Identity
    program_id: Identity
    device_serial: Identity
    account: Identity
    source_sha256: Hash
    caption: str = Field(min_length=1, max_length=4000)
    caption_sha256: Hash
    render_treatment_json: str = Field(min_length=2, max_length=64 * 1024)
    treatment_sha256: Hash
    applied_treatment_sha256: Hash | None
    renderer_id: Literal[RENDERER_ID]
    renderer_version: Literal[RENDERER_VERSION]
    created_at_ms: int = Field(ge=0)

    @field_validator("slot_id", "page_id", "program_id", "device_serial", "account")
    @classmethod
    def clean_identity(cls, value: str) -> str:
        if value.strip() != value or not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid render identity")
        return value

    @model_validator(mode="after")
    def hashes_match(self) -> "PostRenderRequest":
        if sha256(self.caption.encode()) != self.caption_sha256:
            raise ValueError("caption sha256 mismatch")
        if sha256(self.render_treatment_json.encode()) != self.treatment_sha256:
            raise ValueError("exact render treatment JSON sha256 mismatch")
        _caption_request(self)
        return self


@dataclass(frozen=True)
class RenderTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    process_timeout_s: float = 120
    process_output_limit: int = 1024 * 1024

    def __post_init__(self):
        if not 0 < self.process_timeout_s <= 600 or self.process_output_limit < 1:
            raise ValueError("invalid process bounds")


@dataclass(frozen=True)
class MediaProbe:
    width: int
    height: int
    duration_ms: int
    video_codec: str
    pixel_format: str
    sample_aspect_ratio: str
    rotation: int


@dataclass(frozen=True)
class RenderedPost:
    final_path: Path
    qa_frame_path: Path
    receipt_path: Path
    receipt_json: bytes
    receipt_sha256: str
    source_probe: MediaProbe
    final_probe: MediaProbe
    decoded_video_frames: int
    qa_at_ms: int


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate treatment JSON field")
        result[key] = value
    return result


def _reject_nonfinite(_value: str):
    raise ValueError("nonfinite treatment JSON")


def _caption_request(request: PostRenderRequest) -> CaptionRenderRequest:
    treatment = json.loads(request.render_treatment_json, object_pairs_hook=_unique_object,
                           parse_constant=_reject_nonfinite)
    if not isinstance(treatment, dict) or set(treatment) != {
        "stylePreset", "filters", "captionStyle", "clipSpeed", "clipCrop"
    }:
        raise ValueError("exact complete slot render treatment is required")
    style = CaptionStyle.model_validate(treatment["captionStyle"])
    return CaptionRenderRequest.model_validate({
        "schema": "content-lab.caption-render-request.v1",
        "caption": request.caption,
        "style": style.model_dump(),
    })


def _run(args: list[str], tools: RenderTools, *, output_file: Path | None = None,
         output_limit: int = MAX_FINAL_BYTES) -> bytes:
    """Bound both pipes and subprocess group lifetime, including inherited pipes."""
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    output = [bytearray(), bytearray()]
    deadline = time.monotonic() + tools.process_timeout_s
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PostRenderError("process_timeout", "render subprocess exceeded its deadline")
                if output_file is not None and output_file.exists() and output_file.stat().st_size > output_limit:
                    raise PostRenderError("artifact_too_large", "render output exceeded its byte limit")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 64 * 1024)
                    if chunk:
                        output[key.data].extend(chunk)
                        if sum(map(len, output)) > tools.process_output_limit:
                            raise PostRenderError("process_output_limit", "render subprocess exceeded its output limit")
                    else:
                        selector.unregister(key.fileobj)
            if process.returncode:
                raise PostRenderError("process_failed", "render subprocess failed: " + bytes(output[1][-1000:]).decode(errors="replace"))
        return bytes(output[0])
    finally:
        # Kill the process group even after parent exit, so a descendant cannot survive cancellation.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def _probe(path: Path, tools: RenderTools) -> MediaProbe:
    raw = _run([tools.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov",
                "-show_streams", "-show_format", "-of", "json", str(path)], tools)
    try:
        data = json.loads(raw)
        videos = [row for row in data["streams"] if row.get("codec_type") == "video"]
        if len(videos) != 1:
            raise ValueError("one video stream required")
        video = videos[0]
        duration = float(video.get("duration", data["format"]["duration"]))
        if not math.isfinite(duration) or not 1 <= round(duration * 1000) <= MAX_DURATION_MS:
            raise ValueError("invalid duration")
        rotations = [int(row.get("rotation", 0)) for row in video.get("side_data_list", [])]
        rotations.append(int(video.get("tags", {}).get("rotate", 0)))
        return MediaProbe(int(video["width"]), int(video["height"]), round(duration * 1000),
                          video["codec_name"], video["pix_fmt"], video.get("sample_aspect_ratio", ""),
                          next((rotation for rotation in rotations if rotation != 0), 0))
    except (KeyError, ValueError, TypeError) as error:
        raise PostRenderError("media_probe_invalid", "media probe did not prove bounded video facts") from error


def _copy_verified_source(source: Path, destination: Path, expected_sha: str) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as reader:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise PostRenderError("source_invalid", "source must be a bounded regular file")
        digest, total = hashlib.sha256(), 0
        with destination.open("xb") as writer:
            while chunk := reader.read(min(64 * 1024, MAX_SOURCE_BYTES + 1 - total)):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
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


def _file_hash(path: Path, limit: int) -> tuple[str, int]:
    if not 0 < path.stat().st_size <= limit:
        raise PostRenderError("artifact_too_large", "artifact is empty or exceeds byte limit")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as file:
        while chunk := file.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise PostRenderError("artifact_too_large", "artifact exceeds byte limit")
            digest.update(chunk)
    return digest.hexdigest(), total


def _encode_final(source: Path, overlay: Path, final: Path, source_probe: MediaProbe,
                  tools: RenderTools) -> MediaProbe:
    base = [tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-noautorotate", "-f", "mov", "-i", str(source),
            "-protocol_whitelist", "file,pipe", "-i", str(overlay), "-filter_complex", "[0:v:0][1:v:0]overlay=0:0:format=auto[v]",
            "-map", "[v]", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1"]
    for attempt in range(2):
        encode = delivery_encode_args("tiktok_delivery_v1")
        if attempt:
            # Reserve ten percent container/rate-control margin and the full preset audio rate.
            bitrate = int(MAX_FINAL_BYTES * 8 * 0.90 * 1000 / source_probe.duration_ms) - 192_000
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
            if final.stat().st_size <= MAX_FINAL_BYTES and abs(probe.duration_ms - source_probe.duration_ms) <= 80:
                return probe
        except PostRenderError as error:
            if error.code != "artifact_too_large":
                raise
        final.unlink(missing_ok=True)
    raise PostRenderError("delivery_budget_exceeded", "both bounded encodes exceeded the byte or duration contract")


def render_post(source_path: Path, output_directory: Path, request: PostRenderRequest, *,
                font_dir: Path | None = None, tools: RenderTools = RenderTools(),
                clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> RenderedPost:
    """Render one prepared artifact; create a new output directory or leave none on failure.

    The source must already be upright 1080x1920 with the exact requested visual
    treatment. Unknown or different treatment provenance requires regeneration.
    No grade, speed, or crop filter is applied by this function.
    """
    request = PostRenderRequest.model_validate(request.model_dump(by_alias=True))
    if request.applied_treatment_sha256 != request.treatment_sha256:
        raise PostRenderError("regeneration_required", "source treatment provenance is unknown or differs from the requested treatment")
    if request.created_at_ms > clock_ms():
        raise PostRenderError("request_future_dated", "render request is future-dated")
    source_path, output_directory = Path(source_path).absolute(), Path(output_directory).absolute()
    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        source = output_directory / "source.mp4"
        _copy_verified_source(source_path, source, request.source_sha256)
        source_probe = _probe(source, tools)
        if (source_probe.width, source_probe.height, source_probe.sample_aspect_ratio, source_probe.rotation) != (1080, 1920, "1:1", 0):
            raise PostRenderError("regeneration_required", "source is not already upright square-pixel 1080x1920")
        caption_request = _caption_request(request)
        overlay = render_caption_overlay(caption_request, font_dir=font_dir or Path(__file__).parents[1] / "fonts")
        overlay_bytes = base64.b64decode(overlay.overlay.base64, validate=True)
        if sha256(overlay_bytes) != overlay.overlay.sha256.removeprefix("sha256:") or overlay.caption_sha256 != "sha256:" + request.caption_sha256:
            raise PostRenderError("caption_evidence_mismatch", "typed caption renderer evidence does not match requested bytes")
        if overlay_geometry_reasons(overlay.overlay.base64, caption_request.style.model_dump(exclude_none=True)):
            raise PostRenderError("caption_geometry_invalid", "typed caption overlay failed the existing geometry check")
        overlay_path = output_directory / "overlay.png"
        overlay_path.write_bytes(overlay_bytes)
        final_path = output_directory / "final.mp4"
        final_probe = _encode_final(source, overlay_path, final_path, source_probe, tools)
        if (final_probe.width, final_probe.height, final_probe.video_codec, final_probe.pixel_format,
            final_probe.sample_aspect_ratio, final_probe.rotation) != (1080, 1920, "h264", "yuv420p", "1:1", 0):
            raise PostRenderError("final_probe_mismatch", "encoded final media facts do not match delivery contract")
        if abs(final_probe.duration_ms - source_probe.duration_ms) > 80:
            raise PostRenderError("duration_changed", "caption-only delivery unexpectedly changed playback duration")
        progress = _run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                         "-f", "mov", "-i", str(final_path), "-map", "0:v:0",
                         "-progress", "pipe:1", "-nostats", "-f", "null", "-"], tools).decode("ascii")
        frames = [int(line.partition("=")[2]) for line in progress.splitlines() if line.startswith("frame=")]
        if not frames or frames[-1] <= 0 or "progress=end" not in progress:
            raise PostRenderError("final_decode_failed", "full final-video decode did not complete")
        qa_at_ms = final_probe.duration_ms // 2
        qa_path = output_directory / "qa.jpg"
        _run([tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
              "-ss", f"{qa_at_ms / 1000:.3f}", "-f", "mov", "-i", str(final_path),
              "-frames:v", "1", "-q:v", "3", "-update", "1", str(qa_path)], tools, output_file=qa_path, output_limit=MAX_QA_BYTES)
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
        receipt = {"schema": RECEIPT_SCHEMA, **{key: getattr(request, key) for key in (
            "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial", "account", "source_sha256",
            "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version")},
            "final_sha256": final_sha, "final_byte_length": final_length,
            "final_object_key": f"posting/final/{final_sha}.mp4", "mime_type": "video/mp4",
            "width": final_probe.width, "height": final_probe.height, "duration_ms": final_probe.duration_ms,
            "qa_frame_sha256": qa_sha, "completed_at_ms": completed}
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        receipt_path = output_directory / "receipt.json"
        receipt_path.write_bytes(receipt_json)
        (output_directory / "caption-render.json").write_text(overlay.model_dump_json(by_alias=True))
        (output_directory / "request.json").write_text(request.model_dump_json(by_alias=True))
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
