"""Closed contracts for bounded provider artifact downloads (lab #6)."""

import asyncio
from pathlib import Path

import httpx
import pytest

from providers import base


class _FakeStream:
    def __init__(self, chunks, headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code != 200:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    async def aiter_bytes(self, chunk_size):
        if hasattr(self._chunks, "__aiter__"):
            async for chunk in self._chunks:
                yield chunk
        else:
            for chunk in self._chunks:
                yield chunk


class _FakeClient:
    def __init__(self, stream):
        self._stream = stream

    def stream(self, method, url, **kwargs):
        return self._stream


def _run(chunks, headers=None):
    async def go(dest):
        await base.download_media(_FakeClient(_FakeStream(chunks, headers)), "https://x.test/a", dest)
    return go


@pytest.mark.asyncio
async def test_download_writes_atomic_bytes_and_leaves_no_partial(tmp_path):
    dest = tmp_path / "out.mp4"
    await base.download_media(
        _FakeClient(_FakeStream([b"hel", b"lo"], {"content-length": "5", "content-type": "video/mp4"})),
        "https://x.test/a", dest,
    )
    assert dest.read_bytes() == b"hello"
    assert not dest.with_name("out.mp4.part").exists()


@pytest.mark.asyncio
async def test_download_rejects_declared_oversize_before_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_BYTES", "100")
    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="provider_artifact_too_large"):
        await base.download_media(
            _FakeClient(_FakeStream([b"x"], {"content-length": "200"})),
            "https://x.test/a", dest,
        )
    assert not dest.exists()
    assert not dest.with_name("out.mp4.part").exists()


@pytest.mark.asyncio
async def test_download_bounds_observed_bytes_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_BYTES", "100")
    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="provider_artifact_too_large"):
        await base.download_media(
            _FakeClient(_FakeStream([b"x" * 50, b"y" * 50, b"z" * 50])),
            "https://x.test/a", dest,
        )
    assert not dest.exists()
    assert not dest.with_name("out.mp4.part").exists()


@pytest.mark.asyncio
async def test_download_enforces_monotonic_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", "0.05")

    async def drip():
        for _ in range(100):
            await asyncio.sleep(0.02)
            yield b"x" * 10

    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="provider_artifact_download_timeout"):
        await base.download_media(_FakeClient(_FakeStream(drip())), "https://x.test/a", dest)
    assert not dest.exists()
    assert not dest.with_name("out.mp4.part").exists()


@pytest.mark.asyncio
async def test_download_rejects_non_media_content_type(tmp_path):
    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="provider_artifact_not_media"):
        await base.download_media(
            _FakeClient(_FakeStream([b"<html>"], {"content-type": "text/html"})),
            "https://x.test/a", dest,
        )
    assert not dest.exists()
    assert not dest.with_name("out.mp4.part").exists()
