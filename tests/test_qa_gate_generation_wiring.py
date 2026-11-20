"""The QA gate inside the provider-generation executor
(routers/control_plane.py _run_owned_dossier_generation, ROADMAP L57098):
a quarter-turned provider clip (Joy eba568ef) never becomes a clip; the
other candidates of the call still deliver; a call that keeps no candidate
is a qa_gate provider failure under the existing terminal handling."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import routers.control_plane as cp
from tests import qa_gate_clips as clips
from tests import test_control_plane_dossier_execution as dossier_tests

lab = dossier_tests.lab
_advertise_zero_reasons = dossier_tests._advertise_zero_reasons
job_body = dossier_tests.job_body
HEADERS = dossier_tests.HEADERS


@pytest.fixture(autouse=True)
def _gate_enforced(monkeypatch):
    clips.require_ffmpeg()
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "enforce")


def _fake_provider(writers):
    """One provider call, five 'both' crops like the truck fixture recipe;
    each crop file is written by the matching writer (a real synthetic body)."""
    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        master = clips.clean(folder / "master.mp4", seconds=3.0, size="1920x1080")
        crops = []
        for crop_index in range(5):
            crop = folder / f"crop-{crop_index}.mp4"
            writers[crop_index](crop)
            crops.append({"file": str(crop.relative_to(output_dir)), "cropMode": "both",
                          "cropIndex": crop_index, "cropCount": 5, "width": 1080, "height": 1920})
        jobs[provider_job_id]["videos"][index].update({
            "status": "done", "file": crops[0]["file"],
            "provider_master_file": str(master.relative_to(output_dir)), "crops": crops,
        })
    return fake_generate_one


async def _copy_treat(source, destination, color_correction, scale=None, playback_speed=1.0, clip_crop=None):
    shutil.copyfile(source, destination)


async def _fake_thumbnail(job_root, video, index):
    target = job_root / "thumbnails" / f"{index:04d}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"jpeg-thumbnail")
    return cp._generated_manifest(job_root, target)


def _wire(monkeypatch, writers):
    monkeypatch.setattr(cp, "generate_one", _fake_provider(writers))
    monkeypatch.setattr(cp, "run_color_correct", _copy_treat)
    monkeypatch.setattr(cp, "_thumbnail_manifest", _fake_thumbnail)


def _uprights(count):
    """Distinct upright bodies (one seed each: the duplicate-output guard
    refuses byte-identical sibling clips)."""
    return [(lambda seed: (lambda dst: clips.upright_scene(dst, seconds=3.0, seed=seed)))(index + 1)
            for index in range(count)]


def _queue(client):
    response = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["jobId"]


@pytest.mark.asyncio
async def test_a_sideways_provider_clip_is_delivered_with_a_warning_score(lab, monkeypatch):
    # L57135 (a): the image cue warns and scores; only the params rung blocks.
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [lambda dst: clips.sideways_scene(dst, seconds=3.0)] + _uprights(4))
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 5 and job.get("error") is None
    turned = job["clips"][0]["qaGate"]
    assert turned["ok"] and turned["blocks"] == [] and "qa_sideways_picture" in turned["warnings"]
    assert turned["score"] <= 60 and turned["orientation"]["sideways"] is True
    assert "qaRefusedClips" not in job
    job["clips"] = job["clips"][1:]  # the four upright siblings below
    # The synthetic siblings do not move, so each carries only the freeze
    # look warning (one or two runs: score 85 or 70): warnings, never blocks.
    records = [clip["qaGate"] for clip in job["clips"]]
    assert all(record["ok"] and record["blocks"] == [] for record in records), records
    assert all(set(record["warnings"]) == {"qa_freeze"} and 70 <= record["score"] < 100 for record in records), records
    assert all(clip["qaGate"]["orientation"]["sideways"] is False for clip in job["clips"])
    assert [clip["delivery"]["crop"]["index"] for clip in job["clips"]] == [1, 2, 3, 4]
    assert "providerFailures" not in job or not job["providerFailures"]


@pytest.mark.asyncio
async def test_a_landscape_provider_clip_is_dropped_and_its_siblings_deliver(lab, monkeypatch):
    # The params rung: a display frame that is not portrait blocks.
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [lambda dst: clips.clean(dst, seconds=3.0, size="1920x1080")] + _uprights(4))
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 4 and job.get("error") is None
    assert [clip["delivery"]["crop"]["index"] for clip in job["clips"]] == [1, 2, 3, 4]
    (refused,) = job["qaRefusedClips"]
    assert refused["generationIndex"] == 0 and refused["candidateIndex"] == 0
    assert refused["reason"] == "qa_orientation_mismatch" and refused["file"].endswith("crop-0.mp4")
    root = Path(job["artifactRoot"])
    assert not (root / "treated" / "g00-c00.mp4").exists()  # the treated copy is gone
    assert "providerFailures" not in job or not job["providerFailures"]


@pytest.mark.asyncio
async def test_a_call_whose_every_clip_is_blocked_is_a_qa_gate_provider_failure(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [(lambda seed: (lambda dst: clips.upright_scene(dst, seconds=3.0, size="1920x1080", seed=seed)))(index)
                        for index in range(5)])
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed" and job["error"] == "provider_generation_failed"
    assert job["errorClass"] == "qa_gate" and job["errorDetail"] == "qa_orientation_mismatch"
    assert len(job["qaRefusedClips"]) == 5
    assert job["providerFailures"][-1]["class"] == "qa_gate"
    assert job["providerFailures"][-1]["terminal"] == "qa_orientation_mismatch"
    status = client.get(f"/api/control-plane/v1/jobs/{job_id}",
                        headers={"Authorization": f"Bearer {dossier_tests.TOKEN}",
                                 "X-RT-Page-Id": dossier_tests.PAGE_ID})
    assert status.status_code == 200
    assert status.json().get("errorClass") == "qa_gate"


@pytest.mark.asyncio
async def test_a_landscape_provider_clip_is_a_param_block(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [lambda dst: clips.clean(dst, seconds=3.0, size="1920x1080")] + _uprights(4))
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 4
    assert job["qaRefusedClips"][0]["reason"] == "qa_orientation_mismatch"


@pytest.mark.asyncio
async def test_shadow_mode_records_a_landscape_clip_but_delivers_it(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "shadow")
    job_id = _queue(client)
    _wire(monkeypatch, [lambda dst: clips.clean(dst, seconds=3.0, size="1920x1080")] + _uprights(4))
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed" and len(job["clips"]) == 5
    assert job["clips"][0]["qaGate"]["ok"] is False
    assert job["clips"][0]["qaGate"]["blocks"] == ["qa_orientation_mismatch"]
    assert "qaRefusedClips" not in job


@pytest.mark.asyncio
async def test_a_tool_outage_passes_every_provider_clip_and_never_fails_the_job(lab, monkeypatch):
    # L57135 (b): no ffprobe = log + pass as in shadow; nothing deleted, nothing failed.
    from services import qa_gate
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [lambda dst: clips.clean(dst, seconds=3.0, size="1920x1080")] + _uprights(4))

    def no_tool(*args, **kwargs):
        raise qa_gate.QaGateError("ffprobe_unavailable")

    monkeypatch.setattr(qa_gate, "probe_clip", no_tool)
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 5 and "qaRefusedClips" not in job
    assert all(clip["qaGate"]["ok"] and clip["qaGate"]["toolError"] == "ffprobe_unavailable" for clip in job["clips"])
    assert all((Path(job["artifactRoot"]) / clip["path"]).is_file() for clip in job["clips"])


def _rotated_upright(dst):
    coded = clips.upright_scene(dst.with_name(dst.stem + ".coded.mp4"), seconds=3.0, size="1920x1080", seed=7)
    return clips.with_display_rotation(coded, dst, degrees=90)


def _yuv444(dst):
    return clips._encode(dst, clips._src(3.0), frames=90, extra=["-pix_fmt", "yuv444p"])


def _padded_25(dst):
    import subprocess
    native = clips.clean(dst.with_name(dst.stem + ".n25.mp4"), seconds=3.0, fps=25)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(native), "-vf", "fps=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(dst)],
                   check=True, capture_output=True)
    return dst


def _tail_jump(dst):
    return clips.with_tail_cut(dst, seconds=3.0, frames_before_end=3)


@pytest.mark.asyncio
async def test_break_and_recover_every_healable_paid_clip_is_delivered(lab, monkeypatch):
    # control-plane-pad#283: a paid call whose clips carry a rotate tag, a
    # 4:4:4 pixel format, 25-in-30 stutter and a last-frames jump comes back
    # with all five clips clean; before the heals two were refused and the
    # page needed a hand-started make-up job.
    client, tmp_path, _ = lab
    job_id = _queue(client)
    _wire(monkeypatch, [_rotated_upright, _yuv444, _padded_25, _tail_jump] + _uprights(1))
    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 5 and "qaRefusedClips" not in job
    records = [clip["qaGate"] for clip in job["clips"]]
    assert all(record["ok"] and record["blocks"] == [] for record in records), records
    assert [record["healed"] for record in records[:4]] == [["format"], ["format"], ["stutter"], ["edges"]]
    assert all(not ({"qa_stutter", "qa_tail_cut"} & set(record["warnings"])) for record in records)
    # The paid provider files are evidence: never rewritten.
    root = Path(job["artifactRoot"])
    assert all((root / "treated" / f"g00-c{index:02d}-sar1.mp4").exists() for index in range(4))
    from services import qa_gate
    provider_files = sorted(tmp_path.rglob("crop-0.mp4")) + sorted(tmp_path.rglob("crop-1.mp4"))
    assert len(provider_files) == 2
    assert qa_gate.probe_clip(provider_files[0]).rotation != 0          # still the paid original
    assert qa_gate.probe_clip(provider_files[1]).pixel_format == "yuv444p"
