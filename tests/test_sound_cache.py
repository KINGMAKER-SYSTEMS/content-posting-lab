"""Tests for services/sound_cache.py — beats sidecar crash-safety.

Regression for the P0 where prepare_sound() wrote beats.json with a bare
write_text(). A crash mid-write left a truncated file, and the next
prepare_sound() call's cache-hit path (json.loads of the sidecar) then blew up.
The save now goes through services.json_store.atomic_save (tmp + fsync + rename),
so a reader never sees a partial file.
"""

import json

import pytest

import services.sound_cache as sc


@pytest.fixture
def stub_pipeline(tmp_path, monkeypatch):
    """Stub the heavy externals so prepare_sound() reaches its save path."""
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    monkeypatch.setattr(sc, "get_global_sounds_dir", lambda: sounds_dir)

    async def fake_find(label):
        return {"slug": "bella-kay"}

    async def fake_detail(slug):
        return {
            "sound_id": "7123456789",
            "matched_videos": [{"url": "https://tiktok.com/x", "views": 99}],
        }

    monkeypatch.setattr(sc, "find_campaign_by_label", fake_find)
    monkeypatch.setattr(sc, "fetch_campaign_detail", fake_detail)

    # Pretend yt-dlp/ffmpeg ran: create the mp3 so the cache-hit check passes.
    def fake_download(video_url, output_mp3):
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        output_mp3.write_bytes(b"\x00")

    monkeypatch.setattr(sc, "_download_and_extract", fake_download)
    monkeypatch.setattr(
        sc, "_analyze_beats",
        lambda audio_path: {"duration": 12.5, "bpm": 120.0, "beats": [0.0, 0.5, 1.0]},
    )
    # Clear any per-sound lock leaked from a prior test run.
    sc._prepare_locks.clear()
    return sounds_dir


async def test_beats_sidecar_written_atomically(stub_pipeline):
    sounds_dir = stub_pipeline
    result = await sc.prepare_sound("deadbeefcafe", "Bella Kay - Wonder Wander")

    beats_path = sounds_dir / "7123456789.beats.json"
    assert beats_path.exists()
    # No leftover tmp file — atomic_save renames tmp → final.
    assert not (sounds_dir / "7123456789.beats.json.tmp").exists()
    # File on disk is complete, parseable JSON (not truncated).
    on_disk = json.loads(beats_path.read_text())
    assert on_disk["bpm"] == 120.0
    assert on_disk["beats"] == [0.0, 0.5, 1.0]
    assert on_disk["label"] == "Bella Kay - Wonder Wander"
    assert result["cached"] is False
    assert result["sound_id"] == "7123456789"


async def test_cache_hit_reads_back_sidecar(stub_pipeline):
    """Second call must hit cache and json.loads the sidecar cleanly."""
    sc._prepare_locks.clear()
    await sc.prepare_sound("deadbeefcafe", "Bella Kay - Wonder Wander")
    sc._prepare_locks.clear()
    again = await sc.prepare_sound("deadbeefcafe", "Bella Kay - Wonder Wander")
    assert again["cached"] is True
    assert again["bpm"] == 120.0
    assert again["beats"] == [0.0, 0.5, 1.0]


async def test_uses_atomic_save_not_bare_write(stub_pipeline, monkeypatch):
    """Guard the fix: the save path must route through json_store.atomic_save."""
    calls = []
    real_atomic = sc.atomic_save

    def spy(path, data, **kw):
        calls.append(path)
        return real_atomic(path, data, **kw)

    monkeypatch.setattr(sc, "atomic_save", spy)
    await sc.prepare_sound("deadbeefcafe", "Bella Kay - Wonder Wander")
    assert any(str(p).endswith(".beats.json") for p in calls)
