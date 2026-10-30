"""Scrambler (PR #281) x Layer B (#284) precedence, after the rebase onto main.

Rebase decision (pixel89 lead): a job that carries Scrambler draw records -
i.e. the Scrambler was ON for the page at enqueue and its execute-time gate
still passes - is varied ONLY by the Scrambler. Layer B (mirror / motion /
reverse / loop-swap from services/clip_transforms.py) and Layer A must not
stack on a Scrambler cut. Every job WITHOUT records (Scrambler OFF, another
page, or a legacy job enqueued before the switch turned on) takes main's
Layer B path unchanged.

Duration after the merge: a Scrambler cut keeps its hard fail on an
unreadable delivered duration and its 5-9 s output bound; a Layer-B-only cut
keeps main's behaviour (unreadable probe warns and delivers; bound 5-10 s,
9 s when loop-swapped).

These drive the REAL enqueue endpoint and executor; only ffmpeg-facing
helpers are stubbed (argument spies).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import routers.control_plane as cp
from services.clip_transforms import CutTransforms
from tests.test_control_plane_source_execution import (  # noqa: F401
    PAGE_ID,
    _isolated_source_dna,  # autouse: #279 retired libraries; isolated registry
    headers,
    job_body,
    lab,
)
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _job_scrambler_records,
    _originality_feed,
    _scrambler_on,
    isolated_scrambler_state,
)

LAYER_B = CutTransforms(mirror=True, reverse=True)


def _stub_media(monkeypatch, tmp_path, probe_value):
    """Spy every media helper; the executor's decision logic runs for real."""
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    lane = {"renders": [], "transforms": [], "split_swaps": [], "probes": [],
            "cut_transforms": []}

    async def cached_source(*_args):
        return source

    async def render(src, dst, correction, **kwargs):
        lane["renders"].append(kwargs)
        Path(dst).write_bytes(b"cut")

    def probe(path):
        lane["probes"].append(str(path))
        return probe_value

    def layer_b_draw(*args, **kwargs):
        lane["cut_transforms"].append(args)
        return LAYER_B

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", probe)
    monkeypatch.setattr(cp, "cut_transforms", layer_b_draw)
    # Layer B forced on for the page (pov-scenic is a source-cut format, so
    # this matches the production default with TRANSFORMS_ALL on).
    monkeypatch.setattr(cp, "transforms_enabled", lambda page_id, slug: True)
    monkeypatch.setattr(cp, "apply_transforms",
                        lambda path, transforms, encode_args: lane["transforms"].append(transforms))
    monkeypatch.setattr(cp, "apply_split_swap",
                        lambda *args, **kwargs: lane["split_swaps"].append(args))
    return lane


def _enqueue(client, key, quantity=1):
    response = client.post("/api/control-plane/v1/jobs", json=job_body(quantity),
                           headers=headers(key))
    assert response.status_code == 200, response.json()
    return response.json()["jobId"]


def _scrambler_job(client, monkeypatch, tmp_path, key, quantity=1):
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    # Deterministic Scrambler treatment with no transform of its own, so any
    # apply_transforms call can only come from Layer B.
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    job_id = _enqueue(client, key, quantity)
    records = _job_scrambler_records(job_id)
    assert all(isinstance(record, dict) for record in records)
    return job_id, records


# ---------------------------------------------------------------------------
# The vary decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrambler_on_page_cuts_get_no_layer_b(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id, records = _scrambler_job(client, monkeypatch, tmp_path, "pb-on-no-layer-b", 2)
    # Even a clip-varying (Layer A) page: the Scrambler owns variation.
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: True)
    lane = _stub_media(monkeypatch, tmp_path, None)
    # Probe returns each cut's drawn output length (inside 5-9 s).
    lengths = iter(record["lengthMs"] / 1000.0 for record in records)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: next(lengths))
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["renders"]) == 2
    # No Layer B draw, no Layer B encode on any Scrambler cut.
    assert lane["cut_transforms"] == []
    # Only the Scrambler's OWN drawn transforms (its mirror dimension and a
    # camera / reverse treatment) may reach apply_transforms - never the
    # Layer B set, and never a second pass stacked on the same cut.
    own = [CutTransforms(mirror=bool(r["mirror"]),
                         motion="push_in" if r["treatment"] == "camera" else None,
                         reverse=r["treatment"] == "reverse") for r in records]
    assert lane["transforms"] == [t for t in own if t.any()]
    assert LAYER_B not in lane["transforms"]
    for clip, record, mine in zip(job["clips"], records, own):
        assert clip["clipTransforms"] == {
            **mine.manifest(), "split_swap": record["treatment"] == "split_swap",
        }
        assert "similarity" not in clip  # Layer A trial instrument stays off
        assert clip["scrambler"]["cutIndex"] == record["cutIndex"]


@pytest.mark.asyncio
async def test_scrambler_off_page_gets_layer_b_exactly_as_main(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, "pb-off-layer-b", 2)
    lane = _stub_media(monkeypatch, tmp_path, 8.0)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["cut_transforms"]) == 2
    assert lane["transforms"] == [LAYER_B, LAYER_B]
    assert len(lane["probes"]) == 2
    for clip in job["clips"]:
        assert clip["clipTransforms"] == LAYER_B.manifest()
        assert "scrambler" not in clip


@pytest.mark.asyncio
async def test_scrambler_on_for_another_page_leaves_this_page_on_layer_b(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch, pages="tt-some-other-page")
    _originality_feed(monkeypatch, tmp_path)
    job_id = _enqueue(client, "pb-other-page-layer-b")
    assert _job_scrambler_records(job_id) == [None]
    lane = _stub_media(monkeypatch, tmp_path, 8.0)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert lane["transforms"] == [LAYER_B]
    assert job["clips"][0]["clipTransforms"] == LAYER_B.manifest()


@pytest.mark.asyncio
async def test_legacy_job_executed_after_scrambler_turns_on_keeps_layer_b(lab, monkeypatch, tmp_path):
    # Precedence keys off the JOB's own draw records, not the live switch: a
    # job enqueued while OFF has no Scrambler variation, so it must keep
    # main's Layer B even if the page is switched ON before it executes.
    client, tmp_path, _ = lab
    job_id = _enqueue(client, "pb-legacy-then-on")
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    lane = _stub_media(monkeypatch, tmp_path, 8.0)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert lane["transforms"] == [LAYER_B]
    assert job["clips"][0]["clipTransforms"] == LAYER_B.manifest()
    assert "scrambler" not in job["clips"][0]


# ---------------------------------------------------------------------------
# Duration probe + bounds after the merge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrambler_cut_with_unreadable_duration_fails_hard(lab, monkeypatch, tmp_path, caplog):
    client, tmp_path, _ = lab
    job_id, _ = _scrambler_job(client, monkeypatch, tmp_path, "pb-scr-probe-none")
    lane = _stub_media(monkeypatch, tmp_path, None)
    cp._layer_b_probe_unreadable_count = 0
    with caplog.at_level("WARNING", logger="control_plane"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert "source_output_duration_unsupported" in job["error"]
    assert lane["probes"]
    # Never the Layer B warn-and-deliver path.
    assert cp._layer_b_probe_unreadable_count == 0
    assert not any("layer_b_probe_unreadable" in r.message for r in caplog.records)


@pytest.mark.parametrize("seconds", [4.9, 9.2])
@pytest.mark.asyncio
async def test_scrambler_cut_outside_five_to_nine_seconds_fails(lab, monkeypatch, tmp_path, seconds):
    client, tmp_path, _ = lab
    job_id, _ = _scrambler_job(client, monkeypatch, tmp_path, f"pb-scr-bound-{seconds}")
    _stub_media(monkeypatch, tmp_path, seconds)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert "source_output_duration_unsupported" in job["error"]


@pytest.mark.parametrize("seconds", [5.0, 9.0])
@pytest.mark.asyncio
async def test_scrambler_cut_inside_five_to_nine_seconds_delivers(lab, monkeypatch, tmp_path, seconds):
    client, tmp_path, _ = lab
    job_id, _ = _scrambler_job(client, monkeypatch, tmp_path, f"pb-scr-inside-{seconds}")
    _stub_media(monkeypatch, tmp_path, seconds)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")


@pytest.mark.asyncio
async def test_layer_b_only_cut_keeps_main_ten_second_bound(lab, monkeypatch, tmp_path):
    # Main: a Layer-B-only cut that is not loop-swapped is admitted up to 10 s.
    client, tmp_path, _ = lab
    job_id = _enqueue(client, "pb-layer-b-9-5")
    lane = _stub_media(monkeypatch, tmp_path, 9.5)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert lane["transforms"] == [LAYER_B]


@pytest.mark.asyncio
async def test_layer_b_only_cut_with_unreadable_duration_still_delivers(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, "pb-layer-b-probe-none")
    _stub_media(monkeypatch, tmp_path, None)
    cp._layer_b_probe_unreadable_count = 0
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert cp._layer_b_probe_unreadable_count == 1


# ---------------------------------------------------------------------------
# #292 transforms log on a Scrambler job (rebase onto a67221b5)
# ---------------------------------------------------------------------------

def _transform_lines(caplog):
    off = [r.getMessage() for r in caplog.records
           if r.getMessage().startswith("content_lab.source_cut_transforms_off ")]
    per_cut = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("content_lab.source_cut_transforms job_id=")]
    return off, per_cut


@pytest.mark.asyncio
async def test_scrambler_job_logs_layer_b_off_with_reason_scrambler(lab, monkeypatch, tmp_path, caplog):
    """A Scrambler job has Layer B off because the Scrambler owns its cuts.

    #292 logs one up-front reason line for a job with Layer B off. On a page
    whose Layer B gate is ON, the gate itself has no reason to give (None), so
    the Scrambler job must name ``reason=scrambler`` and log no per-cut
    Layer B line.
    """
    client, tmp_path, _ = lab
    job_id, records = _scrambler_job(client, monkeypatch, tmp_path, "pb-log-scrambler", 2)
    _stub_media(monkeypatch, tmp_path, None)
    monkeypatch.setattr(cp, "transforms_disabled_reason", lambda page_id, slug: None)
    lengths = iter(record["lengthMs"] / 1000.0 for record in records)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: next(lengths))
    with caplog.at_level("INFO"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    off, per_cut = _transform_lines(caplog)
    assert len(off) == 1, off
    assert off[0].endswith(" reason=scrambler"), off
    assert f"job_id={job_id} " in off[0]
    assert per_cut == []


@pytest.mark.asyncio
async def test_scrambler_off_job_logs_layer_b_exactly_as_292(lab, monkeypatch, tmp_path, caplog):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, "pb-log-off", 2)
    _stub_media(monkeypatch, tmp_path, 8.0)
    with caplog.at_level("INFO"):
        await cp._run_dossier_source(job_id)
    assert cp._load_jobs()["jobs"][job_id]["status"] == "completed"
    off, per_cut = _transform_lines(caplog)
    assert off == []
    assert len(per_cut) == 2
    for line in per_cut:
        assert f"job_id={job_id} " in line
        assert " mirror=True motion=None reverse=True loop_swap=False layer=B applied=True" in line
