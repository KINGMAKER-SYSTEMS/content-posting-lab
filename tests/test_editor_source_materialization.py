"""Unit tests for the editor-bay flattened-source materialization fallback.

`routers/agenticnews.py` lines ~672-791 implement the path that reconstructs
per-segment audio / video / image source assets from the single flattened
episode render when the discrete per-segment files are missing (the case when
importing an ABN project whose only durable artifact is `episode.mp4`).

These tests pin the **planning** logic (`materialize=False`, used by the
asset-health endpoint) and the **dispatch** logic (`materialize=True`) WITHOUT
shelling out to ffmpeg: the two `_extract_*_window` helpers are stubbed so we
assert on the windows they're called with, not on real encodes. The cheap
`_src.png` -> `_card.png` copy path is exercised for real (pure shutil.copyfile).

The episode id used here has no `ep_` prefix on purpose, so
`abn_assets.episode_singleton_path` returns None and the documented flat
`{episode_id}_episode.mp4` fallback is the path under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import services.agenticnews as db
import services.abn_assets as abn_assets
import routers.agenticnews as r


@pytest.fixture
def assets_dir(tmp_path, monkeypatch):
    """Repoint every captured ASSETS_DIR at a temp dir so nothing touches the volume."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(db, "ASSETS_DIR", assets)
    # abn_assets and the router both captured ASSETS_DIR by value at import time.
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return assets


@pytest.fixture
def captured_extracts(monkeypatch):
    """Replace the ffmpeg shell-outs with spies; return (audio_calls, video_calls)."""
    audio_calls: list[dict] = []
    video_calls: list[dict] = []

    def fake_audio(source, target, *, start, duration):
        audio_calls.append(
            {"source": Path(source), "target": Path(target), "start": start, "duration": duration}
        )
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"AUDIO")

    def fake_video(source, target, *, start, duration):
        video_calls.append(
            {"source": Path(source), "target": Path(target), "start": start, "duration": duration}
        )
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"VIDEO")

    monkeypatch.setattr(r, "_extract_audio_window", fake_audio)
    monkeypatch.setattr(r, "_extract_video_window", fake_video)
    return audio_calls, video_calls


def _url(rel: str) -> str:
    return f"/agenticnews-assets/{rel}"


def _write_episode(assets_dir: Path, episode_id: str) -> Path:
    video = assets_dir / f"{episode_id}_episode.mp4"
    video.write_bytes(b"EPISODE")
    return video


# Two segments: each wants a missing VO audio file and a missing _demo.mp4 video,
# plus a _src.png image whose _card.png sibling already exists (copy path).
def _two_segment_timeline() -> dict:
    return {
        "segments": [
            {
                "durationSec": 10.0,
                "audio": {"vo": {"src": _url("s0_vo.wav")}},
                "shots": [
                    {"src": _url("s0_card_src.png"), "startSec": 0, "durationSec": 4},
                    {
                        "src": _url("s0_demo.mp4"),
                        "startSec": 2,
                        "clipStartSec": 1,
                        "durationSec": 6,
                    },
                ],
            },
            {
                "durationSec": 8.0,
                "audio": {"vo": {"src": _url("s1_vo.wav")}},
                "shots": [
                    {
                        "src": _url("s1_demo.mp4"),
                        "startSec": 0,
                        "clipStartSec": 0,
                        "durationSec": 8,
                    },
                ],
            },
        ]
    }


# ---------------------------------------------------------------- guard clauses

def test_no_episode_render_returns_empty_plan(assets_dir):
    """No flattened episode on disk -> nothing can be derived -> empty list."""
    plan = r._plan_editor_source_materialization("vid_missing", _two_segment_timeline())
    assert plan == []


def test_zero_duration_segments_are_skipped(assets_dir):
    _write_episode(assets_dir, "vid1")
    timeline = {"segments": [{"durationSec": 0, "audio": {"vo": {"src": _url("x.wav")}}}]}
    assert r._plan_editor_source_materialization("vid1", timeline) == []


def test_existing_targets_are_not_replanned(assets_dir):
    _write_episode(assets_dir, "vid1")
    # Pre-create the VO file so it should be left alone.
    (assets_dir / "s0_vo.wav").write_bytes(b"EXISTING")
    timeline = {
        "segments": [
            {"durationSec": 5.0, "audio": {"vo": {"src": _url("s0_vo.wav")}}, "shots": []}
        ]
    }
    assert r._plan_editor_source_materialization("vid1", timeline) == []


# ------------------------------------------------------ planning (no materialize)

def test_plan_when_extraction_enabled_reports_available(assets_dir, monkeypatch):
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True)
    _write_episode(assets_dir, "vid1")
    # _card.png sibling for the _src.png image-copy path.
    (assets_dir / "s0_card_card.png").write_bytes(b"CARD")

    plan = r._plan_editor_source_materialization("vid1", _two_segment_timeline())

    by_type: dict[str, list[dict]] = {}
    for entry in plan:
        by_type.setdefault(entry["type"], []).append(entry)

    # Two VO audio windows, two demo videos, one card-copy image.
    assert len(by_type["audio"]) == 2
    assert len(by_type["video"]) == 2
    assert len(by_type["image"]) == 1

    for entry in by_type["audio"] + by_type["video"]:
        assert entry["status"] == "available"
        assert entry["action"] == "extract"
        assert entry["provenance"] == "derived_from_flattened_episode"
    img = by_type["image"][0]
    assert img["status"] == "available"
    assert img["action"] == "copy"
    assert img["provenance"] == "copied_from_existing_layer_parent"

    # Nothing was actually written in plan-only mode.
    assert not (assets_dir / "s0_vo.wav").exists()
    assert not (assets_dir / "s0_demo.mp4").exists()
    assert not (assets_dir / "s0_card_src.png").exists()


def test_plan_when_extraction_disabled_reports_blocked(assets_dir, monkeypatch):
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", False)
    _write_episode(assets_dir, "vid1")
    (assets_dir / "s0_card_card.png").write_bytes(b"CARD")

    plan = r._plan_editor_source_materialization("vid1", _two_segment_timeline())

    audio_video = [e for e in plan if e["type"] in {"audio", "video"}]
    assert audio_video, "expected the flattened-source entries to still be reported"
    for entry in audio_video:
        assert entry["status"] == "blocked"
        assert entry["provenance"] == "would_derive_from_flattened_episode"
        assert "disabled" in entry["reason"]

    # The cheap card-copy is NOT gated by the flag — it stays available.
    images = [e for e in plan if e["type"] == "image"]
    assert len(images) == 1
    assert images[0]["status"] == "available"


# -------------------------------------------------- materialize (real dispatch)

def test_materialize_extracts_with_cursor_advanced_windows(
    assets_dir, monkeypatch, captured_extracts
):
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True)
    audio_calls, video_calls = captured_extracts
    episode_video = _write_episode(assets_dir, "vid1")
    (assets_dir / "s0_card_card.png").write_bytes(b"CARD")

    out = r._materialize_editor_sources("vid1", _two_segment_timeline())

    # Materialized entries carry no status/action keys (they're done, not planned).
    for entry in out:
        assert "status" not in entry
        assert "action" not in entry

    # Audio windows: segment 0 starts at cursor 0 for 10s; segment 1 at cursor 10 for 8s.
    assert len(audio_calls) == 2
    assert audio_calls[0]["source"] == episode_video
    assert audio_calls[0]["start"] == pytest.approx(0.0)
    assert audio_calls[0]["duration"] == pytest.approx(10.0)
    assert audio_calls[1]["start"] == pytest.approx(10.0)
    assert audio_calls[1]["duration"] == pytest.approx(8.0)

    # Video windows derive from cursor + (startSec - clipStartSec) base offset.
    # seg0 demo: cursor 0 + (2 - 1) = 1.0 start; seg1 demo: cursor 10 + (0 - 0) = 10.0.
    assert len(video_calls) == 2
    starts = sorted(c["start"] for c in video_calls)
    assert starts[0] == pytest.approx(1.0)
    assert starts[1] == pytest.approx(10.0)
    assert all(c["duration"] > 0 for c in video_calls)

    # The card image copy actually happened (real shutil.copyfile, no ffmpeg).
    copied = assets_dir / "s0_card_src.png"
    assert copied.exists()
    assert copied.read_bytes() == b"CARD"


def test_materialize_blocked_flag_skips_extraction(assets_dir, monkeypatch, captured_extracts):
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", False)
    audio_calls, video_calls = captured_extracts
    _write_episode(assets_dir, "vid1")
    (assets_dir / "s0_card_card.png").write_bytes(b"CARD")

    out = r._materialize_editor_sources("vid1", _two_segment_timeline())

    # No ffmpeg dispatch when the safety flag is off.
    assert audio_calls == []
    assert video_calls == []
    # ...but the gated entries are still reported as blocked, and the card copy still runs.
    assert any(e["type"] == "video" and e["status"] == "blocked" for e in out)
    assert any(e["type"] == "audio" and e["status"] == "blocked" for e in out)
    assert (assets_dir / "s0_card_src.png").exists()


# ----------------------------------------- episode-singleton resolver branches
#
# These pin the schema-first / flat-fallback contract of the two episode-read
# resolvers so the migration's deliberate (permanent) flat fallback can't be
# silently broken by a future refactor. The fallback is NOT a half-finished
# migration: it is the documented, sanctioned read path for (a) un-migrated
# episodes whose only durable artifact is the flat legacy file, and (b)
# non-episode ids the gateway can't scope (episode_singleton_path -> None).


def test_timeline_resolver_prefers_schema_when_present(assets_dir):
    """Schema timeline.json exists -> resolver returns the schema path, ignoring
    the flat legacy name even when BOTH exist (schema is the factory's ground truth)."""
    ep = "ep_648e806a"
    schema = abn_assets.asset_path(ep, "timeline")
    schema.write_text('{"segments": [{"id": "s0"}]}')
    (assets_dir / f"{ep}_timeline.json").write_text('{"segments": [{"id": "STALE"}]}')

    assert r._timeline_file_for_episode(ep) == schema


def test_timeline_resolver_falls_back_to_flat_when_unmigrated(assets_dir):
    """No schema timeline, only the flat legacy file -> resolver returns the flat
    path (the permanent fallback for an episode not yet migrated to the schema)."""
    ep = "ep_7a15b8c3"
    flat = assets_dir / f"{ep}_timeline.json"
    flat.write_text('{"segments": [{"id": "s0"}]}')
    assert not abn_assets.episode_singleton_path(ep, "timeline").exists()

    assert r._timeline_file_for_episode(ep) == flat


def test_timeline_resolver_returns_schema_path_when_neither_exists(assets_dir):
    """For a real episode id with nothing on disk, the resolver returns the
    canonical schema path (so a downstream .exists() check fails on the path the
    factory WOULD write, not a flat legacy name)."""
    ep = "ep_deadbeef"
    resolved = r._timeline_file_for_episode(ep)
    assert resolved == abn_assets.episode_singleton_path(ep, "timeline")
    assert not resolved.exists()


def test_timeline_resolver_uses_flat_for_non_episode_id(assets_dir):
    """A non-episode id (no ep_ prefix) -> gateway returns None -> resolver uses
    the flat fallback. This is the permanent path for ad-hoc / video-id reads."""
    flat = assets_dir / "vid_abc123_timeline.json"
    flat.write_text('{"segments": []}')
    assert abn_assets.episode_singleton_path("vid_abc123", "timeline") is None

    assert r._timeline_file_for_episode("vid_abc123") == flat


def test_materialization_prefers_schema_episode_render(assets_dir, monkeypatch):
    """The source-materialization read picks the schema render
    {ep}/renders/episode.mp4 when present, NOT the flat {ep}_episode.mp4."""
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True)
    ep = "ep_648e806a"
    schema_render = abn_assets.asset_path(ep, "episode")
    schema_render.write_bytes(b"SCHEMA")
    # a stale flat render must be ignored in favor of the schema path
    (assets_dir / f"{ep}_episode.mp4").write_bytes(b"STALE")

    plan = r._plan_editor_source_materialization(ep, _two_segment_timeline())
    sources = {entry["source"] for entry in plan if "source" in entry}
    assert sources == {str(schema_render)}


def test_materialization_falls_back_to_flat_episode_render(assets_dir, monkeypatch):
    """No schema render -> the materialization read falls back to the flat legacy
    {ep}_episode.mp4 (permanent fallback for un-migrated episodes)."""
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True)
    ep = "ep_7a15b8c3"
    flat_render = assets_dir / f"{ep}_episode.mp4"
    flat_render.write_bytes(b"FLAT")
    assert not abn_assets.episode_singleton_path(ep, "episode").exists()

    plan = r._plan_editor_source_materialization(ep, _two_segment_timeline())
    sources = {entry["source"] for entry in plan if "source" in entry}
    assert sources == {str(flat_render)}


def test_card_copy_deduplicated_across_repeated_src(assets_dir, monkeypatch):
    """Two shots referencing the same missing _src.png plan a single copy."""
    monkeypatch.setattr(r, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True)
    _write_episode(assets_dir, "vid1")
    (assets_dir / "dup_card_card.png").write_bytes(b"CARD")
    timeline = {
        "segments": [
            {
                "durationSec": 5.0,
                "shots": [
                    {"src": _url("dup_card_src.png"), "startSec": 0, "durationSec": 2},
                    {"src": _url("dup_card_src.png"), "startSec": 2, "durationSec": 2},
                ],
            }
        ]
    }
    plan = r._plan_editor_source_materialization("vid1", timeline)
    images = [e for e in plan if e["type"] == "image"]
    assert len(images) == 1
