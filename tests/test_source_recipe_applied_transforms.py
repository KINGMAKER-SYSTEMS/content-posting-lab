"""Compare advertised recipes with the real executor's completed media calls.

Planning, HTTP enqueue, stored draws and receipt construction run normally;
FFmpeg-facing calls use the existing executor fixture, so no media is encoded.
"""
import pytest

import routers.control_plane as cp
from services.clip_transforms import CutTransforms
from tests.test_control_plane_source_execution import (
    PAGE_ID, _isolated_source_dna, headers, job_body, lab,
)
from tests.test_scrambler_executor_behavior import (
    _job_scrambler_records, _originality_feed, _run, _scrambler_on,
    _wire_executor, isolated_scrambler_state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("treatment", ["none", "split_swap"])
@pytest.mark.parametrize("advertise", [False, True])
async def test_advertised_recipe_describes_actual_scrambler_render(
    lab, monkeypatch, tmp_path, treatment, advertise,
):
    client, _, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ORIGIN", "1" if advertise else "0")
    monkeypatch.setenv(
        "SCRAMBLER_TREATMENT_WEIGHTS",
        f"none:{int(treatment == 'none')},split_swap:{int(treatment == 'split_swap')},camera:0,grade:0",
    )
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("actual-recipe-" + treatment),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    before = cp._load_jobs()["jobs"][job_id]
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    # Both legacy gates can be configured on, but Scrambler records exclude
    # those lanes. A legacy draw must never stack with the Scrambler pass.
    monkeypatch.setattr(cp, "varies_clips", lambda *_: True)
    monkeypatch.setattr(cp, "transforms_enabled", lambda *_: True)

    def forbidden_legacy_draw(*_):
        raise AssertionError("legacy transform draw stacked onto Scrambler")

    monkeypatch.setattr(cp, "cut_transforms", forbidden_legacy_draw)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    draw = _job_scrambler_records(job_id)[0]
    rendered = lane["renders"][0]
    assert rendered["playback_speed"] == draw["speed"] != 1.0
    assert bool(lane["split_swaps"]) == (treatment == "split_swap")
    assert len(lane["transforms"]) == (1 if draw["mirror"] else 0)
    clip = job["clips"][0]
    # These existing Worker admission fields intentionally retain the recipe's
    # baseline treatment; only source.recipe advertises effective variations.
    assert clip["clipSpeed"] == clip["sourceTreatment"]["visualTreatment"]["clipSpeed"] == 1.0
    identity_fields = ("sourceLibraryId", "sourceLibraryHash", "recipeVersion")
    assert {key: job[key] for key in identity_fields} == {key: before[key] for key in identity_fields}
    if not advertise:
        assert "recipe" not in clip["source"]
        assert "origin" not in clip["source"]["master"]
        return
    expected = {
        "speed": rendered["playback_speed"], "reverse": None,
        "hflip": True if draw["mirror"] else None,
        "loop_split": True if treatment == "split_swap" else None,
        "crop": None,
    }
    assert clip["source"]["recipe"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mirror,reverse,loop", [
    (False, False, True), (True, True, True), (False, True, False),
    (True, False, False), (False, False, False),
])
async def test_advertised_recipe_describes_applied_layer_b_loop(
    lab, monkeypatch, tmp_path, mirror, reverse, loop,
):
    client, _, _ = lab
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ORIGIN", "1")
    monkeypatch.setattr(cp, "varies_clips", lambda *_: False)
    monkeypatch.setattr(cp, "transforms_enabled", lambda *_: True)
    # Pin a valid draw to cover each treatment combination deterministically.
    transform = CutTransforms(mirror=mirror, reverse=reverse, loop_swap=loop)
    monkeypatch.setattr(cp, "cut_transforms", lambda *_: transform)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("actual-recipe-layer-b-loop"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["transforms"]) == int(mirror or reverse or loop)
    if lane["transforms"]:
        assert lane["transforms"][0][0][1] == transform
    emitted = job["clips"][0]["source"]["recipe"]
    assert emitted["loop_split"] is (True if loop else None)
    assert emitted["reverse"] is (True if reverse else None)
    assert emitted["hflip"] is (True if mirror else None)


@pytest.mark.asyncio
async def test_scrambler_actual_reverse_and_mirror_are_advertised(lab, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from services import scrambler
    from tests.test_scrambler_executor_behavior import _prime_rendered

    client, _, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ORIGIN", "1")
    # Fourteen actual persisted rendered credits make the next reverse legal;
    # open its gate and pin only the mirror draw, not the reserved record.
    _prime_rendered(
        PAGE_ID, 14, day0=datetime.now(timezone.utc) - timedelta(days=20),
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(scrambler, "REVERSE_GATE", 1.0)
    unit = scrambler._unit
    monkeypatch.setattr(scrambler, "_unit", lambda seed, part: 0.0 if part == "mirror" else unit(seed, part))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("actual-recipe-scrambler-reverse"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    draw = _job_scrambler_records(job_id)[0]
    assert draw["treatment"] == "reverse" and draw["mirror"] is True
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["transforms"]) == 1
    applied = lane["transforms"][0][0][1]
    assert applied.reverse is True and applied.mirror is True
    emitted = job["clips"][0]["source"]["recipe"]
    assert emitted["speed"] == lane["renders"][0]["playback_speed"]
    assert emitted["reverse"] is True and emitted["hflip"] is True
    assert emitted["loop_split"] is None and not lane["split_swaps"]


@pytest.mark.asyncio
async def test_applied_recipe_state_resets_after_a_treated_cut(lab, monkeypatch, tmp_path):
    client, _, _ = lab
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ORIGIN", "1")
    monkeypatch.setattr(cp, "varies_clips", lambda *_: False)
    monkeypatch.setattr(cp, "transforms_enabled", lambda *_: True)
    draws = iter([
        CutTransforms(mirror=True, reverse=True, loop_swap=True), CutTransforms(),
    ])
    monkeypatch.setattr(cp, "cut_transforms", lambda *_: next(draws))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("actual-recipe-mixed-cuts"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["renders"]) == 2 and len(lane["transforms"]) == 1
    first, second = (clip["source"]["recipe"] for clip in job["clips"])
    assert first["reverse"] is first["hflip"] is first["loop_split"] is True
    assert second == dict(speed=None, crop=None, reverse=None, hflip=None, loop_split=None)
