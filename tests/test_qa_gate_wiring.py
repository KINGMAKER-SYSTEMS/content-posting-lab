"""The QA gate inside the source-cut executor (routers/control_plane.py
_run_dossier_source_inner): a hard fault drops the cut through the existing
refusal paths and never becomes a clip; a look issue is scored on the clip
manifest; a SAR-only fault is repaired in place."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import qa_gate
from tests import qa_gate_clips as clips
from tests import test_control_plane_source_execution as source_tests

# The executor fixtures (and their autouse isolation) from the source-execution
# suite, bound by name so pytest sees them in this module.
lab = source_tests.lab
_isolated_source_dna = source_tests._isolated_source_dna
_advertise_zero_reasons = source_tests._advertise_zero_reasons
offline_shipstream_manifest = source_tests.offline_shipstream_manifest
publication = source_tests.publication
headers = source_tests.headers
job_body = source_tests.job_body
MASTER_SHA = source_tests.MASTER_SHA


@pytest.fixture(autouse=True)
def _gate_enforced(monkeypatch):
    clips.require_ffmpeg()
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "enforce")


def _queue_one_cut(client, tag: str, *, duration_ms: int = 6_000, quantity: int = 1) -> str:
    response = client.post("/api/control-plane/v1/jobs", json=job_body(quantity),
                           headers=headers(f"source-job-qa-{tag}"))
    assert response.status_code == 200, response.text
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:0:{duration_ms}",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": 0,
        "startMs": 0,
        "durationMs": duration_ms,
    } for _ in range(quantity)]
    cp.atomic_save(cp._jobs_path(), store)
    return job_id


def _stub_render(monkeypatch, tmp_path, writers):
    """Replace the cut encode with synthetic bodies, one writer per call, the
    way the executor suite stubs run_color_correct. Layer B is neutralized as
    those tests do, so the gate sees exactly the written file."""
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        writers[len(calls)](Path(dst))
        calls.append((dst, kwargs))

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 6.0)
    return calls


@pytest.mark.asyncio
async def test_a_real_cut_with_a_tail_jump_is_healed_and_delivered(lab, monkeypatch):
    # The master jumps to another shot 2 frames before the planned 6 s window
    # ends; the real executor cuts it. Ruling L57062: endings never block.
    # Enforce heals it (control-plane-pad#283): the last good frame is held
    # over the jump, same 180 frames, and the clip ships clean.
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "tail")
    master = tmp_path / "master.mp4"
    clips._encode(master, clips._concat(clips._src(177.5 / 30, size="320x568"),
                                        clips._solid(9.0, "red", size="320x568")), frames=15 * 30)

    async def cached_source(*_):
        return master

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["ok"] is True and record["blocks"] == [] and record["mode"] == "enforce"
    assert record["warnings"] == [] and record["score"] == 100 and record["healed"] == ["edges"]
    assert record["frames"] == 180 and record["tailCuts"] == []
    assert "qaRefusedCuts" not in job


@pytest.mark.asyncio
async def test_a_real_clean_cut_scores_one_hundred(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "clean")
    master = clips.clean(tmp_path / "master.mp4", seconds=12.0, size="320x568")

    async def cached_source(*_):
        return master

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["ok"] and record["score"] == 100 and record["warnings"] == []
    assert record["frames"] == 180 and record["repairedSar"] is False


@pytest.mark.parametrize("fault,writer,reason", [
    # A Lab cut may carry its master's audio (policy_for_source_cut), so the
    # format faults here are length and frame; audio is covered at the
    # policy level in test_qa_gate.py.
    ("long", lambda dst: clips.clean(dst, seconds=7.0), "qa_duration_mismatch"),
    ("short", lambda dst: clips.clean(dst, seconds=5.9), "qa_duration_mismatch"),
    ("frame", lambda dst: clips.clean(dst, size="720x1280"), "qa_geometry_mismatch"),
    ("broken", lambda dst: clips.truncated(clips.clean(dst.with_name("ok.mp4")), dst), None),
])
@pytest.mark.asyncio
async def test_a_hard_fault_fails_the_only_cut_with_the_gates_reason(lab, monkeypatch, fault, writer, reason):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, fault)
    _stub_render(monkeypatch, tmp_path, [writer])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    if reason is not None:
        assert job["error"] == reason
    else:
        assert job["error"] in {"qa_decode_failed", "qa_probe_unreadable", "qa_duration_mismatch"}
    assert job["qaRefusedCuts"][0]["cutIndex"] == 0 and job["qaRefusedCuts"][0]["reason"] == job["error"]
    assert "clips" not in job or not job["clips"]
    assert not list(Path(job["artifactRoot"]).glob("treated/*.mp4")) or not Path(job["artifactRoot"]).exists()


@pytest.mark.asyncio
async def test_one_bad_cut_drops_only_itself(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "pair", quantity=2)
    _stub_render(monkeypatch, tmp_path, [
        lambda dst: clips.clean(dst, seconds=7.0),  # cut 0: 7 s on a 6 s recipe -> dropped
        lambda dst: clips.clean(dst),               # cut 1: clean -> delivered
    ])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 1 and len(job["sourceCuts"]) == 1
    assert job["clips"][0]["qaGate"]["ok"] and job["clips"][0]["qaGate"]["score"] == 100
    assert job["qaRefusedCuts"] == [{
        "cutIndex": 0, "slotId": f"{MASTER_SHA}:0:6000",
        "reason": "qa_duration_mismatch",
        "detail": "210 frames (7.000 s) is not the recipe's 180 +-1",
    }]


@pytest.mark.asyncio
async def test_a_sar_only_fault_is_repaired_before_the_clip_is_hashed(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "sar")
    before = {}

    def write_bad_sar(dst: Path):
        clean = clips.clean(dst.with_name("clean.mp4"))
        clips.non_square_sar(clean, dst)
        before["md5"] = clips.framemd5(dst)
        before["sha"] = hashlib.sha256(dst.read_bytes()).hexdigest()

    _stub_render(monkeypatch, tmp_path, [write_bad_sar])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    output = Path(job["artifactRoot"]) / clip["path"]
    assert clip["qaGate"]["repairedSar"] is True and clip["qaGate"]["ok"]
    assert qa_gate.probe_clip(output).sample_aspect_ratio == "1:1"
    assert clips.framemd5(output) == before["md5"]
    assert clip["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest() != before["sha"]
    assert clip["sourceTreatment"]["sourceSha256"] == clip["sha256"]


@pytest.mark.asyncio
async def test_black_run_and_freeze_are_scored_not_blocked(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "look")

    def write_look_faults(dst: Path):
        clips._encode(dst, clips._concat(clips._src(2.0), clips._solid(0.5, "black"),
                                         clips._src(1.5), clips._solid(1.0, "gray"), clips._src(1.5)),
                      frames=180)

    _stub_render(monkeypatch, tmp_path, [write_look_faults])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["ok"] and set(record["warnings"]) == {"qa_black_run", "qa_freeze"}
    # The 0.5 s black card is also a 0.5 s still (not over the freeze floor);
    # only the 1 s gray hold is a freeze warning.
    assert record["score"] == 60 and record["blackRuns"] == [[2.0, 2.5]]
    assert record["warnings"].count("qa_freeze") == 1 and [4.0, 5.0] in record["freezeRuns"]


@pytest.mark.asyncio
async def test_shadow_mode_records_the_block_but_delivers(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "shadow")
    job_id = _queue_one_cut(client, "shadow")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=7.0)])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["mode"] == "shadow" and record["ok"] is False
    assert record["blocks"] == ["qa_duration_mismatch"] and "qaRefusedCuts" not in job


@pytest.mark.asyncio
async def test_off_mode_skips_the_gate(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "off")
    job_id = _queue_one_cut(client, "off")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=7.0)])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert "qaGate" not in job["clips"][0]


@pytest.mark.asyncio
async def test_shadow_leaves_a_sar_fault_unrepaired_same_bytes_same_sha(lab, monkeypatch):
    # L57135 (c): shadow changes nothing.
    client, tmp_path, _ = lab
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "shadow")
    job_id = _queue_one_cut(client, "shadow-sar")
    before = {}

    def write_bad_sar(dst: Path):
        clips.non_square_sar(clips.clean(dst.with_name("clean.mp4")), dst)
        before["bytes"] = dst.read_bytes()

    _stub_render(monkeypatch, tmp_path, [write_bad_sar])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    output = Path(job["artifactRoot"]) / clip["path"]
    assert output.read_bytes() == before["bytes"]
    assert clip["sha256"] == hashlib.sha256(before["bytes"]).hexdigest()
    assert clip["qaGate"]["mode"] == "shadow" and clip["qaGate"]["repairedSar"] is False
    assert clip["qaGate"]["ok"] is False and clip["qaGate"]["blocks"] == ["qa_sar_non_square"]
    assert qa_gate.probe_clip(output).sample_aspect_ratio == "2272:2277"


@pytest.mark.asyncio
async def test_a_tool_outage_passes_the_cut_and_never_fails_the_job(lab, monkeypatch):
    # L57135 (b): a missing/hung ffmpeg is logged; the cut passes as in shadow.
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "outage")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=7.0)])  # would BLOCK if scanned

    def hung(*args, **kwargs):
        raise qa_gate.QaGateError("ffmpeg_scan_TimeoutExpired")

    monkeypatch.setattr(qa_gate, "scan_clip", hung)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["ok"] and record["blocks"] == [] and record["toolError"] == "ffmpeg_scan_TimeoutExpired"
    assert (Path(job["artifactRoot"]) / job["clips"][0]["path"]).is_file()
    assert "qaRefusedCuts" not in job


# ── a cut is counted at its own rate (control-plane-pad#327) ─────────────────
#
# Since Lab #414 a 25 fps master is cut at 25 fps (delivery_frame_rate). The
# gate counts the 6 s window in the cut's own frames: 150 at 25 fps, not 180.

@pytest.mark.asyncio
async def test_a_25fps_cut_of_the_right_length_is_delivered_under_enforce(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "rate25")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=6.0, fps=25)])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["ok"] is True and record["blocks"] == [] and record["frames"] == 150
    assert "qaRefusedCuts" not in job


@pytest.mark.asyncio
async def test_a_25fps_cut_of_the_wrong_length_is_still_refused(lab, monkeypatch):
    client, tmp_path, _ = lab
    job_id = _queue_one_cut(client, "rate25long")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=7.0, fps=25)])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed" and job["error"] == "qa_duration_mismatch"
    assert job["qaRefusedCuts"][0]["detail"] == "175 frames (7.000 s) is not the recipe's 150 +-1"


@pytest.mark.asyncio
async def test_shadow_records_a_25fps_cut_of_the_right_length_as_ok(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv("CONTENT_LAB_QA_GATE", "shadow")
    job_id = _queue_one_cut(client, "rate25shadow")
    _stub_render(monkeypatch, tmp_path, [lambda dst: clips.clean(dst, seconds=6.0, fps=25)])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    record = job["clips"][0]["qaGate"]
    assert record["mode"] == "shadow" and record["ok"] is True and record["blocks"] == []
    assert record["details"] == [] and record["frames"] == 150
