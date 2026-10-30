"""An invalid timeout must not remove the total provider-download deadline."""
from types import SimpleNamespace

import pytest

from providers import base


@pytest.mark.parametrize("raw", ["inf", "+Infinity", "1e999", "nan", "-inf", "0", "-1", "bad", ""])
def test_invalid_deadline_keeps_finite_default(monkeypatch, raw):
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", raw)
    assert base._provider_download_deadline() == base._DEFAULT_PROVIDER_DOWNLOAD_SECONDS


@pytest.mark.parametrize("raw,expected", [("0.05", 0.05), (" 120 ", 120.0), ("1e3", 1000.0)])
def test_finite_positive_deadline_is_preserved(monkeypatch, raw, expected):
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", raw)
    assert base._provider_download_deadline() == expected


@pytest.mark.asyncio
async def test_infinite_setting_still_stops_late_chunk_and_removes_partial(tmp_path, monkeypatch):
    class Stream:
        headers = {"content-type": "video/mp4"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, size):
            yield b"late bytes"

    class Client:
        def stream(self, *args, **kwargs):
            return Stream()

    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", "inf")
    clock = iter([0.0, base._DEFAULT_PROVIDER_DOWNLOAD_SECONDS + 1.0])
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    dest = tmp_path / "artifact.mp4"
    with pytest.raises(RuntimeError, match="provider_artifact_download_timeout"):
        await base.download_media(Client(), "https://unused.invalid/artifact", dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()
