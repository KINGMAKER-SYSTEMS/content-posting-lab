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
import shutil
import socket
from typing import Any, Callable
from urllib.parse import unquote_plus, urlparse, urlunparse

from scraper.frame_extractor import download_video
from services.ffmpeg import delivery_encode_args


SOURCE_IMPORT_SCHEMA = "content-lab.source-import-request.v1"
SOURCE_PROVENANCE_SCHEMA = "content-lab.page-source-import-source.v1"
MAX_SOURCE_URL_CHARS = 4_096
SOURCE_IMPORT_HOSTS_ENV = "CONTENT_LAB_SOURCE_IMPORT_HOSTS"
MAX_SOURCE_IMPORT_BYTES = 1_000_000_000
MAX_SOURCE_IMPORT_WORKSPACE_BYTES = 2_000_000_000
MAX_NORMALIZED_SOURCE_BYTES = 2_000_000_000
MIN_SOURCE_IMPORT_FREE_BYTES = 512_000_000
MAX_SOURCE_IMPORT_SECONDS = 300
MAX_SOURCE_NORMALIZE_SECONDS = 600
MAX_SOURCE_DURATION_MS = 720_000
MAX_NORMALIZED_BITRATE_BPS = 20_000_000
MAX_CONCURRENT_SOURCE_IMPORTS = 2
SOURCE_IMPORT_POLL_SECONDS = 0.25
_TRACKING_QUERY_FIELDS = {"fbclid", "gclid", "msclkid"}
_CREDENTIAL_QUERY_FIELDS = {
    "token", "key", "api_key", "apikey", "secret", "signature", "sig",
    "credential", "authorization", "auth", "expires", "policy",
}
_SOURCE_IMPORT_GATE = asyncio.Semaphore(MAX_CONCURRENT_SOURCE_IMPORTS)


class SourceImportError(ValueError):
    pass


class SourceImportUnavailable(RuntimeError):
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


def _canonical_hostname(value: str) -> str:
    try:
        hostname = value.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise SourceImportUnavailable("source import host allowlist is invalid") from error
    if (
        not hostname
        or len(hostname) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (char.isalnum() or char == "-") for char in label)
            for label in hostname.split(".")
        )
    ):
        raise SourceImportUnavailable("source import host allowlist is invalid")
    return hostname


def source_import_hosts(value: str | None = None) -> tuple[str, ...]:
    raw = os.environ.get(SOURCE_IMPORT_HOSTS_ENV, "") if value is None else value
    if not isinstance(raw, str) or not raw.strip():
        raise SourceImportUnavailable("source import host allowlist is unavailable")
    hosts: list[str] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if (
            not candidate
            or "://" in candidate
            or "/" in candidate
            or ":" in candidate
            or candidate.startswith(".")
        ):
            raise SourceImportUnavailable("source import host allowlist is invalid")
        host = _canonical_hostname(candidate)
        if "." not in host:
            raise SourceImportUnavailable("source import host allowlist is invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SourceImportUnavailable("source import host allowlist is invalid")
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        raise SourceImportUnavailable("source import host allowlist is unavailable")
    return tuple(hosts)


def _host_allowed(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(
        hostname == allowed or hostname.endswith(f".{allowed}")
        for allowed in allowed_hosts
    )


def _canonical_query(value: str) -> str:
    kept: list[str] = []
    for field in value.split("&") if value else ():
        try:
            name = unquote_plus(field.partition("=")[0])
        except UnicodeDecodeError as error:
            raise SourceImportError("sourceUrl query is invalid") from error
        lowered = name.casefold()
        if (
            lowered in _CREDENTIAL_QUERY_FIELDS
            or lowered.startswith("x-amz-")
            or lowered.startswith("x-goog-")
        ):
            raise SourceImportError(
                "sourceUrl must be a permanent link without credential query parameters"
            )
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_FIELDS:
            continue
        # Preserve retained parameter bytes exactly. The control-plane caller
        # has already canonicalized them with URLSearchParams, and changing
        # encoding here would break exact cross-service sourceUrl equality.
        kept.append(field)
    return "&".join(kept)


def validate_source_url(
    value: Any,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    allowed_hosts: tuple[str, ...] | None = None,
) -> str:
    """Return one public HTTPS URL or reject it before yt-dlp sees it."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > MAX_SOURCE_URL_CHARS
        or any(char.isspace() or ord(char) < 32 for char in value)
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
        canonical_hostname = _canonical_hostname(hostname)
    except SourceImportUnavailable as error:
        raise SourceImportError("sourceUrl must be a public HTTPS URL") from error
    configured_hosts = source_import_hosts() if allowed_hosts is None else allowed_hosts
    if not configured_hosts or not _host_allowed(canonical_hostname, configured_hosts):
        raise SourceImportError("sourceUrl host is not allowed for source import")
    try:
        literal = ipaddress.ip_address(canonical_hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise SourceImportError("sourceUrl must resolve only to public addresses")
        return value
    try:
        answers = resolver(canonical_hostname, 443, type=socket.SOCK_STREAM)
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
    canonical_query = _canonical_query(parsed.query)
    canonical_path = parsed.path or "/"
    return urlunparse((
        "https",
        canonical_hostname,
        canonical_path,
        parsed.params,
        canonical_query,
        "",
    ))


def _tree_bytes(root: Path) -> int:
    total = 0
    try:
        paths = root.iterdir()
    except FileNotFoundError:
        return 0
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def _require_free_space(path: Path, required: int) -> None:
    if shutil.disk_usage(path).free < required:
        raise SourceImportUnavailable("source import storage capacity is unavailable")


def _expected_normalized_bytes(duration_ms: int) -> int:
    expected = math.ceil(
        (duration_ms / 1_000) * (MAX_NORMALIZED_BITRATE_BPS / 8) * 1.05
    )
    if expected > MAX_NORMALIZED_SOURCE_BYTES:
        raise SourceImportError("source video duration exceeds the normalized output limit")
    return max(1, expected)


async def _download_with_workspace_limit(source_url: str, target: Path) -> Path:
    task = asyncio.create_task(download_video(
        source_url,
        target,
        max_filesize=MAX_SOURCE_IMPORT_BYTES,
        source_import_mode=True,
    ))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_SOURCE_IMPORT_SECONDS
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SourceImportError("source video download timed out")
            done, _ = await asyncio.wait(
                {task}, timeout=min(SOURCE_IMPORT_POLL_SECONDS, remaining),
            )
            if task in done:
                return task.result()
            if _tree_bytes(target.parent) > MAX_SOURCE_IMPORT_WORKSPACE_BYTES:
                raise SourceImportError("source video exceeds the import workspace limit")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


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
        "-fs", str(MAX_NORMALIZED_SOURCE_BYTES),
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
        _require_free_space(
            destination.parent,
            MAX_SOURCE_IMPORT_WORKSPACE_BYTES + MIN_SOURCE_IMPORT_FREE_BYTES,
        )
        downloaded = Path(await _download_with_workspace_limit(
            source_url, original_target,
        )).resolve()
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
        expected_output_bytes = _expected_normalized_bytes(original_media.duration_ms)
        _require_free_space(
            destination.parent,
            expected_output_bytes + MIN_SOURCE_IMPORT_FREE_BYTES,
        )
        original_sha256 = await asyncio.to_thread(_sha256_file, original)

        exact = destination.resolve()
        await _normalize_video(original, exact)
        byte_count = exact.stat().st_size
        if not 1 <= byte_count <= MAX_NORMALIZED_SOURCE_BYTES:
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
    except asyncio.CancelledError:
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise


def source_import_slot() -> asyncio.Semaphore:
    return _SOURCE_IMPORT_GATE
