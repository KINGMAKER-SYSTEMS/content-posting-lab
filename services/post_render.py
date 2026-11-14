"""Prepare immutable final post bytes from already treated source media.

This is a local service function, not an HTTP endpoint or scheduler. The caller
owns source authorization, durable jobs, retries, and final-object admission.
"""
from __future__ import annotations

import base64
import hashlib
import io
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

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer
from pydantic_core import PydanticCustomError

from burn_quality_gate import overlay_geometry_reasons
from services.caption_render import CaptionRenderRequest, CaptionStyle, render_caption_with_background
from services.ffmpeg import delivery_encode_args
from services.page_frame import frame_band_height, frame_band_rows, frame_fit, letterbox_filter
from services.pool_supplied_pages import is_pool_supplied
from services.source_treatment import normalized_visual_treatment

REQUEST_SCHEMA = "content-lab.post-render-request.v1"
RECEIPT_SCHEMA = "posting-prepared-artifact/v1"
RENDERER_ID = "content-lab.prepared-post"
RENDERER_VERSION = "1"
MAX_FINAL_BYTES = 22 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_QA_BYTES = 2 * 1024 * 1024
# A 1080x1920 RGBA canvas is about 7.9 MiB before PNG encoding. Keep a
# conservative explicit bound so the job scheduler can reserve peak disk.
MAX_OVERLAY_BYTES = 16 * 1024 * 1024
MAX_DURATION_MS = 600_000
# Proof the caption's words are in the delivered pixels, measured as CHANGE against
# the picture around them. The reference is the same source frame at the same time,
# through the same delivery graph, with the caption's background (box or highlight)
# drawn but the words left out. Where the words would change that reference by more
# than CAPTION_INK_CONTRAST levels, the burned frame must have moved at least
# CAPTION_INK_MIN_SHARE of the way toward the words' colour, on average. A box alone
# cannot pass (the reference already has it), matching colours alone prove nothing
# (white text on a white clip "matches" whether or not it was drawn), and white text
# on a dark box over a white clip is measured against the box, so it is visible and
# passes. Too few changed pixels means the words cannot be seen against what is
# directly behind them (no box and the same colour as the picture, or text in its own
# box's colour): CAPTION_LOW_CONTRAST. Enough contrast but no change in the burned
# frame: CAPTION_NOT_DRAWN.
CAPTION_INK_MIN_SHARE = 0.40
CAPTION_INK_ALPHA_MIN = 200
CAPTION_INK_CONTRAST = 48
CAPTION_INK_MIN_PIXELS = 64
# The box or highlight itself must be in the burned frame too. Words measured against a box-only reference would
# otherwise pass when nothing at all was drawn and the clip happens to be the words' colour (white words on a
# black box over a white clip, with no overlay in the video). A box pixel counts as shown only when the burned
# colour is nearer the box colour than the bare source frame's colour at that pixel (a plain colour match is not
# enough: light-grey footage sits within tolerance of a #c0c0c0 box without any overlay). Box pixels within
# CAPTION_BOX_DISTINCT levels of the source are left out of that tally (a black box over a black clip burns the source
# colour and is a real caption; nothing can be told either way), and there the words are judged against the bare
# source, so an unchanged source pixel can never count as ink. At least CAPTION_BOX_MIN_MATCH of the counted box
# pixels must be shown.
CAPTION_BOX_ALPHA_MIN = 200
CAPTION_BOX_WORDS_TOLERANCE = 24
CAPTION_BOX_DISTINCT = 24
CAPTION_BOX_MIN_MATCH = 0.60
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
    caption: str = Field(max_length=4000)
    source_text_mode: Literal["lyric"] | None = None
    caption_sha256: Hash
    render_treatment_json: str = Field(min_length=2, max_length=64 * 1024)
    treatment_sha256: Hash
    source_visual_treatment_json: str | None = Field(default=None, max_length=64 * 1024)
    source_visual_treatment_sha256: Hash | None = None
    renderer_id: Literal[RENDERER_ID]
    renderer_version: Literal[RENDERER_VERSION]
    created_at_ms: int = Field(ge=0)
    source_duration_repair: dict | None = None

    @field_validator("slot_id", "page_id", "program_id", "device_serial", "account")
    @classmethod
    def clean_identity(cls, value: str) -> str:
        if value.strip() != value or not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid render identity")
        return value

    @model_serializer(mode="wrap")
    def preserve_ordinary_request_bytes(self, handler):
        # Old durable request/idempotency hashes must not gain a null field.
        result = handler(self)
        if self.source_text_mode is None:
            result.pop("source_text_mode", None)
        if self.source_duration_repair is None:
            result.pop("source_duration_repair", None)
        return result

    @model_validator(mode="after")
    def repair_command_schema(self) -> "PostRenderRequest":
        # Timed v2 owns its own refusal; a v1 request carrying a repair command
        # must still be schema-valid even when the capability is off, so stored
        # durable jobs can be reloaded and refused by the dispatch fence without
        # losing their identity. Capability admission is enforced at the HTTP
        # submit boundary, never here.
        if self.source_duration_repair is not None and self.schema_ == REQUEST_SCHEMA:
            from services.source_duration_repair import validate_command
            try:
                validate_command(self.source_duration_repair)
            except Exception as error:
                raise PydanticCustomError(
                    getattr(error, "code", None) or "repair_command_invalid",
                    str(error),
                )
        return self

    @model_validator(mode="after")
    def hashes_match(self) -> "PostRenderRequest":
        if sha256(self.caption.encode()) != self.caption_sha256:
            raise ValueError("caption sha256 mismatch")
        if sha256(self.render_treatment_json.encode()) != self.treatment_sha256:
            raise ValueError("exact render treatment JSON sha256 mismatch")
        if (self.source_visual_treatment_json is None) != (self.source_visual_treatment_sha256 is None):
            raise ValueError("source visual evidence must include JSON and hash")
        if self.source_visual_treatment_json is not None:
            if sha256(self.source_visual_treatment_json.encode()) != self.source_visual_treatment_sha256:
                raise ValueError("source visual treatment sha256 mismatch")
            actual_visual = json.loads(self.source_visual_treatment_json)
            if not isinstance(actual_visual, dict) or set(actual_visual) != {"filters", "clipSpeed", "clipCrop"}:
                raise ValueError("source visual evidence must contain only filters, clipSpeed and clipCrop")
            normalized_visual_treatment(actual_visual)
        if self.source_text_mode == "lyric":
            if self.caption != "" or self.renderer_version != "1":
                raise ValueError("lyric source requires an empty v1 caption")
            _caption_style(self)
            _caption_delivery(self)
        else:
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


SOURCE_DURATION_REPAIR_ENV = "CONTENT_LAB_SOURCE_DURATION_REPAIR"


def source_duration_repair_enabled() -> bool:
    """Whether the Lab admits source-duration-repair commands (default off)."""
    return os.getenv(SOURCE_DURATION_REPAIR_ENV) == "on"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate treatment JSON field")
        result[key] = value
    return result


def _reject_nonfinite(_value: str):
    raise ValueError("nonfinite treatment JSON")


SLOT_TREATMENT_FIELDS = frozenset({"stylePreset", "filters", "captionStyle", "clipSpeed", "clipCrop"})
# The page frame and its fit are not source treatment. Absent frame means
# full-bleed 9:16; frameFit is legal only with a non-9:16 frame.
# captionDelivery: "in_app" asks for a clean clip (the caption is bound but not
# drawn) so TikTok's own text can sit on the video in the app.
SLOT_TREATMENT_OPTIONAL_FIELDS = frozenset({"frame", "frameFit", "captionDelivery"})
CAPTION_DELIVERY_IN_APP = "in_app"


def _slot_treatment(request: PostRenderRequest) -> dict:
    treatment = json.loads(request.render_treatment_json, object_pairs_hook=_unique_object,
                           parse_constant=_reject_nonfinite)
    if (not isinstance(treatment, dict) or not SLOT_TREATMENT_FIELDS <= set(treatment)
            or set(treatment) - SLOT_TREATMENT_FIELDS - SLOT_TREATMENT_OPTIONAL_FIELDS):
        raise ValueError("exact complete slot render treatment is required")
    frame_fit(treatment)
    return treatment


def _frame_band_height(request: PostRenderRequest) -> int | None:
    return frame_band_height(_slot_treatment(request))


def _caption_delivery(request: PostRenderRequest) -> str | None:
    """Return "in_app" for app-side text; None for burned or existing lyric pixels.

    Absent retains burned delivery for ordinary captions. Only the exact string "in_app" is
    accepted; any other value is a closed refusal so the Worker can fall back
    to a burned render.
    """
    treatment = _slot_treatment(request)
    if "captionDelivery" not in treatment:
        return None
    value = treatment["captionDelivery"]
    if not isinstance(value, str) or value != CAPTION_DELIVERY_IN_APP:
        raise PostRenderError("caption_delivery_invalid", "captionDelivery must be 'in_app' when present")
    # Existing lyrics are source pixels, never phone text or a burned fallback.
    return None if request.source_text_mode == "lyric" else value


def _caption_style(request: PostRenderRequest) -> CaptionStyle:
    treatment = _slot_treatment(request)
    style = CaptionStyle.model_validate(treatment["captionStyle"])
    if frame_band_height(treatment) is not None:
        # A framed page's caption sits in the very middle of the picture
        # (owner decision, 2026-09-28): a top or bottom placement would land
        # on the black bars. Font, size, colour and line breaks are unchanged.
        style = style.model_copy(update={"position": "middle", "offset_pct": 0})
    return style


def _caption_request(request: PostRenderRequest) -> CaptionRenderRequest:
    style = _caption_style(request)
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


def _video_graph(source_probe: MediaProbe, frame_height: int | None = None, *, in_app: bool = False,
                 normalize_pair_timing: bool = False) -> str:
    chain = []
    if (source_probe.width, source_probe.height) != (1080, 1920):
        if abs(source_probe.width * 16 - source_probe.height * 9) <= 32:
            # Chroma alignment can make an exact provider crop a few pixels
            # narrow. Preserve its complete selected frame at delivery size.
            chain.append("scale=1080:1920:flags=lanczos,setsar=1")
        else:
            # Native vertical generators can return a near-9:16 coded frame
            # such as 704x1280. Remove the small excess edge before scaling so
            # delivery is true 9:16 without stretching the image.
            chain.append("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
                         "crop=1080:1920,setsar=1")
    elif source_probe.sample_aspect_ratio != "1:1":
        chain.append("setsar=1")
    if frame_height is not None:
        # The delivery frame is exactly 1080x1920 here. A page frame keeps its
        # centred band and letterboxes it in plain black on the same canvas.
        chain.append(letterbox_filter(frame_height))
    if normalize_pair_timing:
        # Resample before the paths diverge. FFmpeg 5.1 can round a half-frame
        # at EOF differently with an overlay if only the encoder sets 30 fps.
        chain.append("fps=30")
    if in_app:
        # No caption overlay input: the clean clip is delivery-only. The
        # encoder's -pix_fmt yuv420p still turns the chain into the delivery
        # pixel format, so a bare source still needs an explicit [v] label.
        if not chain:
            return "[0:v:0]format=yuv420p[v]"
        return f"[0:v:0]{','.join(chain)}[delivery];[delivery]format=yuv420p[v]"
    if not chain:
        return "[0:v:0][1:v:0]overlay=0:0:format=auto[v]"
    return f"[0:v:0]{','.join(chain)}[delivery];[delivery][1:v:0]overlay=0:0:format=auto[v]"


def _encode_final(source: Path, overlay: Path | None, final: Path, source_probe: MediaProbe,
                  tools: RenderTools, frame_height: int | None = None, *, in_app: bool = False,
                  normalize_pair_timing: bool = False) -> MediaProbe:
    graph = _video_graph(source_probe, frame_height, in_app=in_app, normalize_pair_timing=normalize_pair_timing)
    base = [tools.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-noautorotate", "-f", "mov", "-i", str(source)]
    if in_app:
        # The clean graph has no caption overlay input; the source's own audio
        # (input 0) is still mapped through.
        base += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1"]
    else:
        base += ["-protocol_whitelist", "file,pipe", "-i", str(overlay), "-filter_complex", graph,
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


@dataclass(frozen=True)
class _InkField:
    """Per-pixel ink evidence inside the overlay's bounding box (rows from ``top``)."""

    top: int
    measured: np.ndarray
    progress: np.ndarray
    words: np.ndarray
    box_match: float | None


def _ink_field(overlay_png: bytes, qa_frame: Image.Image, reference_frame: Image.Image,
               background_png: bytes | None = None,
               source_frame: Image.Image | None = None) -> _InkField | None:
    """Per-pixel ink evidence over the overlay's bounding box; None when the overlay is empty.

    The single measurement behind ``_caption_ink`` (one score for the caption) and
    ``_caption_line_ink`` (one score per drawn line); see ``_caption_ink`` for the rules.
    """
    if background_png is not None and source_frame is None:
        raise ValueError("a background needs the bare source frame to tell it from the burned frame")
    with Image.open(io.BytesIO(overlay_png)) as opened:
        overlay = opened.convert("RGBA")
    box = overlay.getchannel("A").getbbox()
    if box is None:
        return None
    layer = np.asarray(overlay.crop(box), dtype=np.float32)
    alpha, colour = layer[..., 3], layer[..., :3]
    reference = np.asarray(reference_frame.convert("RGB").crop(box), dtype=np.float32)
    burned = np.asarray(qa_frame.convert("RGB").crop(box), dtype=np.float32)
    behind = reference
    near_box = None
    box_match = None
    words = alpha >= CAPTION_INK_ALPHA_MIN
    if background_png is not None:
        with Image.open(io.BytesIO(background_png)) as opened:
            background = np.asarray(opened.convert("RGBA").crop(box), dtype=np.float32)
        bare = np.asarray(source_frame.convert("RGB").crop(box), dtype=np.float32)
        box_colour = background[..., :3]
        under_box = background[..., 3] >= CAPTION_BOX_ALPHA_MIN
        distinct = np.abs(box_colour - bare).max(axis=-1) > CAPTION_BOX_DISTINCT
        behind = np.where((under_box & distinct)[..., None], reference, bare)
        near_box = under_box & ~distinct
        box_coloured = under_box & (np.abs(colour - box_colour).max(axis=-1) <= CAPTION_BOX_WORDS_TOLERANCE)
        # Overlay pixels in the box's own colour are the box, not the words.
        words &= ~box_coloured
        box_pixels = box_coloured & distinct
        if box_pixels.any():
            shown = np.abs(burned - box_colour).sum(axis=-1) < np.abs(burned - bare).sum(axis=-1)
            box_match = float(shown[box_pixels].mean())
    a3 = (alpha / 255.0)[..., None]
    expected = colour * a3 + behind * (1.0 - a3)
    change = expected - behind
    measured = (alpha >= CAPTION_INK_ALPHA_MIN) & (np.abs(change).max(axis=-1) > CAPTION_INK_CONTRAST)

    def progress_of(wanted: np.ndarray, got: np.ndarray) -> np.ndarray:
        power = (wanted * wanted).sum(axis=-1)
        return np.clip((wanted * got).sum(axis=-1) / np.where(power > 0, power, 1.0), -0.5, 1.5)

    progress = progress_of(change, burned - behind)
    if near_box is not None:
        change_box = colour * a3 + reference * (1.0 - a3) - reference
        measured &= ~near_box | (np.abs(change_box).max(axis=-1) > CAPTION_INK_CONTRAST)
        progress = np.where(near_box, np.minimum(progress, progress_of(change_box, burned - reference)), progress)
    return _InkField(box[1], measured, np.where(measured, progress, 0.0), words, box_match)


def _caption_ink(overlay_png: bytes, qa_frame: Image.Image, reference_frame: Image.Image,
                 background_png: bytes | None = None,
                 source_frame: Image.Image | None = None) -> tuple[float, int, float | None]:
    """What of the caption reached the burned frame: ``(words share, words pixels measured, box match)``.

    The overlay is laid over the 1080x1920 delivery frame at 0:0 (see
    ``_video_graph``), a framed page's band included, so its pixels line up with
    all the frames without any offset. Only the overlay's own bounding box is read.

    ``reference_frame`` is the delivery frame with the caption's background drawn and
    the words left out (the bare source frame when the style has no background);
    ``background_png`` is that background on its own, or None; ``source_frame`` is the
    bare delivery frame (required with a background).

    What is directly behind a word pixel is the box colour when the box really differs
    from the source there (more than CAPTION_BOX_DISTINCT levels), and the bare source
    otherwise. A box that hardly differs from the picture is no evidence of anything, so an
    unchanged source pixel can never count as ink: there the words are judged against the
    bare source AND against the box, and a pixel scores the lower of the two (unchanged
    footage scores nothing against the source, a box with no words scores nothing against
    the box). Codec drift on an unchanged pixel is under CAPTION_INK_CONTRAST x
    CAPTION_INK_MIN_SHARE levels (about 19), which is what keeps it below the gate.

    For each overlay pixel with alpha at least CAPTION_INK_ALPHA_MIN the overlay should
    turn what is behind it into the expected colour. A box or highlight pixel expects what
    is already there, so it changes nothing and is skipped with every other pixel whose
    expected change is under CAPTION_INK_CONTRAST. Each measured pixel scores how far the
    burned frame moved from what is behind it toward the expected colour (1.0 is all the
    way, 0.0 not at all, clipped to -0.5..1.5); the words share is the mean, and
    ``(0.0, count, ...)`` when fewer than CAPTION_INK_MIN_PIXELS pixels can be measured.

    The box match is the share of the box's own pixels (the background, away from the
    words) that are shown in the burned frame: nearer the box colour than the bare source
    is at that pixel. Box pixels within CAPTION_BOX_DISTINCT levels of the source are left
    out of the tally altogether (a black box over a black clip burns the source colour and
    is a real caption; nothing can be told either way); None when none are left.
    """
    field = _ink_field(overlay_png, qa_frame, reference_frame, background_png, source_frame)
    if field is None:
        return 0.0, 0, None
    count = int(field.measured.sum())
    if count < CAPTION_INK_MIN_PIXELS:
        return 0.0, count, field.box_match
    return float(field.progress[field.measured].mean()), count, field.box_match


def _line_rows(line_centers: list[int], height: int = 1920) -> list[tuple[int, int]]:
    """The frame rows each drawn line owns: every row goes to the nearest line centre.

    ``line_centers`` are the plan's ``center_y_px`` of the lines that hold text (an
    inverted caption's centres are already turned). Splitting halfway between
    neighbouring centres gives each line its ascenders, descenders and outline without
    any font metrics, and never drops a row.
    """
    centers = sorted(line_centers)
    cuts = [0] + [(a + b + 1) // 2 for a, b in zip(centers, centers[1:])] + [height]
    return list(zip(cuts, cuts[1:]))


def _caption_line_ink(overlay_png: bytes, qa_frame: Image.Image, reference_frame: Image.Image,
                      line_rows: list[tuple[int, int]], background_png: bytes | None = None,
                      source_frame: Image.Image | None = None) -> list[tuple[float, int, int]]:
    """``(share, measured pixels, word pixels)`` for each line's rows, in the order given.

    The same measure as ``_caption_ink``, kept per line so one well-drawn line cannot
    carry an invisible one through the mean (a two-line caption whose second line is
    the colour of the picture behind it). Word pixels are the overlay's opaque pixels
    that are not the box's own colour; a line with none (nothing drawn in its rows) is
    reported as ``(0.0, 0, 0)`` and judged by nothing.
    """
    field = _ink_field(overlay_png, qa_frame, reference_frame, background_png, source_frame)
    results = []
    for top, bottom in line_rows:
        if field is None:
            results.append((0.0, 0, 0))
            continue
        rows = slice(max(0, top - field.top), max(0, bottom - field.top))
        measured, words = field.measured[rows], field.words[rows]
        count = int(measured.sum())
        share = float(field.progress[rows][measured].mean()) if count else 0.0
        results.append((share, count, int(words.sum())))
    return results


def _line_ink_needs(word_pixels: int) -> int:
    """Measured pixels a line needs to count as told apart from its picture.

    CAPTION_INK_MIN_PIXELS, or half the line's own word pixels for a line so small
    (a lone short word at the fit floor) that it has fewer than twice that.
    """
    return min(CAPTION_INK_MIN_PIXELS, max(1, word_pixels // 2))


def _ink_refusal(share: float, pixels: int, box_match: float | None,
                 lines: list[tuple[float, int, int]] | None = None) -> tuple[str, str] | None:
    """The refusal (code, message) the ink measures call for, or None when the caption is shown.

    One decision for the render and the class-guard tests. A box that is not in the burned frame
    is a renderer fault whatever the words look like, so it is judged first; then words that
    cannot be told apart from what is behind them (a fact about this clip and look); then words
    that should show but did not change the frame (a renderer fault).

    ``lines`` (from ``_caption_line_ink``) applies the same two word rules to every drawn line
    as well, so a caption passes only when each line on its own is shown: a line that should
    show but did not change the frame is CAPTION_NOT_DRAWN (judged before contrast, since it is
    a renderer fault), and a line with too few measurable pixels for its size is
    CAPTION_LOW_CONTRAST even when the rest of the caption is plainly visible.
    """
    if box_match is not None and box_match < CAPTION_BOX_MIN_MATCH:
        return "CAPTION_NOT_DRAWN", "the caption's box is not visible in the burned QA frame"
    drawn = [line for line in lines or [] if line[2] > 0]
    for index, (line_share, line_pixels, word_pixels) in enumerate(drawn):
        if line_pixels >= _line_ink_needs(word_pixels) and line_share < CAPTION_INK_MIN_SHARE:
            return "CAPTION_NOT_DRAWN", f"caption line {index + 1} is not visible in the burned QA frame"
    if pixels < CAPTION_INK_MIN_PIXELS:
        # A fact about this clip and this look (the words are the colour of what is behind them), not a renderer fault.
        return "CAPTION_LOW_CONTRAST", "the caption cannot be told apart from the picture behind it"
    if share < CAPTION_INK_MIN_SHARE:
        # Contrast was expected but the burned frame did not change at the words: the renderer did not draw them.
        return "CAPTION_NOT_DRAWN", "the caption is not visible in the burned QA frame"
    for index, (_, line_pixels, word_pixels) in enumerate(drawn):
        if line_pixels < _line_ink_needs(word_pixels):
            return "CAPTION_LOW_CONTRAST", f"caption line {index + 1} cannot be told apart from the picture behind it"
    return None


def _check_caption_ink(output_directory: Path, qa_path: Path, qa_at_ms: int, overlay_bytes: bytes,
                       background_png: bytes | None, line_centers: list[int], *, reference_source: Path,
                       frame_height: int | None, normalize_pair_timing: bool, tools: RenderTools) -> dict:
    """Refuse a burned final whose QA frame does not show the caption; return the decode.json facts.

    The reference is the same frame of the clip the final was encoded from (the trimmed
    clip for a duration repair), through the same delivery graph, with the caption's
    background (box or highlight) but without its words: what the burned frame must
    differ from at the words. With no background it is the bare delivery frame. With a
    background the bare frame is cut too: it is what the burned frame looks like at the
    box if the overlay never made it into the video. Every line is judged on its own as
    well as the caption as a whole (``_ink_refusal``).
    """
    clip_probe = _probe(reference_source, tools)
    seek = ["-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
            "-noautorotate", "-ss", f"{qa_at_ms / 1000:.3f}", "-f", "mov", "-i", str(reference_source)]
    cut = ["-map", "[v]", "-frames:v", "1", "-q:v", "3", "-update", "1"]
    reference_path = output_directory / "reference.jpg"
    bare_path = output_directory / "bare.jpg" if background_png is not None else None
    background_path = output_directory / "background.png"
    try:
        if background_png is None:
            _run([tools.ffmpeg, *seek, "-filter_complex",
                  _video_graph(clip_probe, frame_height, in_app=True, normalize_pair_timing=normalize_pair_timing),
                  *cut, str(reference_path)], tools, output_file=reference_path, output_limit=MAX_QA_BYTES)
        else:
            background_path.write_bytes(background_png)
            _run([tools.ffmpeg, *seek, "-protocol_whitelist", "file,pipe", "-i", str(background_path), "-filter_complex",
                  _video_graph(clip_probe, frame_height, normalize_pair_timing=normalize_pair_timing),
                  *cut, str(reference_path)], tools, output_file=reference_path, output_limit=MAX_QA_BYTES)
            _run([tools.ffmpeg, *seek, "-filter_complex",
                  _video_graph(clip_probe, frame_height, in_app=True, normalize_pair_timing=normalize_pair_timing),
                  *cut, str(bare_path)], tools, output_file=bare_path, output_limit=MAX_QA_BYTES)
        frames = [Image.open(path) for path in (qa_path, reference_path, bare_path) if path is not None]
        try:
            for image in frames:
                image.load()
                if image.size != (1080, 1920):
                    raise PostRenderError("qa_frame_invalid", "source reference frame is not 1080x1920")
            frame, reference = frames[0], frames[1]
            bare = frames[2] if bare_path is not None else None
            share, pixels, box_match = _caption_ink(overlay_bytes, frame, reference, background_png, bare)
            lines = _caption_line_ink(overlay_bytes, frame, reference, _line_rows(line_centers), background_png, bare)
        finally:
            for image in frames:
                image.close()
    finally:
        for path in (reference_path, bare_path, background_path):
            if path is not None:
                path.unlink(missing_ok=True)
    refusal = _ink_refusal(share, pixels, box_match, lines)
    if refusal is not None:
        raise PostRenderError(*refusal)
    return {
        "caption_ink_share": round(share, 4), "caption_ink_pixels": pixels,
        "caption_ink_lines": [{"share": round(line_share, 4), "pixels": line_pixels, "word_pixels": words}
                              for line_share, line_pixels, words in lines],
        **({"caption_box_match": round(box_match, 4)} if box_match is not None else {}),
    }


def source_visual_matches(request: PostRenderRequest, *,
                          allow_pool_supplied_without_provenance: bool = False) -> bool:
    """Return whether the source has the requested visual treatment.

    Pool clips are already-finished source media and deliberately have no
    producer receipt.  The durable job runner is the only caller allowed to
    opt into that exception after it has checked both the submission and the
    closed pool-page list.
    """
    if request.source_visual_treatment_json is None:
        return allow_pool_supplied_without_provenance and is_pool_supplied(request.page_id)
    actual = normalized_visual_treatment(json.loads(request.source_visual_treatment_json))
    desired = normalized_visual_treatment(json.loads(request.render_treatment_json))
    return actual == desired


def _render_post_single(source_path: Path, output_directory: Path, request: PostRenderRequest, *,
                font_dir: Path | None = None, tools: RenderTools = RenderTools(),
                clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                burned_fallback: bool = False,
                allow_pool_supplied_without_provenance: bool = False) -> RenderedPost:
    """Render one prepared artifact; create a new output directory or leave none on failure.

    The source must already be upright vertical video with the exact requested
    visual treatment. Delivery encoding fits its full frame to 1080x1920.
    Unknown or different treatment provenance requires regeneration.
    This function adds only the typed caption, the page frame's letterbox
    (a centred band on plain black, still 1080x1920) and delivery encoding;
    it never repeats the source's grade, speed or focal crop. A bound lyric source
    receives delivery encoding only, without new caption pixels or a fallback. A slot whose
    treatment says captionDelivery "in_app" gets no caption drawn at all (the
    phone types it in TikTok); its receipt says caption_delivery "in_app".
    """
    request = PostRenderRequest.model_validate(request.model_dump(by_alias=True))
    if not source_visual_matches(
            request, allow_pool_supplied_without_provenance=allow_pool_supplied_without_provenance):
        raise PostRenderError("regeneration_required", "source treatment provenance is unknown or differs from the requested treatment")
    if request.created_at_ms > clock_ms():
        raise PostRenderError("request_future_dated", "render request is future-dated")
    lyric_source = request.source_text_mode == "lyric"
    if lyric_source and burned_fallback:
        raise PostRenderError("lyric_caption_forbidden", "lyric source cannot have a burned fallback")
    paired_caption = _caption_delivery(request) == CAPTION_DELIVERY_IN_APP
    in_app = paired_caption and not burned_fallback
    no_overlay = in_app or lyric_source
    if in_app and _caption_request(request).style.inverted:
        raise PostRenderError("in_app_inverted_unsupported", "in_app caption delivery cannot be inverted")
    source_path, output_directory = Path(source_path).absolute(), Path(output_directory).absolute()
    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        source = output_directory / "source.mp4"
        _copy_verified_source(source_path, source, request.source_sha256)
        source_probe = _probe(source, tools)
        # Landscape 768p/1080p masters produce 432x768/606x1080 9:16 crops
        # after even-pixel alignment. Preserve that selected frame: delivery
        # scaling is not another creative crop, grade, or speed treatment.
        # Provider H.264 crops may omit SAR entirely. Keep their coded frame
        # and declare square pixels at encode; explicit non-square SAR refuses.
        target_width = source_probe.height * 9 / 16
        near_vertical = abs(source_probe.width - target_width) <= target_width * 0.03
        if (source_probe.height < 768 or source_probe.sample_aspect_ratio not in {"1:1", "", "N/A", "0:1"}
                or source_probe.rotation != 0 or not near_vertical):
            raise PostRenderError("regeneration_required", "source must be upright square-pixel near-9:16 video at least 768 pixels high")
        caption_request = None if lyric_source else _caption_request(request)
        band_height = _frame_band_height(request)
        if no_overlay:
            # The caption stays bound (caption_sha256 is required and validated
            # on the request) but is not drawn: the ffmpeg graph has no [1:v]
            # overlay input and no caption-render evidence is produced.
            overlay_path = None
        else:
            # A caption shrinks only if it would leave the visible picture: the
            # framed page's band, or the whole canvas when full-screen (owner
            # rule, 2026-10-04). The gate checks the same area.
            band_rows = frame_band_rows(band_height) if band_height is not None else None
            overlay, background_png = render_caption_with_background(
                caption_request, font_dir=font_dir or Path(__file__).parents[1] / "fonts", fit_rows=band_rows)
            overlay_bytes = base64.b64decode(overlay.overlay.base64, validate=True)
            if (not 0 < len(overlay_bytes) <= MAX_OVERLAY_BYTES
                    or sha256(overlay_bytes) != overlay.overlay.sha256.removeprefix("sha256:")
                    or overlay.caption_sha256 != "sha256:" + request.caption_sha256):
                raise PostRenderError("caption_evidence_mismatch", "typed caption renderer evidence does not match requested bytes")
            if overlay_geometry_reasons(overlay.overlay.base64, caption_request.style.model_dump(exclude_none=True),
                                        band_rows=band_rows):
                raise PostRenderError("caption_geometry_invalid", "typed caption overlay failed the existing geometry check")
            overlay_path = output_directory / "overlay.png"
            overlay_path.write_bytes(overlay_bytes)
        final_path = output_directory / "final.mp4"
        repair = request.source_duration_repair
        if repair is not None:
            from services.source_duration_repair import render_shorter
            # The adapter-owned encoder owns the trim + delivery encode (real
            # `_encode_final` path); it re-verifies the source hash and the
            # near-9:16 contract itself, then writes the shorter final.
            repair_dir = output_directory / "repair"
            render_shorter(source_path, repair_dir, repair,
                           overlay_path=overlay_path, frame_height=band_height,
                           normalize_pair_timing=paired_caption)
            shutil.move(str(repair_dir / "final.mp4"), final_path)
            final_probe = _probe(final_path, tools)
            if (final_probe.width, final_probe.height, final_probe.video_codec, final_probe.pixel_format,
                final_probe.sample_aspect_ratio, final_probe.rotation) != (1080, 1920, "h264", "yuv420p", "1:1", 0):
                raise PostRenderError("final_probe_mismatch", "encoded final media facts do not match delivery contract")
        else:
            final_probe = _encode_final(source, overlay_path, final_path, source_probe, tools, band_height,
                                        in_app=no_overlay, normalize_pair_timing=paired_caption)
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
        ink = None
        if not no_overlay:
            # The delivered pixels must show the words, not only the receipt.
            ink = _check_caption_ink(
                output_directory, qa_path, qa_at_ms, overlay_bytes, background_png,
                [record.center_y_px for record in overlay.plan.lines if record.text],
                reference_source=(output_directory / "repair" / "trimmed.mp4") if repair is not None else source,
                frame_height=band_height, normalize_pair_timing=paired_caption, tools=tools)
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
        if in_app:
            receipt["caption_delivery"] = "in_app"
        if repair is not None:
            from services.source_duration_repair import command_digest
            receipt["source_duration_repair_sha256"] = command_digest(repair)
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        receipt_path = output_directory / "receipt.json"
        receipt_path.write_bytes(receipt_json)
        if not no_overlay:
            (output_directory / "caption-render.json").write_text(overlay.model_dump_json(by_alias=True))
        (output_directory / "request.json").write_text(request.model_dump_json(by_alias=True))
        (output_directory / "decode.json").write_text(json.dumps({
            "source_probe": source_probe.__dict__, "final_probe": final_probe.__dict__,
            "decoded_video_frames": frames[-1], "qa_at_ms": qa_at_ms,
            "final_sha256": final_sha, "qa_frame_sha256": qa_sha,
            **(ink or {}),
        }, sort_keys=True))
        source.unlink()
        return RenderedPost(final_path, qa_path, receipt_path, receipt_json, sha256(receipt_json),
                            source_probe, final_probe, frames[-1], qa_at_ms)
    except BaseException:
        shutil.rmtree(output_directory)
        raise


def render_post(source_path: Path, output_directory: Path, request: PostRenderRequest, *,
                font_dir: Path | None = None, tools: RenderTools = RenderTools(),
                clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                allow_pool_supplied_without_provenance: bool = False) -> RenderedPost:
    """Publish both clean and captioned assets with job-level verification for an in-app slot.

    Both variants use the identical source, caption and requested treatment.
    A failed fallback invalidates the entire attempt; the durable job retry
    handles transient errors before either asset can become ready.
    """
    result = _render_post_single(source_path, output_directory, request,
                                 font_dir=font_dir, tools=tools, clock_ms=clock_ms,
                                 allow_pool_supplied_without_provenance=allow_pool_supplied_without_provenance)
    if _caption_delivery(request) != CAPTION_DELIVERY_IN_APP:
        return result
    try:
        fallback = _render_post_single(source_path, Path(output_directory) / "burned", request,
                                      font_dir=font_dir, tools=tools, clock_ms=clock_ms,
                                      burned_fallback=True,
                                      allow_pool_supplied_without_provenance=allow_pool_supplied_without_provenance)
        receipt = json.loads(result.receipt_json)
        burned_receipt = json.loads(fallback.receipt_json)
        receipt["burned_fallback"] = {key: burned_receipt[key] for key in (
            "final_sha256", "final_byte_length", "final_object_key", "qa_frame_sha256")}
        receipt["burned_fallback"]["final_object_key"] = f"posting/dual-final/{burned_receipt['final_sha256']}.mp4"
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        result.receipt_path.write_bytes(encoded)
        return RenderedPost(result.final_path, result.qa_frame_path, result.receipt_path,
                            encoded, sha256(encoded), result.source_probe, result.final_probe,
                            result.decoded_video_frames, result.qa_at_ms)
    except BaseException:
        shutil.rmtree(output_directory)
        raise
