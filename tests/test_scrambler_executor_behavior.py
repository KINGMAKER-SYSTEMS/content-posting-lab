"""Behavioral Scrambler tests through the REAL enqueue endpoint and executor.

Every test drives the actual ``POST /api/control-plane/v1/jobs`` response and
``_run_dossier_source`` - no feature-helper monkeypatching that could mask a
switch-off, and no blanket autouse fixture that silently turns the Scrambler
off inside the logic under test. The autouse fixture below only isolates the
PRIVATE ledger and clears leaked env between tests; ON tests opt in by setting
the real switches inside the test body.

Coverage required by the parent review:
- ON enqueue reserves draws before planning (span = offset + length x speed),
  enriches ledger rows with the exact origin window, and fails closed without
  the authoritative originality feed (409 + reservations released).
- ON execute renders the drawn sub-window at the drawn EFFECTIVE speed while
  the receipt keeps the recipe's speed/crop/filters byte-for-byte; the grade
  treatment REALLY darkens the render arguments inside the 0.04 band.
- OFF enqueue -> ON execute stays legacy (enabling never retro-scrambles);
  ON enqueue -> OFF/deny execute stops safely with the named error and never
  renders the reserved span through the legacy path.
- Identity guards: ON adds no library/version/catalog field to the job or the
  capabilities response.
- The OFF serialization (enqueue + queued/completed jobs + capabilities + artifacts) is
  compared against a golden captured from the BASE commit (036bb75f) - actual
  serialized responses, identical fixtures, not a function compared to itself.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import scrambler as scrambler_core
from services.scrambler import (
    RELEASED,
    RENDERED,
    ScramblerHistoryCorrupt,
    scrambler_daily_report,
    scrambler_history_rows,
)
from tests.test_control_plane_source_execution import (
    _isolated_source_dna,  # noqa: F401 - autouse: main #279 isolated source-DNA registry
    MASTER_SHA,
    PAGE_ID,
    TOKEN,
    headers,
    job_body,
    lab,
    publication,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "scrambler"
OFF_GOLDEN = GOLDEN_DIR / "off-enqueue-base.json"


# ---------------------------------------------------------------------------
# Isolation (default OFF; ON tests opt in inside the test body)
# ---------------------------------------------------------------------------

SCRAMBLER_ENV_KEYS = (
    "SCRAMBLER_ENABLED", "SCRAMBLER_PAGES", "SCRAMBLER_DENY_PAGES",
    "SCRAMBLER_FAST_SHARE", "SCRAMBLER_PAGE_FAST_SHARE",
    "SCRAMBLER_TREATMENT_WEIGHTS", "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT",
)
ORIGINALITY_ENV_KEYS = (
    "CONTENT_LAB_ORIGINALITY_USAGE_PATH",
    "CONTENT_LAB_ORIGINALITY_ORIGINS_PATH",
    "CONTENT_LAB_ORIGINALITY_RETIRED_PATH",
)


@pytest.fixture(autouse=True)
def isolated_scrambler_state(monkeypatch, tmp_path):
    """Clear leaked switches, keep the private ledger and feeds inside tmp,
    and keep the ShipStream vault offline exactly like the source-execution
    suite does (unavailable manifests are handled; sockets never open).

    This never turns an ON test off: tests that exercise ON set the switches
    explicitly after this fixture has run.
    """
    import services.shipstream_source_manifest as source_manifest

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable(
            "ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)
    for key in (*SCRAMBLER_ENV_KEYS, *ORIGINALITY_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(tmp_path / "scrambler-history.json"))
    yield


def _scrambler_on(monkeypatch, *, pages=PAGE_ID):
    monkeypatch.setenv("SCRAMBLER_ENABLED", "1")
    monkeypatch.setenv("SCRAMBLER_PAGES", pages)
    # Operator acknowledgment of the Worker source-window release dependency
    # (see services/scrambler.py SCRAMBLER_WORKER_COMPAT_ENV): ON refuses
    # without it, so every ON test sets it explicitly.
    monkeypatch.setenv("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT", "1")


def _originality_feed(monkeypatch, tmp_path, usage_rows=()):
    """A real-shaped authoritative feed: usage jsonl + origins map + retired."""
    feed = tmp_path / "originality"
    feed.mkdir(exist_ok=True)
    usage = feed / "usage.jsonl"
    usage.write_text("".join(json.dumps(row) + "\n" for row in usage_rows))
    (feed / "origins.json").write_text(json.dumps({
        MASTER_SHA: {"origin_kind": "yt", "origin_id": "TestVideo-1",
                     "origin_start_s": 120.0, "origin_end_s": 301.0},
    }))
    (feed / "retired.json").write_text("[]")
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", str(usage))
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_ORIGINS_PATH", str(feed / "origins.json"))
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_RETIRED_PATH", str(feed / "retired.json"))
    return feed


def _ledger_path() -> Path:
    return Path(__import__("os").environ["SCRAMBLER_HISTORY_PATH"])


# ---------------------------------------------------------------------------
# Executor intercepts (arguments only - the executor logic itself is real)
# ---------------------------------------------------------------------------

def _wire_executor(monkeypatch, tmp_path, job_id_ref):
    """Fake the media lane but record every render/probe/transform argument.

    The executor's own decision logic - scramble detection, kill switch,
    sub-window math, grade application, manifests, receipts, ledger commits -
    all runs for real; only ffmpeg-facing helpers are replaced because their
    media behavior is proven separately by the parent's real-media benchmark.
    """
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    renders = []
    transforms = []
    split_swaps = []
    probes = []

    async def cached_source(*_args):
        return source

    async def render(src, dst, correction, **kwargs):
        renders.append({"src": str(src), "dst": str(dst), "cc": correction, **kwargs})
        Path(dst).write_bytes(b"scrambler-cut")

    def probe(path):
        probes.append(str(path))
        # The measured duration must equal the DRAWN output length: the probe
        # reads it straight from the job's own scrambler record.
        record = _job_scrambler_records(job_id_ref[0], int(Path(path).stem.split("-")[1]))
        if record is None:
            # A legacy (OFF-enqueued) cut takes main's Layer B path (#284),
            # which probes the delivered duration too: answer in bounds.
            return 8.0
        return record["lengthMs"] / 1000.0

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", probe)
    monkeypatch.setattr(cp, "apply_transforms",
                        lambda *args, **kwargs: transforms.append((args, kwargs)))
    monkeypatch.setattr(cp, "apply_split_swap",
                        lambda *args, **kwargs: split_swaps.append((args, kwargs)))
    return {"renders": renders, "transforms": transforms, "split_swaps": split_swaps,
            "probes": probes}


def _job_scrambler_records(job_id, index=None):
    job = cp._load_jobs()["jobs"][job_id]
    records = [cut.get("scrambler") for cut in job["sourceCuts"]]
    if index is None:
        return records
    return records[index]


# ---------------------------------------------------------------------------
# Enqueue ON/OFF behavior
# ---------------------------------------------------------------------------

def test_on_enqueue_reserves_draws_and_enriches_origin_windows(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-on-enqueue"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    job = cp._load_jobs()["jobs"][job_id]
    records = _job_scrambler_records(job_id)
    assert len(records) == 2 and all(isinstance(r, dict) for r in records)
    for record in records:
        # The production vocabulary: slow 0.3-0.8 on the 0.05 grid or fast
        # 1.1/1.2/1.3 ONLY (1.4 retired).
        assert record["speed"] in scrambler_core.SPEEDS
        assert record["speed"] != 1.0
        assert record["speed"] not in (1.0, 1.4)
        assert 5_000 <= record["lengthMs"] <= 9_000
        assert record["lengthMs"] % 500 == 0
        assert record["treatment"] in scrambler_core.TREATMENTS
        # The reserved span covers offset + length x effective speed BEFORE
        # the window is chosen: the planned cut duration IS that whole span.
        span = record["offsetMs"] + record["windowMs"]
        assert record["windowMs"] == round(record["lengthMs"] * record["speed"])
    cuts = job["sourceCuts"]
    for record, cut in zip(records, cuts):
        assert cut["durationMs"] == record["offsetMs"] + record["windowMs"]
    # The ledger rows carry the exact origin window of every reserved span.
    rows = scrambler_history_rows(PAGE_ID)
    job_rows = sorted((r for r in rows if r["jobId"] == job_id),
                      key=lambda r: r["cutIndex"])
    assert len(job_rows) == 2
    for row, cut, record in zip(job_rows, cuts, records):
        assert row["masterSha256"] == MASTER_SHA
        assert row["libraryStartMs"] == cut["startMs"]
        assert row["originStartMs"] == 120_000 + cut["startMs"] + record["offsetMs"]
        assert row["originEndMs"] == (120_000 + cut["startMs"] + record["offsetMs"]
                                      + record["windowMs"])
        assert row["state"] == "reserved"


def test_on_enqueue_without_originality_feed_fails_closed_and_releases(lab, monkeypatch):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    # No CONTENT_LAB_ORIGINALITY_* env: the authoritative feed is REQUIRED.
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-no-feed"),
    )
    assert response.status_code == 409
    assert "usage_path_unconfigured" in response.json()["detail"]
    assert "scrambler_unavailable" in response.json()["detail"]
    # All-or-nothing: no job was created and no reservation survives.
    assert cp._load_jobs()["jobs"] == {} or all(
        job.get("status") != "queued"
        for job in cp._load_jobs()["jobs"].values())
    assert scrambler_history_rows(PAGE_ID) == []


def test_on_enqueue_with_missing_page_handle_refuses(lab, monkeypatch):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)

    def recipe_without_handle(_publication):
        resolved = cp.resolve_source_recipe(publication())
        resolved.recipe_spec["masterPages"]["handle"] = ""
        return resolved

    monkeypatch.setattr(cp, "_dossier_source_recipe", recipe_without_handle)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-no-handle"),
    )
    assert response.status_code == 409
    assert "scrambler_page_handle_missing" in response.json()["detail"]


def test_endpoint_replay_returns_stored_draws_under_changed_config(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-replay"),
    )
    job_id = first.json()["jobId"]
    stored = json.dumps(_job_scrambler_records(job_id), sort_keys=True)
    # A later operator retune must not touch the committed operation: the
    # replay returns the same job and the same stored draws.
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "0.9")
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:0,split_swap:1,camera:0,grade:0")
    replay = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-replay"),
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json()["jobId"] == job_id
    assert json.dumps(_job_scrambler_records(job_id), sort_keys=True) == stored
    assert len([job for job in cp._load_jobs()["jobs"].values()
                if job.get("recipeId")]) == 1


def test_off_enqueue_has_no_scrambler_records_and_real_media(lab):
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-off-enqueue"),
    )
    assert response.status_code == 200, response.json()
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    for cut in job["sourceCuts"]:
        assert "scrambler" not in cut
        assert 5_000 <= cut["durationMs"] <= 11_000  # legacy 6-11 s vocabulary
    # The private ledger is never touched while OFF.
    assert not _ledger_path().exists()


# ---------------------------------------------------------------------------
# Executor ON behavior
# ---------------------------------------------------------------------------

async def _run(job_id):
    await cp._run_dossier_source(job_id)


@pytest.mark.asyncio
async def test_on_execute_renders_drawn_subwindow_at_effective_speed(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    # Deterministic non-transform treatment so the render lane itself is what
    # is under test.
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS",
                       "none:1,split_swap:0,camera:0,grade:0")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-on-execute"),
    )
    job_id = response.json()["jobId"]
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["renders"]) == 2
    records = _job_scrambler_records(job_id)
    for index, (record, cut, call) in enumerate(
            zip(records, job["sourceCuts"], lane["renders"])):
        # The drawn sub-window (offset head skipped) at the drawn speed.
        assert call["clip_start_ms"] == cut["startMs"] + record["offsetMs"]
        assert call["clip_duration_ms"] == record["windowMs"]
        assert call["playback_speed"] == record["speed"]
        # The manifest-only record carries the rendered truth.
        clip = job["clips"][index]
        assert clip["scrambler"]["renderedStartMs"] == cut["startMs"] + record["offsetMs"]
        assert clip["scrambler"]["renderedDurationMs"] == record["windowMs"]
        assert clip["scrambler"]["effectiveSpeed"] == record["speed"]
        # Provenance reports the window ACTUALLY rendered, never the span.
        assert clip["source"]["cutWindow"]["libraryStartMs"] == (
            cut["startMs"] + record["offsetMs"])
        assert clip["source"]["cutWindow"]["durationMs"] == record["windowMs"]
        assert clip["source"]["cutWindow"]["originalStartMs"] == (
            120_000 + cut["startMs"] + record["offsetMs"])
        # The receipt keeps the RECIPE's speed and crop, unchanged.
        assert clip["clipSpeed"] == pytest.approx(1.0)
        assert clip["sourceTreatment"]["visualTreatment"]["clipSpeed"] == pytest.approx(1.0)
    # Every drawn cut is committed rendered and the daily report is truthful.
    rows = [r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id]
    assert [r["state"] for r in rows] == [RENDERED, RENDERED]
    assert scrambler_daily_report(rows)["cuts"] == 2


@pytest.mark.asyncio
async def test_on_execute_grade_really_darkens_the_render_arguments(
    lab, monkeypatch, tmp_path,
):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    payload = publication(recipe_version="dossier-grade00000")
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"] = {"brightness": 0.5}
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("scrambler-register-grade"),
    ).status_code == 200
    # Only the grade treatment draws: the look 0.5 is under the 0.8 ceiling.
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS",
                       "none:0,split_swap:0,camera:0,grade:1")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("scrambler-grade-execute"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    record = _job_scrambler_records(job_id)[0]
    assert record["treatment"] == "grade"
    assert 0.004 <= record["gradeBrightnessDelta"] <= 0.040
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    (call,) = lane["renders"]
    applied = job["clips"][0]["scrambler"]["gradeBrightness"]
    # The grade is REALLY applied to the render: darker than the look, inside
    # the 0.04 admission band, on the 0.001 grid, never brighter.
    assert applied < 0.5
    assert 0.0 <= 0.5 - applied <= 0.040
    assert round(applied, 3) == applied
    assert applied >= 0.5 - record["gradeBrightnessDelta"] - 0.0005
    expected_cc = cp.filters_to_color_correction(
        {"brightness": applied})
    assert call["cc"] == expected_cc
    # The receipt keeps the recipe look: the RECIPE's brightness (0.5), never
    # the applied darker value.
    receipt = job["clips"][0]["sourceTreatment"]["visualTreatment"]
    assert receipt["filters"]["brightness"] == pytest.approx(0.5)
    receipt_text = json.dumps(job["clips"][0]["sourceTreatment"], sort_keys=True)
    assert f"{applied}" not in receipt_text


@pytest.mark.asyncio
async def test_on_execute_transform_and_split_swap_lanes_dispatch(
    lab, monkeypatch, tmp_path,
):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    feed = _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS",
                       "none:0,split_swap:1,camera:0,grade:0")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-swap-execute"),
    )
    job_id = response.json()["jobId"]
    record = _job_scrambler_records(job_id)[0]
    assert record["treatment"] == "split_swap"
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["split_swaps"]) == 1
    # The mirror dimension is drawn independently of the treatment: a
    # mirrored split_swap cut runs BOTH lanes (mirror transform, then the
    # halves swap); an unmirrored one runs the swap alone.
    assert len(lane["transforms"]) == (1 if record["mirror"] else 0)
    assert job["clips"][0]["clipTransforms"]["split_swap"] is True


# ---------------------------------------------------------------------------
# Kill switch / legacy interplay through real executor runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_off_enqueue_then_on_execute_stays_legacy(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    # Enqueue with every switch unset: a plain legacy job.
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-off-then-on"),
    )
    job_id = response.json()["jobId"]
    for cut in cp._load_jobs()["jobs"][job_id]["sourceCuts"]:
        assert "scrambler" not in cut
    # Later the operator enables the Scrambler for the page. Execution keys
    # off the job's OWN records: a legacy job must stay legacy.
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    (call,) = lane["renders"]
    cut = job["sourceCuts"][0]
    assert call["clip_start_ms"] == cut["startMs"]
    assert call["clip_duration_ms"] == cut["durationMs"]
    assert call["playback_speed"] == pytest.approx(1.0)
    assert "scrambler" not in job["clips"][0]


@pytest.mark.asyncio
async def test_on_enqueue_then_off_execute_fails_safely(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-on-then-off"),
    )
    job_id = response.json()["jobId"]
    record = _job_scrambler_records(job_id)[0]
    reserved_span = record["offsetMs"] + record["windowMs"]
    # The switch turns OFF after enqueue. The queued job must stop safely: it
    # may NEVER render the reserved span through the legacy duration/speed.
    monkeypatch.delenv("SCRAMBLER_ENABLED")
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "scrambler_disabled_at_execution"
    assert lane["renders"] == []
    # Nothing rendered: the reservations are released whole. Released rows
    # stay as tombstones (immutable identity for validation), so the
    # live check is that no row of this job remains non-released.
    rows = [r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id]
    assert [r["state"] for r in rows] == [RELEASED]
    # Sanity: the record is a real draw (window = length x effective speed).
    assert record["windowMs"] == round(record["lengthMs"] * record["speed"])


@pytest.mark.asyncio
async def test_on_enqueue_then_deny_execute_fails_safely(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-on-then-deny"),
    )
    job_id = response.json()["jobId"]
    monkeypatch.setenv("SCRAMBLER_DENY_PAGES", PAGE_ID)  # deny wins over allow
    ref = [job_id]
    lane = _wire_executor(monkeypatch, tmp_path, ref)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "scrambler_disabled_at_execution"
    assert lane["renders"] == []


@pytest.mark.asyncio
async def test_executor_failure_keeps_rendered_row_and_releases_the_rest(
    lab, monkeypatch, tmp_path,
):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS",
                       "none:1,split_swap:0,camera:0,grade:0")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-partial-fail"),
    )
    job_id = response.json()["jobId"]
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    calls = []

    async def cached_source(*_args):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append(dst)
        if len(calls) == 1:
            Path(dst).write_bytes(b"first-cut")
        else:
            raise RuntimeError("second cut encode failed")

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "apply_split_swap", lambda *a, **k: None)

    def probe(path):
        record = _job_scrambler_records(job_id, int(Path(path).stem.split("-")[1]))
        return record["lengthMs"] / 1000.0

    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", probe)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    # The first cut's rendered row survives; the failed cut is released to a
    # tombstone that keeps the job's row identity intact.
    rows = [r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id]
    assert [r["state"] for r in rows] == [RENDERED, RELEASED]
    # Durable rendered support means the actual asset survives a later failure.
    assert job["clips"] == []
    assert len(job["scramblerRenderedClips"]) == 1
    assert (Path(job["artifactRoot"]) / job["scramblerRenderedClips"][0]["path"]).is_file()
    artifacts = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID})
    assert artifacts.status_code == 200
    assert artifacts.json()["artifacts"] == []


# ---------------------------------------------------------------------------
# Worker source-window compatibility gate (release dependency)
# ---------------------------------------------------------------------------

def test_on_without_worker_compat_ack_refuses_loudly(lab, monkeypatch):
    """Switches on but no compat acknowledgment: a NAMED refusal, not a silent
    legacy fallback (the Worker today rejects >10 s true origin spans)."""
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.delenv("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-no-compat-ack"),
    )
    assert response.status_code == 409
    assert "scrambler_worker_source_window_compat_required" in response.json()["detail"]
    # Nothing was reserved and no job was created.
    assert scrambler_history_rows(PAGE_ID) == []
    assert cp._load_jobs()["jobs"] == {}


@pytest.mark.asyncio
async def test_compat_ack_dropped_before_execute_fails_safely(lab, monkeypatch):
    """A job enqueued WITH the acknowledgment must never render its reserved
    span through the legacy pipeline after the ack is dropped."""
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-ack-then-dropped"),
    )
    job_id = response.json()["jobId"]
    assert _job_scrambler_records(job_id)
    monkeypatch.delenv("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT")
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "scrambler_worker_source_window_compat_required"
    assert lane["renders"] == []
    rows = [r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id]
    assert [r["state"] for r in rows] == [RELEASED]


@pytest.mark.asyncio
async def test_off_vocabulary_speed_in_stored_record_fails_closed(lab, monkeypatch):
    """M2: a corrupt stored speed of 1.304 rounds into the 1.3 bucket but
    renders ABOVE Eric's 1.3 cap - the validator must require EXACT
    vocabulary membership, not bucket rounding, and the executor must refuse
    before any render."""
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-corrupt-speed"),
    )
    job_id = response.json()["jobId"]
    record = _job_scrambler_records(job_id)[0]
    corrupt_speed = round(record["speed"] + 0.004, 3)  # e.g. 1.3 -> 1.304
    window = round(record["lengthMs"] * corrupt_speed)
    store = cp._load_jobs()
    cut = store["jobs"][job_id]["sourceCuts"][0]
    cut["scrambler"] = {
        **cut["scrambler"], "speed": corrupt_speed,
        "windowMs": window, "spanMs": record["offsetMs"] + window,
    }
    cut["durationMs"] = record["offsetMs"] + window
    cp.atomic_save(cp._jobs_path(), store)
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert "source_recipe_cut_invalid" in (job.get("error") or "")
    assert lane["renders"] == []


# ---------------------------------------------------------------------------
# Identity guards: the Scrambler adds nothing to public identities
# ---------------------------------------------------------------------------

IDENTITY_FIELDS = (
    "sourceLibraryId", "sourceLibraryHash", "engineRegistryHash",
    "engineProfileHash", "formatContractVersion", "executorVersion",
    "materialSource", "assetType", "dossierRevision", "recipeSpecHash",
    "recipeId", "engine", "recipeVersion",
)


def test_on_job_preserves_every_library_and_version_identity(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    off = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-identity-off"),
    ).json()["jobId"]
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    on = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-identity-on"),
    ).json()["jobId"]
    jobs = cp._load_jobs()["jobs"]
    for field in IDENTITY_FIELDS:
        assert jobs[on][field] == jobs[off][field], field


def test_capabilities_response_is_identical_with_switches_on(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-cap-off"),
    )
    off_serialized = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).content
    # Enqueue an ON job too, then read capabilities again with the switches
    # on: the serialized response must be byte-identical (a queued ON job
    # reserves the same windows OFF logic reserves; no scrambler field leaks
    # into the capability shape).
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("scrambler-cap-on"),
    )
    on_serialized = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).content
    assert on_serialized == off_serialized


# ---------------------------------------------------------------------------
# OFF serialized golden, base vs candidate (raw responses, same fixtures)
# ---------------------------------------------------------------------------

VOLATILE_KEYS = {
    "createdAt", "completedAt", "updatedAt", "startedAt", "runtimeId",
    "lastCheckedAt",
}


def _normalize(value, tmp: str):
    if isinstance(value, dict):
        return {key: _normalize(item, tmp)
                for key, item in sorted(value.items()) if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_normalize(item, tmp) for item in value]
    if isinstance(value, str):
        # macOS /var temporary roots may resolve under /private/var.
        # Replace only the run-specific prefix, preserving every path suffix.
        prefixes = {tmp, str(Path(tmp).resolve())} if tmp else set()
        for prefix in sorted(prefixes, key=len, reverse=True):
            value = value.replace(prefix, "<TMP>")
        return value
    return value


def _off_serialization(client, tmp_path, monkeypatch):
    """Actual queued/completed OFF jobs and enqueue/capability/artifact responses."""
    # The job id is normally random (secrets.token_hex) and seeds the cut
    # plan, so two runs of the same code pick different windows. Pin it (as
    # the base golden generator does) so only real base-vs-head differences
    # can fail this comparison, never per-run randomness.
    monkeypatch.setattr(cp._secrets, "token_hex", lambda n: "f" * (2 * n))
    monkeypatch.setattr(cp._secrets, "token_urlsafe", lambda n: "g" * n)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("scrambler-off-golden"),
    )
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    stored = cp._load_jobs()["jobs"][job_id]
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).json()
    from tests.golden.scrambler.generate_base_golden import complete_off_job

    completed = complete_off_job(cp, tmp_path, job_id, monkeypatch)
    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=headers(),
    )
    assert artifacts.status_code == 200, artifacts.text
    assert len(artifacts.json()["artifacts"]) == 2
    completed_response = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=headers(),
    )
    assert completed_response.status_code == 200, completed_response.text
    assert completed_response.json()["status"] == "completed"
    return {
        "response": _normalize(response.json(), str(tmp_path)),
        "job": _normalize(stored, str(tmp_path)),
        "capabilities": _normalize(capabilities, str(tmp_path)),
        "completedJob": _normalize(completed, str(tmp_path)),
        "completedResponse": _normalize(completed_response.json(), str(tmp_path)),
        "artifacts": _normalize(artifacts.json(), str(tmp_path)),
    }


def test_off_enqueue_serialization_matches_base_golden(lab, tmp_path, monkeypatch):
    """The OFF surface is byte-identical to the BASE commit under identical
    fixtures. The golden was re-captured from Lab main 036bb75f (the rebase
    base) with the same normalization
    (see tests/golden/scrambler/generate_base_golden.py); volatile fields
    (timestamps/runtime ids) are removed and only the tmp root is replaced
    on BOTH sides; paths, artifactRoot and thumbnail claims remain compared."""
    client, tmp_path, _ = lab
    captured = _off_serialization(client, tmp_path, monkeypatch)
    assert OFF_GOLDEN.is_file(), (
        "tests/golden/scrambler/off-enqueue-base.json is missing: capture it "
        "from the base commit with generate_base_golden.py")
    golden = json.loads(OFF_GOLDEN.read_text())
    # Runtime ownership adds one private coordination marker to stored jobs.
    # Pin its exact value, then retain the BASE comparison for every original
    # stored field and every public enqueue/status/capability/artifact byte.
    for phase in ("job", "completedJob"):
        assert type(captured[phase]["sourceClaimVersion"]) is int
        assert captured[phase].pop("sourceClaimVersion") == 1
    assert captured == golden


# ---------------------------------------------------------------------------
# Defense-in-depth pins: the 1/15 hash gate alone cannot be trusted to hold
# the reverse budget, so the cumulative cap and the ET-day cap must each
# refuse on their own. Both tests force the gate fully open (REVERSE_GATE=1)
# to isolate the defense under test.
# ---------------------------------------------------------------------------

def _prime_rendered(page, count, *, day0, monkeypatch):
    """`count` rendered non-reverse rows on consecutive days (gate closed)."""
    monkeypatch.setattr(scrambler_core, "REVERSE_GATE", 0.0)
    for i in range(count):
        day = (day0 + timedelta(days=i)).isoformat()
        job = f"prime-{i}"
        assert scrambler_core.reserve_job_cuts(page, job, 1, now=day)[0] \
            .treatment != "reverse"
        scrambler_core.mark_cut_rendered(
            page, job, 0, asset_sha256=f"{'p' * 8}{i:056d}", rendered_at=day)


def test_reverse_cumulative_cap_refuses_without_gate_help(tmp_path, monkeypatch):
    """With the gate fully open, the CUMULATIVE cap alone must hold reverses
    to (reverses + 1) x 15 <= rendered + 1 (denominator pinned at 15)."""
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(tmp_path / "h.json"))
    page = "acct:cap-pin"
    day0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    _prime_rendered(page, 32, day0=day0, monkeypatch=monkeypatch)
    # Two supported reverses on their own days, each followed by a fresh
    # non-reverse render so the previous-treatment rule stays satisfied.
    pattern = ["open", "closed", "open", "closed", "open"]
    reverses = 0
    for step, state in enumerate(pattern):
        day = (day0 + timedelta(days=40 + step)).isoformat()
        job = f"step-{step}"
        monkeypatch.setattr(scrambler_core, "REVERSE_GATE",
                            1.0 if state == "open" else 0.0)
        draw = scrambler_core.reserve_job_cuts(page, job, 1, now=day)[0]
        if state == "open":
            if draw.treatment == "reverse":
                reverses += 1
            # After 2 committed reverses over 34 rendered cuts, a THIRD
            # reverse needs 3 x 15 <= 35 which the pinned denominator
            # refuses: steps 0 and 2 may be reverse, step 4 must NOT be.
            if step == 4:
                assert draw.treatment != "reverse"
        else:
            assert draw.treatment != "reverse"
        scrambler_core.mark_cut_rendered(
            page, job, 0, asset_sha256=f"{'c' * 8}{step:056d}", rendered_at=day)
    assert reverses == 2


def test_reverse_et_day_cap_refuses_second_same_day_reverse(
    tmp_path, monkeypatch,
):
    """With the gate fully open and cap support abundant, ONE ET day still
    admits at most one reverse."""
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(tmp_path / "h.json"))
    page = "acct:day-pin"
    day0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    _prime_rendered(page, 32, day0=day0, monkeypatch=monkeypatch)
    monkeypatch.setattr(scrambler_core, "REVERSE_GATE", 1.0)
    same_day = (day0 + timedelta(days=40)).isoformat()
    draws = scrambler_core.reserve_job_cuts(page, "same-day", 4, now=same_day)
    same_day_reverses = sum(d.treatment == "reverse" for d in draws)
    assert same_day_reverses <= 1
