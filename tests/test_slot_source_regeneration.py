"""Exact original-master slot-source repair: wire, authority, queue and render."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import post_render_jobs as jobs
from services import slot_source_regeneration as regen
from services.post_render import sha256
from services.source_treatment import normalized_visual_treatment
from tests.test_control_plane_source_execution import (  # noqa: F401  (pytest fixtures)
    PAGE_ID, _probe_duration, headers, job_body, lab, publication,
)
from tests.test_post_render import NOW, STYLE, request as render_request
from tests.test_post_render_jobs import client_for, submission

TOKEN = "only-this-test-service-token"
PARENT_GRADE = {"brightness": 0.7}
DESIRED_GRADE = {"brightness": 0.5}
DESIRED_CROP = {"zoom": 1.63, "focusX": 0.5, "focusY": 0.5}
WINDOW_START_MS, WINDOW_DURATION_MS = 4_000, 6_000
TREATMENT_KEYS = ("filters", "clipSpeed", "clipCrop")


def graded_publication(brightness, version="dossier-slotrepair0000"):
    """The registered page recipe with one real grade (never relabelled later)."""
    import hashlib

    payload = publication(recipe_version=version)
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"] = dict(brightness)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return payload


def _visual(filters, crop=None, speed=1.0):
    return normalized_visual_treatment({
        "filters": dict(filters), "clipSpeed": speed, "clipCrop": crop or DESIRED_CROP,
    })


def _treatments():
    desired = _visual(DESIRED_GRADE)
    actual = _visual(PARENT_GRADE)
    desired_json = json.dumps(desired, sort_keys=True, separators=(",", ":"))
    actual_json = json.dumps(actual, sort_keys=True, separators=(",", ":"))
    return {
        # The stored request asks for .5; the delivered source bytes are .7.
        "render_treatment_json": desired_json,
        "treatment_sha256": sha256(desired_json.encode()),
        "source_visual_treatment_json": actual_json,
        "source_visual_treatment_sha256": sha256(actual_json.encode()),
    }


def _write_master(path: Path) -> Path:
    """15 s of light gray with black marker bands at the planned window's ends."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for real render proof")
    overlay = (
        "[1:v]format=rgba,colorchannelmixer=aa=0.999[mark];"
        "[0:v][mark]overlay=enable='between(t,4,4.4)+between(t,9.6,10)'"
    )
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "color=c=0xE6E6E6:s=320x568:rate=30",
        "-f", "lavfi", "-i", "color=c=black:s=320x568:rate=30",
        "-filter_complex", overlay, "-t", "15",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "30", str(path),
    ], check=True, capture_output=True, text=True, timeout=120)
    return path


def _frame_mean(path: Path, seconds: float) -> float:
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True, timeout=60,
    )
    pixels = completed.stdout
    assert pixels, "no frame decoded"
    return sum(pixels) / len(pixels)


@pytest.fixture
def source_parent(lab, monkeypatch, tmp_path):
    """A completed parent generation job produced by the real source executor."""
    client, _cp_tmp, _started = lab
    master = _write_master(tmp_path / "master.mp4")
    payload = graded_publication(PARENT_GRADE)
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("slot-repair-register"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slot-repair-parent"),
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    for cut in store["jobs"][job_id]["sourceCuts"]:
        cut.update({
            "slotId": f"{cut['masterSha256']}:{WINDOW_START_MS}:{WINDOW_DURATION_MS}",
            "libraryStartMs": WINDOW_START_MS,
            "startMs": WINDOW_START_MS,
            "durationMs": WINDOW_DURATION_MS,
        })
    cp.atomic_save(cp._jobs_path(), store)

    async def cached_source(*_args):
        return master

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    import asyncio

    asyncio.run(cp._run_dossier_source(job_id))
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = Path(job["artifactRoot"]) / job["clips"][0]["path"]
    return {
        "client": client, "job": job, "job_id": job_id, "clip": clip,
        "master": master, "master_pages_root": cp._generation_root(),
    }


@pytest.fixture(autouse=True)
def offline_shipstream_manifest(monkeypatch):
    import services.shipstream_source_manifest as source_manifest

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable("ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)


def _wire_treatment(filters, crop):
    """The complete stored request treatment (caption style is not source treatment)."""
    return {"stylePreset": "treated-source-test", "filters": dict(filters),
            "captionStyle": STYLE, "clipSpeed": 1.0, "clipCrop": dict(crop)}


def _submission(source_sha, page_id=PAGE_ID):
    """The real Worker-sent submission shape, bound to one delivered source SHA."""
    from services.post_render import PostRenderRequest

    request_treatment = _wire_treatment(DESIRED_GRADE, DESIRED_CROP)
    treatment = json.dumps(request_treatment, sort_keys=True, separators=(",", ":"))
    visual = json.dumps(normalized_visual_treatment(
        _wire_treatment(PARENT_GRADE, DESIRED_CROP)), sort_keys=True, separators=(",", ":"))
    payload = {
        "schema": "content-lab.post-render-request.v1",
        # The slot identity is unique per source fixture: a different source is
        # a different slot, exactly as the Worker's immutable slot payload is.
        "slot_id": f"slot:slotrepair-{source_sha[:12]}", "slot_payload_sha256": "b" * 64,
        "page_id": page_id,
        "program_id": "playlist:fixture", "device_serial": "fixture-only",
        "account": "fixture.account", "source_sha256": source_sha,
        "caption": "already treated", "caption_sha256": sha256(b"already treated"),
        "render_treatment_json": treatment, "treatment_sha256": sha256(treatment.encode()),
        "source_visual_treatment_json": visual,
        "source_visual_treatment_sha256": sha256(visual.encode()),
        "renderer_id": "content-lab.prepared-post", "renderer_version": "1",
        "created_at_ms": NOW - 1000,
    }
    slot = {"schema_version": 4, "slot_id": payload["slot_id"], "page_id": payload["page_id"],
            "handle": payload["account"], "asset": {"sha256": source_sha},
            "device_hint": {"device_serial": payload["device_serial"]},
            "caption": {"text": payload["caption"]},
            "render_treatment": request_treatment}
    raw = json.dumps(slot, separators=(",", ":"))
    payload["slot_payload_sha256"] = sha256(raw.encode())
    request = PostRenderRequest.model_validate(payload)
    return jobs.RenderJobSubmission.model_validate({
        "schema": jobs.JOB_SCHEMA, "request": request, "slot_payload_json": raw,
        "source_provenance": {
            "schema": "posting-source-treatment/v1",
            "source_sha256": request.source_sha256,
            "source_visual_treatment_sha256": request.source_visual_treatment_sha256,
            "provenance_id": "generation:slot-source-repair-fixture",
        },
    })


def _prepared(worker, source_sha, page_id=PAGE_ID):
    """Submit the stored prepared-post request that needs a source regeneration."""
    created = worker.enqueue(
        _submission(source_sha, page_id), f"prepared-slot-repair-{source_sha[:8]}")
    assert created["state"] == "regeneration_needed", created
    assert created["attempts"] == 0
    return created


def _post(worker, monkeypatch):
    return client_for(worker, monkeypatch)


def _repair_headers(job_id, *, page_id=PAGE_ID, parent_id="cpl0000000000000000", key="slot-repair-command-1"):
    return {
        "Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": page_id,
        "Idempotency-Key": key,
    }, {"schema": regen.REQUEST_SCHEMA, "parent_generation_job_id": parent_id}


def test_regenerate_source_route_requires_auth_page_and_closed_body(source_parent, tmp_path, monkeypatch):
    worker = jobs.PostRenderJobs(tmp_path / "post", fetcher=lambda *_: pytest.fail("no media IO"),
                                clock_ms=lambda: NOW)
    client = _post(worker, monkeypatch)
    prepared = _prepared(worker, source_parent["job"]["clips"][0]["sha256"])
    url = f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source"
    good_headers, body = _repair_headers(prepared["id"], parent_id=source_parent["job_id"])

    assert client.post(url, json=body, headers={**good_headers, "Authorization": ""}).status_code == 401
    assert client.post(url, json=body, headers={**good_headers, "X-RT-Page-Id": "tt-other-page"}).status_code == 404
    assert client.post(url, json=body, headers={**good_headers, "Idempotency-Key": ""}).status_code == 400
    assert client.post(url, json={**body, "source_url": "https://example.test/a.mp4"},
                       headers=good_headers).status_code == 422
    assert client.post(url, json={"schema": "content-lab.source-regeneration-request.v0"},
                       headers=good_headers).status_code == 422
    # Never reached the queue: the submission stays exactly as the Worker sent it.
    assert worker.status(prepared["id"])["state"] == "regeneration_needed"
    assert regen.pending_command(worker._row(prepared["id"])) is None


@pytest.mark.parametrize("case", [
    "foreign_page", "not_completed", "source_hash", "forged_cut", "two_clips", "no_provenance",
])
def test_regenerate_source_refuses_unprovable_parent_before_media_io(
    source_parent, tmp_path, monkeypatch, case,
):
    worker = jobs.PostRenderJobs(tmp_path / "post", fetcher=lambda *_: pytest.fail("no media IO"),
                                clock_ms=lambda: NOW, source_regenerator=fail_regenerator)
    client = _post(worker, monkeypatch)
    parent_id = source_parent["job_id"]
    source_sha = source_parent["job"]["clips"][0]["sha256"]
    prepared = _prepared(worker, source_sha)
    store = cp._load_jobs()
    parent = store["jobs"][parent_id]
    if case == "not_completed":
        parent["status"] = "failed"
        parent["error"] = "provider_generation_failed"
        cp.atomic_save(cp._jobs_path(), store)
    elif case == "forged_cut":
        parent["clips"][0]["source"]["cutWindow"]["libraryStartMs"] = WINDOW_START_MS + 500
        parent["clips"][0]["source"]["cutWindow"]["libraryEndMs"] = (
            WINDOW_START_MS + 500 + WINDOW_DURATION_MS)
        cp.atomic_save(cp._jobs_path(), store)
    elif case == "two_clips":
        parent["clips"].append(dict(parent["clips"][0]))
        cp.atomic_save(cp._jobs_path(), store)
    elif case == "no_provenance":
        parent["clips"][0].pop("source")
        cp.atomic_save(cp._jobs_path(), store)
    expected_page = "tt-other-page" if case == "foreign_page" else PAGE_ID
    if case == "source_hash":
        prepared = _prepared(worker, "b" * 64, page_id=PAGE_ID)
    headers_now, body = _repair_headers(prepared["id"], page_id=expected_page, parent_id=parent_id)
    response = client.post(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source",
        json=body, headers=headers_now,
    )
    if case == "foreign_page":
        assert response.status_code == 404
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"].startswith("source_regeneration_")
    row = worker._row(prepared["id"])
    assert row["state"] == "regeneration_needed"
    assert row["attempts"] == 0
    assert regen.pending_command(row) is None


class FailRenderer:
    def __call__(self, *_args, **_kwargs):
        raise AssertionError("the prepared renderer must never run the source lane")


def fail_regenerator(*_args, **_kwargs):
    raise AssertionError("the source lane must not run for a refused parent")


def fake_regenerator(command, work_dir, *, job_id, attempt_id):
    work_dir.mkdir(parents=True, exist_ok=True)
    path = work_dir / regen.ARTIFACT_NAME
    path.write_bytes(b"repaired:" + job_id.encode() + b":" + attempt_id.encode())
    digest = sha256(path.read_bytes())
    return {
        "schema": regen.ENVELOPE_SCHEMA,
        "receipt": {
            "schema": regen.RECEIPT_SCHEMA,
            "regeneration_job_id": job_id,
            "attempt_id": attempt_id,
            "page_id": command["pageId"],
            "slot_id": command["slotId"],
            "slot_payload_sha256": command["slotPayloadSha256"],
            "parent_generation_job_id": command["parentGenerationJobId"],
            "parent_source_sha256": command["parentSourceSha256"],
            "parent_recipe_id": command["parentRecipeId"],
            "parent_recipe_version": command["parentRecipeVersion"],
            "master_sha256": command["master"]["sha256"],
            "source_library_id": command["sourceLibraryId"],
            "source_library_version": command["sourceLibraryVersion"],
            "cut": dict(command["cut"]),
            "visual_treatment_json": command["requestedVisualJson"],
            "visual_treatment_sha256": command["requestedVisualSha256"],
            "source_sha256": digest,
            "completed_at_ms": NOW,
        },
        "artifact": {
            "sha256": digest,
            "byte_length": path.stat().st_size,
            "object_key": regen.object_key(job_id, attempt_id),
            "mime_type": regen.ARTIFACT_MIME_TYPE,
            "width": 1080, "height": 1920, "duration_ms": WINDOW_DURATION_MS,
        },
    }


def _queued_worker(tmp_path, monkeypatch, source_parent, *, regenerator=fake_regenerator):
    worker = jobs.PostRenderJobs(
        tmp_path / "post", renderer=FailRenderer(),
        fetcher=lambda *_: pytest.fail("the prepared source grant must not be fetched"),
        clock_ms=lambda: NOW, source_regenerator=regenerator,
    )
    client = _post(worker, monkeypatch)
    prepared = _prepared(worker, source_parent["job"]["clips"][0]["sha256"])
    headers_now, body = _repair_headers(prepared["id"], parent_id=source_parent["job_id"])
    response = client.post(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source",
        json=body, headers=headers_now,
    )
    assert response.status_code == 202, response.text
    return worker, client, prepared, headers_now, body


def test_queue_is_atomic_for_one_parent_and_replays_the_same_key(source_parent, tmp_path, monkeypatch):
    worker = jobs.PostRenderJobs(
        tmp_path / "post", renderer=FailRenderer(),
        fetcher=lambda *_: pytest.fail("the prepared source grant must not be fetched"),
        clock_ms=lambda: NOW, source_regenerator=fake_regenerator,
    )
    client = _post(worker, monkeypatch)
    prepared = _prepared(worker, source_parent["job"]["clips"][0]["sha256"])
    url = f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source"
    headers_now, body = _repair_headers(prepared["id"], parent_id=source_parent["job_id"])

    first = client.post(url, json=body, headers=headers_now)
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "queued"
    command = regen.pending_command(worker._row(prepared["id"]))
    assert command["parentGenerationJobId"] == source_parent["job_id"]
    assert command["cut"] == {"start_ms": WINDOW_START_MS,
                              "end_ms": WINDOW_START_MS + WINDOW_DURATION_MS,
                              "duration_ms": WINDOW_DURATION_MS}
    # The immutable submission is untouched and no final render is claimed.
    row = worker._row(prepared["id"])
    stored = json.loads(row["submission_json"])["request"]["render_treatment_json"]
    assert stored == _submission(source_parent["job"]["clips"][0]["sha256"]).request.render_treatment_json
    assert json.loads(stored)["filters"] == DESIRED_GRADE

    replay = client.post(url, json=body, headers=headers_now)
    assert replay.status_code == 202
    assert replay.json()["state"] == "queued"
    other_key = client.post(url, json=body, headers={**headers_now, "Idempotency-Key": "slot-repair-command-2"})
    assert other_key.status_code == 409
    assert other_key.json()["detail"] == "source_regeneration_pending"
    # A provenance revision may not hijack the queued repair.
    with pytest.raises(jobs.RenderJobError) as blocked:
        worker.provide_provenance(prepared["id"], _submission("e" * 64, PAGE_ID))
    assert blocked.value.code == "source_regeneration_pending"
    # Nor may a fresh idempotency key reset the queued repair's attempts.
    with pytest.raises(jobs.RenderJobError) as reset:
        worker.enqueue(_submission(source_parent["job"]["clips"][0]["sha256"], PAGE_ID),
                       "fresh-key-must-not-reset")
    assert reset.value.code == "source_regeneration_pending"


def test_completed_repair_serves_exact_source_bytes_and_stays_regeneration_needed(
    source_parent, tmp_path, monkeypatch,
):
    worker, client, prepared, headers_now, _body = _queued_worker(
        tmp_path, monkeypatch, source_parent)
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "regeneration_needed"
    assert "artifacts" not in job and "final_sha256" not in job
    envelope = job["source_regeneration"]
    assert envelope["schema"] == regen.ENVELOPE_SCHEMA
    receipt = envelope["receipt"]
    assert receipt["parent_generation_job_id"] == source_parent["job_id"]
    assert receipt["parent_recipe_id"] == "pov-dirt-bike:master"
    assert receipt["master_sha256"] == source_parent["job"]["clips"][0]["source"]["master"]["sha256"]
    assert receipt["cut"]["start_ms"] == WINDOW_START_MS

    download = client.get(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/artifacts/source",
        headers=headers_now,
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert sha256(download.content) == envelope["artifact"]["sha256"]
    assert len(download.content) == envelope["artifact"]["byte_length"]
    # A restart whose state write never landed resumes the same attempt from its
    # already-published envelope instead of rendering the master again.
    calls = []
    real = worker.source_regenerator

    def counting(command, work_dir, *, job_id, attempt_id):
        calls.append(attempt_id)
        return real(command, work_dir, job_id=job_id, attempt_id=attempt_id)

    worker.source_regenerator = counting
    row = worker._row(prepared["id"])
    with worker._db() as db:
        db.execute("UPDATE jobs SET state='running',available_at_ms=?,updated_at_ms=? WHERE id=?",
                   (NOW - 1, NOW, prepared["id"]))
        db.execute("UPDATE attempts SET state='running',ended_at_ms=NULL WHERE id=?", (row["attempt_id"],))
    assert worker.run_one()
    assert calls == []
    resumed = worker.status(prepared["id"])
    assert resumed["state"] == "regeneration_needed"
    assert resumed["source_regeneration"] == envelope
    bad = client.get(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/artifacts/source",
        headers={**headers_now, "X-RT-Page-Id": "tt-other-page"},
    )
    assert bad.status_code == 404
    worker.artifact(prepared["id"], "source").write_bytes(b"tampered")
    assert client.get(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/artifacts/source",
        headers=headers_now,
    ).status_code == 409
    assert not worker.run_one()


def test_failed_repair_retains_the_command_for_the_existing_retry_door(
    source_parent, tmp_path, monkeypatch,
):
    calls = []

    def broken(command, work_dir, *, job_id, attempt_id):
        calls.append(attempt_id)
        raise jobs.RenderJobError("source_regeneration_decode_unavailable")

    worker, client, prepared, headers_now, _body = _queued_worker(
        tmp_path, monkeypatch, source_parent, regenerator=broken)
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "failed"
    assert job["error_code"] == "source_regeneration_decode_unavailable"
    assert job["retryable"] is True
    assert regen.pending_command(worker._row(prepared["id"])) is not None
    worker.source_regenerator = fake_regenerator
    retried = client.post(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/retry", headers=headers_now)
    assert retried.status_code == 202
    assert worker.run_one()
    assert worker.status(prepared["id"])["state"] == "regeneration_needed"
    assert len(calls) == 1


def test_ordinary_prepared_jobs_never_enter_the_source_lane(tmp_path, monkeypatch):
    rendered = []

    def render(source, output, request, **_kwargs):
        rendered.append(request.slot_id)
        from tests.test_post_render_jobs import _write_fake_render
        _write_fake_render(output, request, clock_ms=lambda: NOW, final=b"final")

    worker = jobs.PostRenderJobs(
        tmp_path / "post", renderer=render,
        fetcher=lambda _request, path: path.write_bytes(b"source"),
        clock_ms=lambda: NOW, source_regenerator=fail_regenerator,
    )
    created = worker.enqueue(submission(), "ordinary-prepared-job")
    assert created["state"] == "queued"
    assert worker.run_one()
    assert worker.status(created["id"])["state"] == "succeeded"
    assert rendered == [created["slot_id"]]
    assert "source_regeneration" not in worker.status(created["id"])


def test_real_repair_cuts_the_same_window_from_the_original_master_once(
    source_parent, tmp_path, monkeypatch, source_loop,
):
    """The delivered bytes are the master window @ .5, never the .7 cut regraded.

    The parent fixture registers a synthetic master, so the bytes here stand in
    for that registered master file: the command's master identity is replaced
    with the fixture file's own SHA/bytes before the cut. Registered-master
    authority binding is proved separately against the captured real shapes.
    """
    master = source_parent["master"]
    master_sha = sha256(master.read_bytes())
    master_bytes = master.stat().st_size
    clip = source_parent["job"]["clips"][0]
    request = _submission(clip["sha256"], PAGE_ID).request
    command = regen.resolve_parent_authority(
        page_id=PAGE_ID, parent_generation_job_id=source_parent["job_id"], request=request,
    )
    assert command["parentSourceSha256"] == request.source_sha256
    assert command["requestedVisualSha256"] == sha256(command["requestedVisualJson"].encode())
    command["submissionSha256"] = sha256(command["requestedVisualJson"].encode())
    command["master"].update({"sha256": master_sha, "bytes": master_bytes})

    cache = source_parent["master_pages_root"] / "_source_dna"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cached = cache / f"{master_sha}.mp4"
    shutil.copyfile(master, cached)
    before = (cached.stat().st_size, sha256(cached.read_bytes()))

    root = tmp_path / "repair"
    work_dir = root / "attempts" / "render_fixture" / ("a" * 32) / "source-regeneration"
    envelope = regen.regenerate(command, work_dir, job_id="render_fixture",
                                attempt_id="a" * 32, clock_ms=lambda: NOW, loop=source_loop)
    artifact, receipt = envelope["artifact"], envelope["receipt"]
    path = regen.artifact_path(root, "render_fixture", "a" * 32)
    assert regen.verify_envelope(envelope, root=root, job_id="render_fixture",
                                 attempt_id="a" * 32) == path
    assert (artifact["width"], artifact["height"]) == (1080, 1920)
    assert abs(artifact["duration_ms"] - WINDOW_DURATION_MS) <= 34
    assert _probe_duration(path) == pytest.approx(6.0, abs=0.05)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.split() == ["video"], "the repaired clip must be silent"

    # The exact planned window: black marker bands only at 4.0-4.4 s and 9.6-10 s.
    assert _frame_mean(path, 0.2) < 25
    assert _frame_mean(path, 5.8) < 25
    light = _frame_mean(path, 3.0)
    assert 85 <= light <= 120, light
    # The parent cut is the same window graded at .7; regrading it would land far lower.
    parent_light = _frame_mean(source_parent["clip"], 3.0)
    assert parent_light == pytest.approx(153, abs=20), parent_light
    assert parent_light - light > 30

    desired = normalized_visual_treatment(json.loads(receipt["visual_treatment_json"]))
    assert set(desired) == set(TREATMENT_KEYS)
    assert desired["filters"]["brightness"] == 0.5
    assert desired["clipCrop"]["zoom"] == 1.63
    assert receipt["visual_treatment_sha256"] == sha256(receipt["visual_treatment_json"].encode())
    assert receipt["cut"] == {"start_ms": WINDOW_START_MS,
                              "end_ms": WINDOW_START_MS + WINDOW_DURATION_MS,
                              "duration_ms": WINDOW_DURATION_MS}
    assert receipt["source_sha256"] == artifact["sha256"] == sha256(path.read_bytes())
    assert receipt["master_sha256"] == master_sha
    # The verified cache is read-only evidence and is never mutated.
    assert (cached.stat().st_size, sha256(cached.read_bytes())) == before
    # No scratch master survives the attempt.
    assert sorted(entry.name for entry in path.parent.iterdir()) == [regen.ARTIFACT_NAME]


REAL_PARENT_SOURCE = Path("/tmp/warner-real-parent-source-authority-1021.json")
REAL_PARENT_RECIPE = Path("/tmp/warner-real-parent-recipe-authority-1021.json")
REAL_PARENT_ID = "cpl-dc0287c3a8d95056"


@pytest.fixture
def real_authority(monkeypatch, tmp_path):
    """The exact captured a323 parent jobs plus their registered recipes.

    Only file/registry transport is stubbed: the job store and the registered
    recipe record are the captured production bytes, and `_current_master_pages_intent`
    answers from that captured intent (the live Notion read is not available
    here). The registered source-library manifest and the cut planner run for real.
    """
    if not (REAL_PARENT_SOURCE.is_file() and REAL_PARENT_RECIPE.is_file()):
        pytest.skip("captured production parent authority is not available")
    store = json.loads(REAL_PARENT_SOURCE.read_text())
    records = json.loads(REAL_PARENT_RECIPE.read_text())["records"]
    monkeypatch.setattr(cp, "_load_jobs", lambda: {"jobs": store["jobs"]})
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")

    def registered(page_id, recipe_id, engine, recipe_version):
        for record in (row["registeredRecord"] for row in records.values()):
            if (record["pageId"], record["recipeId"], record["engine"], record["recipeVersion"]) == (
                page_id, recipe_id, engine, recipe_version):
                return record
        raise AssertionError("registered recipe is not in the captured authority")

    monkeypatch.setattr(cp, "load_registered_recipe", registered)

    def current(page_id, asserted=None):
        job = store["jobs"][REAL_PARENT_ID]
        intent = cp.exact_intent(job["masterPages"], job["masterPagesHash"], expected_page_id=page_id)
        return None if intent is None or page_id != job["pageId"] else (intent, job["masterPagesHash"])

    monkeypatch.setattr(cp, "_current_master_pages_intent", current)
    return store["jobs"][REAL_PARENT_ID]


def test_real_captured_parent_resolves_the_exact_original_master_window(real_authority):
    job = real_authority
    clip = job["clips"][0]
    lineage = clip["source"]
    assert len(job["clips"]) == len(job["sourceCuts"]) == 10
    request = _submission(clip["sha256"], page_id=job["pageId"]).request
    command = regen.resolve_parent_authority(
        page_id=job["pageId"], parent_generation_job_id=REAL_PARENT_ID, request=request,
    )
    assert command["schema"] == regen.COMMAND_SCHEMA
    assert command["parentGenerationJobId"] == REAL_PARENT_ID
    assert command["parentRecipeId"] == "pov-night-core:master"
    assert command["parentRecipeVersion"] == job["recipeVersion"] == "dossier-30e6bbaffb8d18ca"
    assert command["sourceLibraryId"] == job["sourceLibraryId"]
    assert command["sourceLibraryVersion"] == lineage["sourceLibraryVersion"]
    assert command["master"]["sha256"] == (
        "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314")
    assert command["master"]["bytes"] == lineage["master"]["bytes"]
    # The cut is the master-local planned window, not a caller-supplied one.
    assert command["cut"] == {
        "start_ms": lineage["cutWindow"]["libraryStartMs"],
        "end_ms": lineage["cutWindow"]["libraryEndMs"],
        "duration_ms": lineage["cutWindow"]["durationMs"],
    }
    # The visual comes from the stored request, never from the parent recipe.
    assert normalized_visual_treatment(json.loads(command["requestedVisualJson"]))["filters"][
        "brightness"] == DESIRED_GRADE["brightness"]


def test_real_captured_parent_refuses_a_foreign_page_and_a_rebound_window(real_authority, monkeypatch):
    job = real_authority
    clip = job["clips"][0]
    request = _submission(clip["sha256"], page_id=job["pageId"]).request
    with pytest.raises(regen.SourceRegenerationError) as foreign:
        regen.resolve_parent_authority(
            page_id="acct:some-other-page", parent_generation_job_id=REAL_PARENT_ID, request=request,
        )
    assert foreign.value.code == "source_regeneration_parent_foreign_page"
    # The stored clip no longer matches a planned window of that master.
    rebound = json.loads(json.dumps(job))
    # Keep local/original provenance internally consistent while selecting a
    # window absent from the immutable parent sourceCuts.
    window = rebound["clips"][0]["source"]["cutWindow"]
    for field in ("libraryStartMs", "libraryEndMs", "originalStartMs", "originalEndMs"):
        window[field] += 333
    monkeypatch.setattr(cp, "_load_jobs", lambda: {"jobs": {REAL_PARENT_ID: rebound}})
    with pytest.raises(regen.SourceRegenerationError) as refused:
        regen.resolve_parent_authority(
            page_id=job["pageId"], parent_generation_job_id=REAL_PARENT_ID, request=request,
        )
    assert refused.value.code == "source_regeneration_cut_unplanned"


# ---------------------------------------------------------------------------
# Subsequent-human-edit ancestry: a prior render_<id> parent walks back to the
# terminal Control Plane job, keeping the original master/window as authority.
# ---------------------------------------------------------------------------

import asyncio
import threading

RENDER_ID = "render_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def source_loop():
    """A long-lived loop in its own thread, like the ASGI loop in production."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _terminal_identity(source_parent):
    clip = source_parent["job"]["clips"][0]
    lineage = clip["source"]
    return {
        "master_sha": lineage["master"]["sha256"],
        "cut": {"start_ms": WINDOW_START_MS,
                "end_ms": WINDOW_START_MS + WINDOW_DURATION_MS,
                "duration_ms": WINDOW_DURATION_MS},
        "clip_sha": clip["sha256"],
        "cp_id": source_parent["job_id"],
    }


def _ancestor_envelope(source_parent, *, source_sha, parent_generation_job_id,
                       parent_source_sha, master_sha, cut, page_id=PAGE_ID,
                       job_id=RENDER_ID, attempt_id=None, slot_id="slot:ancestor",
                       slot_hash=None):
    attempt_id = attempt_id or ("c" * 32)
    slot_hash = slot_hash or ("b" * 64)
    return {
        "schema": regen.ENVELOPE_SCHEMA,
        "receipt": {
            "schema": regen.RECEIPT_SCHEMA,
            "regeneration_job_id": job_id,
            "attempt_id": attempt_id,
            "page_id": page_id,
            "slot_id": slot_id,
            "slot_payload_sha256": slot_hash,
            "parent_generation_job_id": parent_generation_job_id,
            "parent_source_sha256": parent_source_sha,
            "parent_recipe_id": "pov-dirt-bike:master",
            "parent_recipe_version": source_parent["job"]["recipeVersion"],
            "master_sha256": master_sha,
            "source_library_id": source_parent["job"]["sourceLibraryId"],
            "source_library_version": f"sha256:{source_parent['job']['sourceLibraryHash']}",
            "cut": dict(cut),
            "visual_treatment_json": "{}",
            "visual_treatment_sha256": "x",
            "source_sha256": source_sha,
            "completed_at_ms": NOW,
        },
        "artifact": {
            "sha256": source_sha,
            "byte_length": 1,
            "object_key": regen.object_key(job_id, attempt_id),
            "mime_type": regen.ARTIFACT_MIME_TYPE,
            "width": 1080, "height": 1920, "duration_ms": cut["duration_ms"],
        },
    }


def _ancestor_row(envelope, *, account="fixture.account"):
    return {
        "id": envelope["receipt"]["regeneration_job_id"],
        "slot_id": envelope["receipt"]["slot_id"],
        "slot_hash": envelope["receipt"]["slot_payload_sha256"],
        "attempt_id": envelope["receipt"]["attempt_id"],
        "submission_json": json.dumps({"request": {
            "page_id": envelope["receipt"]["page_id"],
            "account": account,
        }}),
        "output_authority_json": regen.canonical_json(envelope),
    }


def _single_hop_loader(render_id, envelope):
    def loader(job_id):
        return _ancestor_row(envelope) if job_id == render_id else None
    return loader


def test_subsequent_human_edit_resolves_the_prior_render_parent_to_the_same_original_window(
    source_parent, tmp_path, monkeypatch,
):
    worker, client, prepared, headers_now, _body = _queued_worker(
        tmp_path, monkeypatch, source_parent, regenerator=fake_regenerator)
    assert worker.run_one()
    output_a_sha = worker.status(prepared["id"])["source_regeneration"]["artifact"]["sha256"]

    second = _prepared(worker, output_a_sha)
    headers2, body2 = _repair_headers(second["id"], parent_id=prepared["id"], key="slot-repair-command-2")
    response = client.post(
        f"/api/control-plane/v1/post-renders/{second['id']}/regenerate-source",
        json=body2, headers=headers2,
    )
    assert response.status_code == 202, response.text
    command = regen.pending_command(worker._row(second["id"]))
    assert command is not None
    # Honest direct parent stays the prior render, while authority remains the
    # original master/window (never a rebound .5 * .25 window).
    assert command["parentGenerationJobId"] == prepared["id"]
    assert command["master"]["sha256"] == source_parent["job"]["clips"][0]["source"]["master"]["sha256"]
    assert command["cut"]["start_ms"] == WINDOW_START_MS
    assert command["cut"]["duration_ms"] == WINDOW_DURATION_MS


def test_ancestry_refuses_cycle(source_parent):
    ident = _terminal_identity(source_parent)
    envelope = _ancestor_envelope(
        source_parent, source_sha=ident["clip_sha"],
        parent_generation_job_id=RENDER_ID, parent_source_sha=ident["clip_sha"],
        master_sha=ident["master_sha"], cut=ident["cut"],
    )
    request = _submission(ident["clip_sha"], PAGE_ID).request
    with pytest.raises(regen.SourceRegenerationError) as caught:
        regen.resolve_parent_authority(
            page_id=PAGE_ID, parent_generation_job_id=RENDER_ID, request=request,
            prior_row_loader=_single_hop_loader(RENDER_ID, envelope),
        )
    assert caught.value.code == "source_regeneration_ancestor_cycle"


def test_ancestry_refuses_foreign_page(source_parent):
    ident = _terminal_identity(source_parent)
    envelope = _ancestor_envelope(
        source_parent, source_sha=ident["clip_sha"],
        parent_generation_job_id=ident["cp_id"], parent_source_sha=ident["clip_sha"],
        master_sha=ident["master_sha"], cut=ident["cut"], page_id="tt-other-page",
    )
    request = _submission(ident["clip_sha"], PAGE_ID).request
    with pytest.raises(regen.SourceRegenerationError) as caught:
        regen.resolve_parent_authority(
            page_id=PAGE_ID, parent_generation_job_id=RENDER_ID, request=request,
            prior_row_loader=_single_hop_loader(RENDER_ID, envelope),
        )
    assert caught.value.code == "source_regeneration_ancestor_foreign_page"


def test_ancestry_refuses_source_mismatch(source_parent):
    ident = _terminal_identity(source_parent)
    envelope = _ancestor_envelope(
        source_parent, source_sha="a" * 64,
        parent_generation_job_id=ident["cp_id"], parent_source_sha="a" * 64,
        master_sha=ident["master_sha"], cut=ident["cut"],
    )
    request = _submission("e" * 64, PAGE_ID).request
    with pytest.raises(regen.SourceRegenerationError) as caught:
        regen.resolve_parent_authority(
            page_id=PAGE_ID, parent_generation_job_id=RENDER_ID, request=request,
            prior_row_loader=_single_hop_loader(RENDER_ID, envelope),
        )
    assert caught.value.code == "source_regeneration_ancestor_source_mismatch"


def test_ancestry_refuses_spliced_window(source_parent):
    ident = _terminal_identity(source_parent)
    spliced = {"start_ms": WINDOW_START_MS + 333,
               "end_ms": WINDOW_START_MS + 333 + WINDOW_DURATION_MS,
               "duration_ms": WINDOW_DURATION_MS}
    envelope = _ancestor_envelope(
        source_parent, source_sha=ident["clip_sha"],
        parent_generation_job_id=ident["cp_id"], parent_source_sha=ident["clip_sha"],
        master_sha=ident["master_sha"], cut=spliced,
    )
    request = _submission(ident["clip_sha"], PAGE_ID).request
    with pytest.raises(regen.SourceRegenerationError) as caught:
        regen.resolve_parent_authority(
            page_id=PAGE_ID, parent_generation_job_id=RENDER_ID, request=request,
            prior_row_loader=_single_hop_loader(RENDER_ID, envelope),
        )
    assert caught.value.code == "source_regeneration_ancestor_cut_mismatch"


def test_default_source_regenerator_fails_closed_without_a_shared_loop(
    source_parent, tmp_path, monkeypatch,
):
    worker = jobs.PostRenderJobs(
        tmp_path / "post", renderer=FailRenderer(),
        fetcher=lambda *_: pytest.fail("the prepared source grant must not be fetched"),
        clock_ms=lambda: NOW,  # source_regenerator defaults to slot_source.regenerate
    )
    client = _post(worker, monkeypatch)
    prepared = _prepared(worker, source_parent["job"]["clips"][0]["sha256"])
    headers_now, body = _repair_headers(prepared["id"], parent_id=source_parent["job_id"])
    response = client.post(
        f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source",
        json=body, headers=headers_now,
    )
    assert response.status_code == 202, response.text
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "failed"
    assert job["error_code"] == "source_regeneration_runtime_unavailable"


def _minimal_command():
    return {
        "schema": regen.COMMAND_SCHEMA,
        "submissionSha256": "a" * 64,
        "parentGenerationJobId": "cpl-minimal",
        "parentAuthorityJobId": "cpl-minimal",
        "cutSlotId": "f" * 64 + ":0:6000",
        "pageId": PAGE_ID,
        "slotId": "slot:minimal",
        "slotPayloadSha256": "b" * 64,
        "parentSourceSha256": "c" * 64,
        "parentRecipeId": "pov-dirt-bike:master",
        "parentRecipeVersion": "v",
        "sourceLibraryId": "lib",
        "sourceLibraryVersion": "sha256:" + "d" * 64,
        "requestedVisualJson": "{}",
        "requestedVisualSha256": "a" * 64,
        "requestedSourceVisualSha256": "e" * 64,
        "master": {"sha256": "f" * 64, "bytes": 10, "storageKey": "k",
                   "filename": "m.mp4", "mimeType": "video/mp4",
                   "durationMs": 10000, "sourceOffsetMs": 0},
        "cut": {"start_ms": 0, "end_ms": 6000, "duration_ms": 6000},
        "render": {"playbackSpeed": 1.0,
                   "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
                   "filters": {"brightness": 0.5}, "encodePreset": "tiktok_delivery_v1",
                   "outputWidth": 1080, "outputHeight": 1920, "frame": None},
        "createdAtMs": 0,
    }


def test_two_source_repairs_share_one_loop_and_serialize_on_the_gate(monkeypatch, tmp_path, source_loop):
    """The old private-loop asyncio.run would raise once the gate binds elsewhere."""
    from services import ffmpeg

    async def _bind():
        # Force the gate to bind to this loop: hold it while a second acquire
        # blocks, which is the only path that sets Semaphore._loop.
        gate = ffmpeg._COLOR_CORRECT_GATE
        await gate.acquire()
        loop = asyncio.get_running_loop()
        async def _waiter():
            await gate.acquire()
        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0)
        gate.release()
        await task
        gate.release()
    asyncio.run_coroutine_threadsafe(_bind(), source_loop).result(timeout=5)
    assert ffmpeg._COLOR_CORRECT_GATE._loop is source_loop

    loops_seen = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    class Reader:
        async def read(self, _size=-1):
            return b""

    class Proc:
        def __init__(self, out):
            self.returncode = None
            self.stderr = Reader()
            self.out = out
        async def wait(self):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0)
            self.returncode = 0
            Path(self.out).write_bytes(b"x" * 64)
            with lock:
                active -= 1
            return 0

    async def fake_exec(*args, **_kwargs):
        loops_seen.append(asyncio.get_running_loop())
        return Proc(args[-1])

    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg, "_probe_input_color",
                        lambda _path: {"color_space": "bt709", "color_transfer": "bt709",
                                       "color_primaries": "bt709", "color_range": "tv"})
    monkeypatch.setattr(regen, "fetch_master",
                        lambda master, destination, *, page_id: destination.write_bytes(b"m" * 10) or destination)
    monkeypatch.setattr(regen, "_probe_artifact",
                        lambda _path: {"width": 1080, "height": 1920, "duration_ms": 6000,
                                       "video_codec": "h264", "pixel_format": "yuv420p",
                                       "frame_rate": 30.0})
    monkeypatch.setattr(regen, "_reload_planned_cut",
                        lambda command: (object(), object()))
    from services.control_plane_sources import source_cut_is_planned
    monkeypatch.setattr("services.control_plane_sources.source_cut_is_planned",
                        lambda *a, **k: True)

    start = threading.Barrier(2)
    results = []

    def run(index):
        start.wait(timeout=5)
        results.append(regen.regenerate(
            _minimal_command(), tmp_path / f"w{index}",
            job_id=f"render_x{index}", attempt_id="0" * 32, loop=source_loop,
        ))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    assert all(result["schema"] == regen.ENVELOPE_SCHEMA for result in results)
    assert loops_seen == [source_loop, source_loop]
    assert max_active == 1



def test_completed_repair_same_key_replays_completed_status(source_parent, tmp_path, monkeypatch):
    """A same-key replay of a COMPLETED regeneration returns 202 same status."""
    worker, client, prepared, headers_now, body = _queued_worker(
        tmp_path, monkeypatch, source_parent)
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "regeneration_needed"
    assert job["source_regeneration"]["schema"] == regen.ENVELOPE_SCHEMA

    url = f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source"
    replay = client.post(url, json=body, headers=headers_now)
    assert replay.status_code == 202, replay.text
    assert replay.json()["state"] == "regeneration_needed"
    assert replay.json()["source_regeneration"] == job["source_regeneration"]
    # A different key still observes a conflict, never a fresh repair.
    other = client.post(url, json=body, headers={**headers_now, "Idempotency-Key": "slot-repair-command-2"})
    assert other.status_code == 409


def test_source_ack_body_and_gc_retain_envelope_but_remove_media(source_parent, tmp_path, monkeypatch):
    """The closed ACK body admits a completed source-only attempt; GC frees its media."""
    now = [NOW]
    worker = jobs.PostRenderJobs(
        tmp_path / "post", renderer=FailRenderer(),
        fetcher=lambda *_: pytest.fail("the prepared source grant must not be fetched"),
        clock_ms=lambda: now[0], source_regenerator=fake_regenerator,
    )
    client = _post(worker, monkeypatch)
    prepared = _prepared(worker, source_parent["job"]["clips"][0]["sha256"])
    headers_now, body = _repair_headers(prepared["id"], parent_id=source_parent["job_id"])
    url = f"/api/control-plane/v1/post-renders/{prepared['id']}/regenerate-source"
    assert client.post(url, json=body, headers=headers_now).status_code == 202
    assert worker.run_one()
    job = worker.status(prepared["id"])
    envelope = job["source_regeneration"]
    row = worker._row(prepared["id"])
    ack = {
        "schema": regen.SOURCE_ACK_SCHEMA,
        "attempt_id": row["attempt_id"],
        "source_sha256": envelope["receipt"]["source_sha256"],
        "slot_payload_sha256": row["slot_hash"],
    }
    ack_url = f"/api/control-plane/v1/post-renders/{prepared['id']}/acknowledge"
    acked = client.post(ack_url, json=ack, headers=headers_now)
    assert acked.status_code == 202, acked.text
    assert acked.json()["acknowledged_at_ms"] is not None
    assert acked.json()["source_regeneration"] == envelope
    # Exact identity is required: a wrong source SHA is refused.
    bad = dict(ack, source_sha256="f" * 64)
    assert client.post(ack_url, json=bad, headers=headers_now).status_code == 409

    artifact = regen.artifact_path(worker.root, prepared["id"], row["attempt_id"])
    assert artifact.exists()
    now[0] += jobs._retire_after_ms() + 1
    worker._gc()
    assert not artifact.exists()
    status = worker.status(prepared["id"])
    assert status["retired_at_ms"] is not None
    assert status["source_regeneration"] == envelope  # authority retained
    # Replay after retirement is metadata-only exact validation.
    replay = client.post(ack_url, json=ack, headers=headers_now)
    assert replay.status_code == 202, replay.text
    assert replay.json()["acknowledged_at_ms"] == acked.json()["acknowledged_at_ms"]


def test_malformed_authority_fails_closed_before_any_io(source_parent, tmp_path, monkeypatch):
    """A nonblank authority that is none of the typed variants must never fall through."""
    worker, client, prepared, headers_now, _body = _queued_worker(
        tmp_path, monkeypatch, source_parent)
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", ("{not json", prepared["id"]))
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "failed"
    assert job["error_code"] == "source_regeneration_authority_corrupt"
    # No media IO: the prepared grant was never fetched and no repair rendered.
    assert regen.pending_command(worker._row(prepared["id"])) is None


def test_parent_strategy_flip_after_queue_refuses_before_io(source_parent, tmp_path, monkeypatch):
    """Revalidate the terminal parent's intent before media IO and completion."""
    worker, client, prepared, headers_now, _body = _queued_worker(
        tmp_path, monkeypatch, source_parent)
    store = cp._load_jobs()
    store["jobs"][source_parent["job_id"]]["status"] = "failed"
    store["jobs"][source_parent["job_id"]]["error"] = "provider_generation_failed"
    cp.atomic_save(cp._jobs_path(), store)
    assert worker.run_one()
    job = worker.status(prepared["id"])
    assert job["state"] == "failed"
    assert job["error_code"] == "source_regeneration_parent_changed"
