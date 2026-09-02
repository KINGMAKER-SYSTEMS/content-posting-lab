"""Bounded URL intake for one page-scoped source-video artifact.

The control-plane router owns page identity, idempotency, and durable job state.
This module owns only the untrusted network/media boundary: require a public
HTTPS source, reuse Clipper's yt-dlp downloader with hard time/size ceilings,
then probe and hash the exact bytes that the artifact endpoint will serve.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
from typing import Any, Callable
from urllib.parse import urlparse

from scraper.frame_extractor import download_video
from services.ffmpeg import delivery_encode_args


SOURCE_IMPORT_SCHEMA = "content-lab.source-import-request.v1"
SOURCE_PROVENANCE_SCHEMA = "content-lab.page-source-import-source.v1"
MAX_SOURCE_URL_CHARS = 4_096
MAX_SOURCE_IMPORT_BYTES = 5_000_000_000
MAX_SOURCE_IMPORT_SECONDS = 300
MAX_SOURCE_NORMALIZE_SECONDS = 600
MAX_SOURCE_DURATION_MS = 86_400_000


class SourceImportError(ValueError):
    pass


@dataclass(frozen=True)
class SourceImportMedia:
    duration_ms: int
    width: int
    height: int
    video_codec: str
    pixel_format: str
    fps: float
    audio_streams: int

    def wire(self) -> dict[str, Any]:
        return {
            "durationMs": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "videoCodec": self.video_codec,
            "pixelFormat": self.pixel_format,
            "fps": self.fps,
            "audioStreams": self.audio_streams,
        }


@dataclass(frozen=True)
class SourceImportArtifact:
    path: Path
    sha256: str
    bytes: int
    media: SourceImportMedia
    original_sha256: str
    original_bytes: int
    original_media: SourceImportMedia


def _public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def validate_source_url(
    value: Any,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    """Return one public HTTPS URL or reject it before yt-dlp sees it."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > MAX_SOURCE_URL_CHARS
        or any(ord(char) < 32 for char in value)
    ):
        raise SourceImportError("sourceUrl must be a bounded HTTPS URL")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise SourceImportError("sourceUrl must be a bounded HTTPS URL") from error
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith((".localhost", ".local", ".internal"))
    ):
        raise SourceImportError("sourceUrl must be a public HTTPS URL")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise SourceImportError("sourceUrl must resolve only to public addresses")
        return value
    try:
        answers = resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise SourceImportError("sourceUrl host could not be resolved") from error
    addresses = {
        str(answer[4][0])
        for answer in answers
        if isinstance(answer, tuple)
        and len(answer) >= 5
        and isinstance(answer[4], tuple)
        and answer[4]
    }
    if not addresses or any(not _public_address(address) for address in addresses):
        raise SourceImportError("sourceUrl must resolve only to public addresses")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _probe_video(path: Path) -> SourceImportMedia:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError as error:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise SourceImportError("source video probe timed out") from error
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[-160:].strip()
        raise SourceImportError(f"source video probe failed: {detail}")
    try:
        document = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceImportError("source video probe returned invalid JSON") from error
    streams = document.get("streams") if isinstance(document, dict) else None
    if not isinstance(streams, list):
        raise SourceImportError("source video has no readable streams")
    videos = [row for row in streams if isinstance(row, dict) and row.get("codec_type") == "video"]
    audios = [row for row in streams if isinstance(row, dict) and row.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise SourceImportError("source video must contain exactly one video stream")
    video = videos[0]
    format_row = document.get("format")
    duration = format_row.get("duration") if isinstance(format_row, dict) else None
    format_names = (
        str(format_row.get("format_name") or "").split(",")
        if isinstance(format_row, dict) else []
    )
    try:
        duration_ms = round(float(duration) * 1_000)
        width = int(video.get("width"))
        height = int(video.get("height"))
        numerator, denominator = str(
            video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        ).split("/", 1)
        fps = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise SourceImportError("source video media facts are invalid") from error
    video_codec = video.get("codec_name")
    pixel_format = video.get("pix_fmt")
    if (
        not 1 <= duration_ms <= MAX_SOURCE_DURATION_MS
        or "mp4" not in format_names
        or not 1 <= width <= 32_768
        or not 1 <= height <= 32_768
        or not isinstance(video_codec, str)
        or not video_codec
        or not isinstance(pixel_format, str)
        or not pixel_format
        or not math.isfinite(fps)
        or not 0 < fps <= 240
    ):
        raise SourceImportError("source video media facts are invalid")
    return SourceImportMedia(
        duration_ms=duration_ms,
        width=width,
        height=height,
        video_codec=video_codec,
        pixel_format=pixel_format,
        fps=fps,
        audio_streams=len(audios),
    )


async def _normalize_video(source: Path, destination: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-an", "-vf",
        (
            "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
            "crop=1080:1920,setsar=1,fps=30,format=yuv420p"
        ),
        *delivery_encode_args("tiktok_delivery_v1"),
        str(destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=MAX_SOURCE_NORMALIZE_SECONDS,
        )
    except TimeoutError as error:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise SourceImportError("source video normalization timed out") from error
    if (
        process.returncode != 0
        or not destination.is_file()
        or destination.stat().st_size <= 0
    ):
        detail = stderr.decode(errors="replace")[-160:].strip()
        raise SourceImportError(f"source video normalization failed: {detail}")


async def download_source_video(source_url: str, destination: Path) -> SourceImportArtifact:
    """Download one URL and return one normalized refillable master artifact."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    original_target = destination.with_name("original.mp4")
    try:
        downloaded = await asyncio.wait_for(
            download_video(
                source_url,
                original_target,
                max_filesize=MAX_SOURCE_IMPORT_BYTES,
            ),
            timeout=MAX_SOURCE_IMPORT_SECONDS,
        )
    except TimeoutError as error:
        raise SourceImportError("source video download timed out") from error
    downloaded = Path(downloaded).resolve()
    root = destination.parent.resolve()
    if root not in downloaded.parents or not downloaded.is_file() or downloaded.is_symlink():
        raise SourceImportError("source downloader returned an invalid artifact")
    original = original_target.resolve()
    if downloaded != original:
        if original.exists():
            original.unlink()
        os.replace(downloaded, original)
    original_bytes = original.stat().st_size
    if not 1 <= original_bytes <= MAX_SOURCE_IMPORT_BYTES:
        raise SourceImportError("source video exceeds the import size limit")
    original_media = await _probe_video(original)
    original_sha256 = await asyncio.to_thread(_sha256_file, original)

    exact = destination.resolve()
    await _normalize_video(original, exact)
    byte_count = exact.stat().st_size
    if not 1 <= byte_count <= MAX_SOURCE_IMPORT_BYTES:
        raise SourceImportError("normalized source video exceeds the import size limit")
    media = await _probe_video(exact)
    if (
        media.width != 1080
        or media.height != 1920
        or media.video_codec != "h264"
        or media.pixel_format != "yuv420p"
        or abs(media.fps - 30.0) > 0.01
        or media.audio_streams != 0
        or abs(media.duration_ms - original_media.duration_ms) > 1_000
    ):
        raise SourceImportError("normalized source video does not match the refillable master contract")
    sha256 = await asyncio.to_thread(_sha256_file, exact)
    original.unlink(missing_ok=True)
    return SourceImportArtifact(
        exact,
        sha256,
        byte_count,
        media,
        original_sha256,
        original_bytes,
        original_media,
    )
