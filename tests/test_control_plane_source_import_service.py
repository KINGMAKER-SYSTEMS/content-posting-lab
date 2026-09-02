"""Network and exact-byte boundaries for page source-link imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import socket

import pytest

import services.control_plane_source_imports as imports


def _public_answers(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _private_answers(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


def test_source_url_must_be_https_without_private_network_resolution():
    url = "https://cdn.example.com/video.mp4?token=exact"
    assert imports.validate_source_url(url, resolver=_public_answers) == url
    for invalid in (
        "http://cdn.example.com/video.mp4",
        "https://user:pass@cdn.example.com/video.mp4",
        "https://localhost/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://cdn.example.com/video.mp4#fragment",
    ):
        with pytest.raises(imports.SourceImportError):
            imports.validate_source_url(invalid, resolver=_public_answers)
    with pytest.raises(imports.SourceImportError, match="public addresses"):
        imports.validate_source_url(
            "https://cdn.example.com/video.mp4", resolver=_private_answers,
        )


def test_probe_returns_closed_mp4_media_facts(monkeypatch, tmp_path):
    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "width": 1080,
                        "height": 1920,
                        "avg_frame_rate": "30/1",
                    },
                    {"codec_type": "audio"},
                ],
                "format": {
                    "duration": "6.025",
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                },
            }).encode(), b""

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    media = asyncio.run(imports._probe_video(tmp_path / "source.mp4"))
    assert media.wire() == {
        "durationMs": 6025,
        "width": 1080,
        "height": 1920,
        "videoCodec": "h264",
        "pixelFormat": "yuv420p",
        "fps": 30.0,
        "audioStreams": 1,
    }


def test_normalization_is_center_crop_muted_h264_30fps(monkeypatch, tmp_path):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create(*command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(b"normalized")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")
    destination = tmp_path / "source.mp4"
    asyncio.run(imports._normalize_video(source, destination))
    command = calls[0]
    assert "-an" in command
    filter_graph = command[command.index("-vf") + 1]
    assert "force_original_aspect_ratio=increase" in filter_graph
    assert "crop=1080:1920" in filter_graph
    assert "fps=30" in filter_graph
    assert command[command.index("-c:v") + 1] == "libx264"


def test_download_source_video_reuses_clipper_and_hashes_exact_bytes(monkeypatch, tmp_path):
    original = b"downloaded-video"
    normalized = b"normalized-vertical-video"
    calls = []

    async def downloader(url, destination, cookies_file=None, *, max_filesize=None):
        calls.append((url, destination, cookies_file, max_filesize))
        destination.write_bytes(original)
        return destination

    original_media = imports.SourceImportMedia(
        duration_ms=6_000,
        width=1920,
        height=1080,
        video_codec="vp9",
        pixel_format="yuv420p",
        fps=60.0,
        audio_streams=1,
    )
    normalized_media = imports.SourceImportMedia(
        duration_ms=6_000,
        width=1080,
        height=1920,
        video_codec="h264",
        pixel_format="yuv420p",
        fps=30.0,
        audio_streams=0,
    )

    async def probe(path):
        return original_media if path.name == "original.mp4" else normalized_media

    async def normalize(source, destination):
        assert source.read_bytes() == original
        destination.write_bytes(normalized)

    monkeypatch.setattr(imports, "download_video", downloader)
    monkeypatch.setattr(imports, "_probe_video", probe)
    monkeypatch.setattr(imports, "_normalize_video", normalize)
    destination = tmp_path / "source" / "source.mp4"
    result = asyncio.run(imports.download_source_video(
        "https://cdn.example.com/video.mp4", destination,
    ))
    assert calls == [(
        "https://cdn.example.com/video.mp4",
        destination.with_name("original.mp4"),
        None,
        imports.MAX_SOURCE_IMPORT_BYTES,
    )]
    assert result.path == destination.resolve()
    assert result.sha256 == hashlib.sha256(normalized).hexdigest()
    assert result.bytes == len(normalized)
    assert result.media == normalized_media
    assert result.original_sha256 == hashlib.sha256(original).hexdigest()
    assert result.original_bytes == len(original)
    assert result.original_media == original_media
    assert not destination.with_name("original.mp4").exists()


def test_download_source_video_rejects_post_download_size_drift(monkeypatch, tmp_path):
    monkeypatch.setattr(imports, "MAX_SOURCE_IMPORT_BYTES", 4)

    async def downloader(_url, destination, cookies_file=None, *, max_filesize=None):
        destination.write_bytes(b"12345")
        return destination

    monkeypatch.setattr(imports, "download_video", downloader)
    with pytest.raises(imports.SourceImportError, match="size limit"):
        asyncio.run(imports.download_source_video(
            "https://cdn.example.com/video.mp4", tmp_path / "source.mp4",
        ))
