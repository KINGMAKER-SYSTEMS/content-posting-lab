"""Real native swaps and real HTTP/runner metadata; synthetic public media only."""
from pathlib import Path
import shutil

import pytest

import routers.control_plane as cp
import services.clip_transforms as transforms
from services.control_plane_sources import source_treatment_capability
from tests.test_clip_loop_swap import (
    LOSSLESS, _duration, _frame_hash, _tone_hz, _two_tone_clip,
)
from tests.test_control_plane_source_execution import (
    PAGE_ID, _isolated_source_dna, offline_shipstream_manifest,
    _pin_cuts_to_first_frame, headers, job_body, lab, publication,
)


def test_real_loop_swap_frame_audio_witness(tmp_path):
    source = tmp_path / "synthetic-two-tone.mp4"
    target = tmp_path / "swapped.mp4"
    _two_tone_clip(source, seconds=5.5)
    shutil.copyfile(source, target)
    transforms.apply_transforms(target, transforms.CutTransforms(loop_swap=True), LOSSLESS)
    assert _frame_hash(target, 0) == _frame_hash(source, 83)
    assert _frame_hash(target, 164) == _frame_hash(source, 82)
    assert _duration(target) == pytest.approx(5.5, abs=0.1)
    assert _tone_hz(source, 0.2) == pytest.approx(440, abs=60)
    assert _tone_hz(source, 5.0) == pytest.approx(880, abs=60)
    assert _tone_hz(target, 0.2) == pytest.approx(880, abs=60)
    assert _tone_hz(target, 5.0) == pytest.approx(440, abs=60)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_loop,draw_loop,origin_on", [
    pytest.param(True, True, True, id="loop-origin-on"),
    pytest.param(True, False, True, id="nonloop-origin-on"),
    pytest.param(False, True, True, id="unallowlisted-origin-on"),
    pytest.param(True, True, False, id="loop-origin-off"),
])
async def test_layerb_advertised_loop_matches_real_render(
    lab, monkeypatch, tmp_path, allow_loop, draw_loop, origin_on,
):
    client, _, _ = lab
    monkeypatch.setenv("SCRAMBLER_ENABLED", "0")
    if origin_on:
        monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    else:
        monkeypatch.delenv(cp.ADVERTISE_ORIGIN_ENV, raising=False)
    # Exercise the real draw and page allow-list. Only test draw probabilities
    # are made deterministic; rendering/recipe emission are never replaced.
    monkeypatch.setattr(transforms, "LOOP_SWAP_PAGES", frozenset({PAGE_ID}) if allow_loop else frozenset())
    monkeypatch.setattr(transforms, "LOOP_SWAP_SHARE", 1.0 if draw_loop else 0.0)
    monkeypatch.setattr(transforms, "MIRROR_SHARE", 0.0)
    monkeypatch.setattr(transforms, "MOTION_SHARE", 0.0)
    monkeypatch.setattr(transforms, "REVERSE_SHARE", 0.0)
    assert not cp.varies_clips(PAGE_ID), "fixture must isolate Layer B from Layer A"
    assert transforms.transforms_enabled(PAGE_ID, "pov-dirt-bike")
    route_duration_ms = 6_000
    control = source_treatment_capability()["controls"]["cutDurationMs"]
    assert control["min"] <= route_duration_ms <= control["max"]
    assert (route_duration_ms - control["min"]) % control["step"] == 0
    route_seconds = route_duration_ms / 1000
    audio_last_s = route_seconds - 0.5
    payload = publication(cut_duration_ms=route_duration_ms,
                          recipe_version="dossier-loop-metadata-native")
    response = client.post("/api/control-plane/v1/recipes", json=payload,
                           headers=headers("loop-native-register"))
    assert response.status_code == 200, response.json()
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1, payload),
                           headers=headers("loop-native-job"))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "synthetic-master.mp4"
    _two_tone_clip(source, seconds=route_seconds)

    async def cached_source(*_args):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    real_apply = cp.apply_transforms
    observed = []

    def observe_real_transform(path, drawn, encode_args):
        # Delegate the unchanged real encoder args and actual implementation.
        # Audio marks the actual router output; the separate lossless native
        # witness above pins exact frame83/82 without lossy hash assumptions.
        before_first = _tone_hz(Path(path), 0.2)
        before_last = _tone_hz(Path(path), audio_last_s)
        real_apply(path, drawn, encode_args)
        observed.append({"loop": drawn.loop_swap,
                         "before": (before_first, before_last),
                         "after": (_tone_hz(Path(path), 0.2), _tone_hz(Path(path), audio_last_s))})

    monkeypatch.setattr(cp, "apply_transforms", observe_real_transform)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    expected_loop = allow_loop and draw_loop
    assert "scrambler" not in clip
    assert clip["clipTransforms"]["loop_swap"] is expected_loop
    if expected_loop:
        assert len(observed) == 1 and observed[0]["loop"] is True
        assert observed[0]["before"] == pytest.approx((440, 880), abs=60)
        assert observed[0]["after"] == pytest.approx((880, 440), abs=60)
    else:
        assert observed == []
    if origin_on:
        assert clip["source"]["recipe"]["loop_split"] is (True if expected_loop else None), \
            "advertised Layer B loop metadata must match the actual native swap"
    else:
        assert "recipe" not in clip["source"]
