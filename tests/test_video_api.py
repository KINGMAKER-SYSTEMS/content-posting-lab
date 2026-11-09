import json
import time
import zipfile
from io import BytesIO

from project_manager import get_project_video_dir
from routers import video as video_router


def test_generate_job_lifecycle_and_download(sync_client, monkeypatch):
    provider_id = next(iter(video_router.PROVIDERS.keys()))
    key_id = video_router.PROVIDERS[provider_id]["key_id"]
    monkeypatch.setitem(video_router.API_KEYS, key_id, "test-key")

    async def fake_generate_one(
        job_id,
        index,
        provider,
        prompt,
        aspect_ratio,
        resolution,
        duration,
        image_data_uri,
        jobs,
        output_dir,
        url_prefix,
        on_complete=None,
        **extra,
    ):
        entry = jobs[job_id]["videos"][index]
        filename = f"fake_{index}.mp4"
        (output_dir / filename).write_bytes(b"fake video")
        entry["status"] = "done"
        entry["file"] = filename
        entry["url"] = f"{url_prefix}/{filename}"
        if on_complete:
            on_complete(job_id)

    monkeypatch.setattr(video_router, "generate_one", fake_generate_one)

    response = sync_client.post(
        "/api/video/generate",
        data={
            "prompt": "test prompt",
            "provider": provider_id,
            "count": "2",
            "duration": "5",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "project": "video-suite",
        },
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    final_job = None
    for _ in range(50):
        job_response = sync_client.get(f"/api/video/jobs/{job_id}")
        assert job_response.status_code == 200
        final_job = job_response.json()
        statuses = [video["status"] for video in final_job["videos"]]
        if all(status == "done" for status in statuses):
            break
        time.sleep(0.02)

    assert final_job is not None
    assert all(video["status"] == "done" for video in final_job["videos"])

    download = sync_client.get(f"/api/video/jobs/{job_id}/download-all")
    assert download.status_code == 200
    archive = zipfile.ZipFile(BytesIO(download.content))
    names = archive.namelist()
    assert len(names) == 2
    assert all(name.startswith("fake_") for name in names)


def test_generate_rejects_unknown_provider(sync_client):
    response = sync_client.post(
        "/api/video/generate",
        data={
            "prompt": "test prompt",
            "provider": "unknown-provider",
            "count": "1",
            "duration": "5",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "project": "video-suite",
        },
    )
    assert response.status_code == 400


def test_generate_budget_refusal_leaves_no_ghost_job_or_prompt(
    sync_client, monkeypatch, isolated_projects_root,
):
    """P2: a 429 refusal must not leave an in-memory queued job or a persisted
    prompt record — affordability is checked before any job/prompt state is built."""
    from services import generation_budget

    provider_id = next(iter(video_router.PROVIDERS.keys()))
    key_id = video_router.PROVIDERS[provider_id]["key_id"]
    monkeypatch.setitem(video_router.API_KEYS, key_id, "test-key")
    monkeypatch.setattr(generation_budget, "can_reserve_at",
                        lambda *a, **k: False)
    saved_prompts = []
    monkeypatch.setattr(video_router, "_save_prompt",
                        lambda proj, entry: saved_prompts.append(entry))
    before_jobs = dict(video_router.jobs)

    response = sync_client.post(
        "/api/video/generate",
        data={
            "prompt": "ghost prompt",
            "provider": provider_id,
            "count": "1",
            "duration": "5",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "project": "video-suite",
        },
    )
    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "generation_daily_budget_reached"
    assert video_router.jobs == before_jobs, "refusal must not enqueue a ghost job"
    assert saved_prompts == [], "refusal must not persist a prompt record"


def test_generate_budget_refusal_uses_one_clock_sample_for_body_and_header(
    sync_client, monkeypatch, isolated_projects_root,
):
    """P2: `resets_at` in the body and the `Retry-After` header must be derived
    from a SINGLE `now` so midnight cannot fall between the two samples."""
    from services import generation_budget

    provider_id = next(iter(video_router.PROVIDERS.keys()))
    key_id = video_router.PROVIDERS[provider_id]["key_id"]
    monkeypatch.setitem(video_router.API_KEYS, key_id, "test-key")
    monkeypatch.setattr(generation_budget, "can_reserve_at",
                        lambda *a, **k: False)

    seen = {}

    def fake_next_reset_iso(now=None):
        seen["reset_now"] = now
        return "2026-10-02T00:00:00+00:00"

    def fake_retry_after_seconds(now=None):
        seen["retry_now"] = now
        return 42

    monkeypatch.setattr(generation_budget, "next_reset_iso", fake_next_reset_iso)
    monkeypatch.setattr(generation_budget, "retry_after_seconds", fake_retry_after_seconds)

    response = sync_client.post(
        "/api/video/generate",
        data={
            "prompt": "midnight prompt",
            "provider": provider_id,
            "count": "1",
            "duration": "5",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "project": "video-suite",
        },
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    # Both the body and the header were computed from the same captured instant.
    assert seen["reset_now"] is not None
    assert seen["retry_now"] is not None
    assert seen["reset_now"] == seen["retry_now"]


def test_generate_stores_and_passes_negative_prompt(sync_client, monkeypatch, isolated_projects_root):
    provider_id = "wan-i2v-fast"
    key_id = video_router.PROVIDERS[provider_id]["key_id"]
    monkeypatch.setitem(video_router.API_KEYS, key_id, "test-key")
    captured_extra = {}

    async def fake_generate_one(
        job_id,
        index,
        provider,
        prompt,
        aspect_ratio,
        resolution,
        duration,
        image_data_uri,
        jobs,
        output_dir,
        url_prefix,
        on_complete=None,
        **extra,
    ):
        captured_extra.update(extra)
        entry = jobs[job_id]["videos"][index]
        filename = f"fake_{index}.mp4"
        (output_dir / filename).write_bytes(b"fake video")
        entry["status"] = "done"
        entry["file"] = filename
        entry["url"] = f"{url_prefix}/{filename}"
        if on_complete:
            on_complete(job_id)

    monkeypatch.setattr(video_router, "generate_one", fake_generate_one)

    negative_prompt = "driving, tire rotation, camera pan, morphing, extra vehicles"
    from PIL import Image
    image = BytesIO()
    Image.new("RGB", (2, 2), "black").save(image, format="PNG")
    response = sync_client.post(
        "/api/video/generate",
        data={
            "prompt": "Locked-off parked truck shot.",
            "provider": provider_id,
            "count": "1",
            "duration": "5",
            "aspect_ratio": "9:16",
            "resolution": "480p",
            "negative_prompt": f"  {negative_prompt}  ",
            "project": "video-suite",
        },
        files={"media": ("fixture.png", image.getvalue(), "image/png")},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    final_job = None
    for _ in range(50):
        job_response = sync_client.get(f"/api/video/jobs/{job_id}")
        assert job_response.status_code == 200
        final_job = job_response.json()
        if final_job["videos"][0]["status"] == "done":
            break
        time.sleep(0.02)

    assert captured_extra["negative_prompt"] == negative_prompt
    assert final_job["negative_prompt"] == negative_prompt

    prompts = json.loads((isolated_projects_root / "video-suite" / "prompts.json").read_text())
    assert prompts[0]["negative_prompt"] == negative_prompt


# --- job persistence error paths (_load_jobs / _save_jobs) ---


def test_load_jobs_missing_file_is_noop(isolated_projects_root):
    """No jobs.json on disk → load leaves the in-memory dict untouched, no raise."""
    video_router.jobs.clear()
    video_router._load_jobs("nonexistent-project")
    assert video_router.jobs == {}


def test_load_jobs_corrupted_json_swallows_error(isolated_projects_root):
    """Corrupted jobs.json → JSONDecodeError is caught and logged, not raised."""
    proj_dir = isolated_projects_root / "corrupt-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "jobs.json").write_text("{not valid json", encoding="utf-8")

    video_router.jobs.clear()
    # Must not raise despite the malformed file.
    video_router._load_jobs("corrupt-proj")
    # Nothing got loaded from the unparseable file.
    assert video_router.jobs == {}


def test_load_jobs_recovers_stuck_nonterminal_videos(isolated_projects_root):
    """A persisted job killed mid-flight: videos in non-terminal states get
    repaired on load (done if a file/crop landed, else error)."""
    proj_dir = isolated_projects_root / "recover-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    persisted = {
        "job-1": {
            "project": "recover-proj",
            "videos": [
                {"status": "generating"},  # no file/crop → error
                {"status": "downloading", "file": "v1.mp4"},  # has file → done
                {"status": "cropping", "crops": ["c.mp4"]},  # has crops → done
                {"status": "done", "file": "v3.mp4"},  # already terminal → untouched
            ],
        }
    }
    (proj_dir / "jobs.json").write_text(json.dumps(persisted), encoding="utf-8")

    video_router.jobs.clear()
    video_router._load_jobs("recover-proj")

    vids = video_router.jobs["job-1"]["videos"]
    assert vids[0]["status"] == "error"
    assert vids[0]["error"] == "Server restarted during processing"
    assert vids[1]["status"] == "done"
    assert vids[2]["status"] == "done"
    assert vids[3]["status"] == "done"


def test_save_jobs_unwritable_dir_swallows_error(isolated_projects_root, monkeypatch):
    """atomic_save raising OSError (e.g. disk full / unwritable) is caught,
    not propagated — an in-flight render must not crash the request."""
    video_router.jobs.clear()
    video_router.jobs["job-x"] = {"project": "save-proj", "videos": [{"status": "done"}]}

    def boom(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(video_router, "atomic_save", boom)
    # Must not raise.
    assert video_router._save_jobs("save-proj") is False


def test_generate_refuses_before_provider_when_initial_job_save_fails(
    sync_client, isolated_projects_root, monkeypatch,
):
    provider_id = next(iter(video_router.PROVIDERS))
    monkeypatch.setitem(video_router.API_KEYS, video_router.PROVIDERS[provider_id]["key_id"], "test-key")
    started = []

    async def fake_generate_one(*args, **kwargs):
        started.append(True)

    real_save = video_router.atomic_save

    def fail_jobs(path, data, **kwargs):
        if path.name == "jobs.json":
            raise OSError("No space left on device")
        return real_save(path, data, **kwargs)

    monkeypatch.setattr(video_router, "atomic_save", fail_jobs)
    monkeypatch.setattr(video_router, "generate_one", fake_generate_one)
    monkeypatch.setattr(video_router.generation_budget, "debit_generation_spend_at",
                        lambda *args, **kwargs: started.append("debited"))
    response = sync_client.post("/api/video/generate", data={
        "prompt": "receipt failure", "provider": provider_id, "count": "1",
        "duration": "5", "aspect_ratio": "9:16", "resolution": "720p",
        "project": "receipt-failure",
    })
    assert response.status_code == 503
    assert not started
    assert not [j for j in video_router.jobs.values() if j.get("project") == "receipt-failure"]
    assert not (isolated_projects_root / "receipt-failure" / "jobs.json").exists()


def test_completed_video_waits_for_durable_receipt_before_done(
    sync_client, isolated_projects_root, monkeypatch,
):
    provider_id = next(iter(video_router.PROVIDERS))
    monkeypatch.setitem(video_router.API_KEYS, video_router.PROVIDERS[provider_id]["key_id"], "test-key")
    real_save = video_router.atomic_save
    saves = 0
    blocked = True

    def fail_completion(path, data, **kwargs):
        nonlocal saves
        if path.name == "jobs.json":
            saves += 1
            if saves > 1 and blocked:
                raise OSError("No space left on device")
        if path.parent.name == ".video-completions" and blocked:
            raise OSError("No space left on device")
        return real_save(path, data, **kwargs)

    async def fake_generate_one(job_id, index, provider, prompt, aspect_ratio,
                                resolution, duration, image_data_uri, jobs,
                                output_dir, url_prefix, on_complete=None, **extra):
        entry = jobs[job_id]["videos"][index]
        (output_dir / "finished.mp4").write_bytes(b"video")
        entry.update(status="done", file="finished.mp4", url=f"{url_prefix}/finished.mp4")
        on_complete(job_id)

    monkeypatch.setattr(video_router, "atomic_save", fail_completion)
    monkeypatch.setattr(video_router, "generate_one", fake_generate_one)
    response = sync_client.post("/api/video/generate", data={
        "prompt": "completion failure", "provider": provider_id, "count": "1",
        "duration": "5", "aspect_ratio": "9:16", "resolution": "720p",
        "project": "completion-failure",
    })
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    result = None
    for _ in range(50):
        result = sync_client.get(f"/api/video/jobs/{job_id}").json()["videos"][0]
        if result.get("file"):
            break
        time.sleep(0.02)
    assert result["status"] == "processing"
    assert sync_client.get(f"/api/video/jobs/{job_id}/download-all").status_code == 400
    persisted = json.loads((isolated_projects_root / "completion-failure" / "jobs.json").read_text())
    assert persisted[job_id]["videos"][0]["status"] == "queued"

    blocked = False
    recovered = sync_client.get(f"/api/video/jobs/{job_id}")
    assert recovered.json()["videos"][0]["status"] == "done"
    persisted = json.loads((isolated_projects_root / "completion-failure" / "jobs.json").read_text())
    assert persisted[job_id]["videos"][0]["status"] == "done"


def test_completion_receipt_recovers_paid_video_after_jobs_save_fails(
    isolated_projects_root, monkeypatch,
):
    project = "receipt-recovery"
    entry = {"index": 0, "status": "queued"}
    job = {"id": "paid-job", "project": project, "completion_receipts_v1": True,
           "videos": [entry]}
    video_router.jobs.clear()
    video_router.jobs[job["id"]] = job
    assert video_router._save_jobs(project)
    _make_video(project, "paid.mp4", b"paid video")
    entry.update(status="done", file="paid.mp4", url="/projects/receipt-recovery/videos/paid.mp4")

    real_save = video_router.atomic_save

    def fail_jobs(path, data, **kwargs):
        if path.name == "jobs.json":
            raise OSError("jobs.json unavailable")
        return real_save(path, data, **kwargs)

    monkeypatch.setattr(video_router, "atomic_save", fail_jobs)
    video_router._persist_job(job["id"], 0)
    assert entry["status"] == "done"
    assert video_router._completion_path(project, job["id"], 0).exists()
    video_router.jobs.clear()
    video_router._load_jobs(project)
    recovered = video_router.jobs[job["id"]]["videos"][0]
    assert recovered["status"] == "done"
    assert recovered["file"] == "paid.mp4"


def test_completion_receipt_rejects_changed_crop_bytes_after_restart(isolated_projects_root):
    project = "receipt-crops"
    entry = {"index": 0, "status": "queued"}
    job = {"id": "crop-job", "project": project, "completion_receipts_v1": True,
           "videos": [entry]}
    video_router.jobs.clear()
    video_router.jobs[job["id"]] = job
    assert video_router._save_jobs(project)
    for name in ("master.mp4", "crop0.mp4", "crop1.mp4"):
        _make_video(project, name, name.encode())
    entry.update(status="done", file="crop0.mp4", url="/projects/receipt-crops/videos/crop0.mp4",
                 provider_master_file="master.mp4",
                 crops=[{"file": "crop0.mp4"}, {"file": "crop1.mp4"}])
    assert video_router._write_completion(project, job["id"], 0, entry)
    _make_video(project, "crop1.mp4", b"changed crop")

    video_router.jobs.clear()
    video_router._load_jobs(project)
    recovered = video_router.jobs[job["id"]]["videos"][0]
    assert recovered["status"] == "error"
    assert "changed" in recovered["error"]


def test_new_job_partial_file_without_completion_receipt_is_not_done(isolated_projects_root):
    project = "partial-video"
    _make_video(project, "partial.mp4")
    job = {"id": "partial-job", "project": project, "completion_receipts_v1": True,
           "videos": [{"index": 0, "status": "downloading", "file": "partial.mp4"}]}
    video_router.jobs.clear()
    video_router.jobs[job["id"]] = job
    assert video_router._save_jobs(project)

    video_router.jobs.clear()
    video_router._load_jobs(project)
    recovered = video_router.jobs[job["id"]]["videos"][0]
    assert recovered["status"] == "error"


def test_completion_receipt_refuses_symlinked_directory(isolated_projects_root):
    project = "receipt-link"
    project_dir = isolated_projects_root / project
    project_dir.mkdir()
    elsewhere = isolated_projects_root / "other-project"
    elsewhere.mkdir()
    (project_dir / ".video-completions").symlink_to(elsewhere, target_is_directory=True)
    _make_video(project, "paid.mp4")
    entry = {"index": 0, "status": "done", "file": "paid.mp4",
             "url": "/projects/receipt-link/videos/paid.mp4"}

    assert video_router._write_completion(project, "paid-job", 0, entry) is False
    assert video_router._read_completion(project, "paid-job", 0) is None
    assert not list(elsewhere.iterdir())


def test_save_jobs_skips_when_no_matching_jobs(isolated_projects_root):
    """Saving a project with no in-memory jobs writes nothing (early return)."""
    video_router.jobs.clear()
    video_router._save_jobs("empty-proj")
    assert not (isolated_projects_root / "empty-proj" / "jobs.json").exists()


def test_save_then_load_roundtrip(isolated_projects_root):
    """Happy path: a terminal job round-trips through disk intact."""
    video_router.jobs.clear()
    video_router.jobs["rt-1"] = {
        "project": "rt-proj",
        "videos": [{"status": "done", "file": "a.mp4"}],
    }
    video_router._save_jobs("rt-proj")
    assert (isolated_projects_root / "rt-proj" / "jobs.json").exists()

    video_router.jobs.clear()
    video_router._load_jobs("rt-proj")
    assert video_router.jobs["rt-1"]["videos"][0]["status"] == "done"
    assert video_router.jobs["rt-1"]["videos"][0]["file"] == "a.mp4"


# --- helpers -------------------------------------------------------------


def _make_video(project: str, name: str, data: bytes = b"vid") -> None:
    """Write a fake video file into a project's videos dir."""
    vdir = get_project_video_dir(project)
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / name).write_bytes(data)


# --- providers / schemas -------------------------------------------------


def test_list_providers_only_returns_keyed(sync_client, monkeypatch):
    """Only providers with a configured API key are surfaced, and 'module' is stripped."""
    # Clear all keys, then enable exactly one provider.
    for k in list(video_router.API_KEYS):
        monkeypatch.setitem(video_router.API_KEYS, k, "")
    provider_id = next(iter(video_router.PROVIDERS.keys()))
    key_id = video_router.PROVIDERS[provider_id]["key_id"]
    monkeypatch.setitem(video_router.API_KEYS, key_id, "test-key")

    resp = sync_client.get("/api/video/providers")
    assert resp.status_code == 200
    body = resp.json()
    ids = {p["id"] for p in body}
    assert provider_id in ids
    # Every returned provider must be keyed and must not leak the module object.
    for p in body:
        assert "module" not in p
        assert video_router.API_KEYS.get(video_router.PROVIDERS[p["id"]]["key_id"])


def test_get_provider_schemas(sync_client):
    resp = sync_client.get("/api/video/provider-schemas")
    assert resp.status_code == 200
    body = resp.json()
    assert body == video_router.PROVIDER_SCHEMAS
    assert "grok" in body


# --- prompts list / clear ------------------------------------------------


def test_list_prompts_empty_then_clear(sync_client, isolated_projects_root):
    # No prompts.json yet → empty list.
    resp = sync_client.get("/api/video/prompts?project=prompt-proj")
    assert resp.status_code == 200
    assert resp.json() == []

    # Seed a prompts file, confirm it reads back, then clear it.
    video_router._save_prompt("prompt-proj", {"prompt": "hi", "timestamp": "t"})
    resp = sync_client.get("/api/video/prompts?project=prompt-proj")
    assert resp.json()[0]["prompt"] == "hi"

    pfile = isolated_projects_root / "prompt-proj" / "prompts.json"
    assert pfile.exists()
    resp = sync_client.delete("/api/video/prompts?project=prompt-proj")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert not pfile.exists()

    # Clearing again (file already gone) is a no-op success.
    resp = sync_client.delete("/api/video/prompts?project=prompt-proj")
    assert resp.status_code == 200


# --- delete_video_file (path traversal guard) ----------------------------


def test_delete_file_happy_path(sync_client):
    _make_video("del-proj", "gone.mp4")
    target = get_project_video_dir("del-proj") / "gone.mp4"
    assert target.exists()

    resp = sync_client.request(
        "DELETE", "/api/video/file", params={"project": "del-proj", "path": "gone.mp4"}
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not target.exists()


def test_delete_file_requires_path(sync_client):
    resp = sync_client.request("DELETE", "/api/video/file", params={"project": "p"})
    assert resp.status_code == 400


def test_delete_file_blocks_traversal(sync_client, isolated_projects_root):
    """A ../ path that escapes the project videos dir is rejected with 400 and
    leaves the targeted outside file untouched."""
    victim = isolated_projects_root / "secret.txt"
    victim.write_text("do not delete", encoding="utf-8")
    # Ensure the project dir exists so resolution doesn't short-circuit.
    get_project_video_dir("trav-proj").mkdir(parents=True, exist_ok=True)

    resp = sync_client.request(
        "DELETE",
        "/api/video/file",
        params={"project": "trav-proj", "path": "../../../secret.txt"},
    )
    assert resp.status_code == 400
    assert victim.exists()


def test_delete_file_missing_returns_404(sync_client):
    get_project_video_dir("miss-proj").mkdir(parents=True, exist_ok=True)
    resp = sync_client.request(
        "DELETE", "/api/video/file", params={"project": "miss-proj", "path": "nope.mp4"}
    )
    assert resp.status_code == 404


# --- bulk-download / bulk-delete -----------------------------------------


def test_bulk_download_zips_done_videos(sync_client):
    _make_video("bulk-proj", "a.mp4", b"AAAA")
    _make_video("bulk-proj", "b.mp4", b"BBBB")
    video_router.jobs.clear()
    video_router.jobs["j1"] = {
        "project": "bulk-proj",
        "videos": [{"status": "done", "file": "a.mp4"}],
    }
    video_router.jobs["j2"] = {
        "project": "bulk-proj",
        "videos": [{"status": "done", "file": "b.mp4"}, {"status": "error"}],
    }

    resp = sync_client.post(
        "/api/video/bulk-download",
        json={"job_ids": ["j1", "j2"], "project": "bulk-proj"},
    )
    assert resp.status_code == 200
    names = zipfile.ZipFile(BytesIO(resp.content)).namelist()
    assert set(names) == {"j1/a.mp4", "j2/b.mp4"}


def test_bulk_download_no_ids_400(sync_client):
    resp = sync_client.post("/api/video/bulk-download", json={"job_ids": []})
    assert resp.status_code == 400


def test_bulk_download_no_completed_400(sync_client):
    video_router.jobs.clear()
    video_router.jobs["j1"] = {
        "project": "bulk-proj",
        "videos": [{"status": "error"}],
    }
    resp = sync_client.post(
        "/api/video/bulk-download",
        json={"job_ids": ["j1"], "project": "bulk-proj"},
    )
    assert resp.status_code == 400


def test_bulk_delete_removes_jobs_and_files(sync_client):
    _make_video("bd-proj", "x.mp4")
    _make_video("bd-proj", "y.mp4")
    video_router.jobs.clear()
    video_router.jobs["d1"] = {
        "project": "bd-proj",
        "videos": [{"status": "done", "file": "x.mp4"}],
    }
    video_router.jobs["d2"] = {
        "project": "bd-proj",
        "videos": [{"status": "done", "file": "y.mp4"}],
    }
    assert video_router._save_jobs("bd-proj")
    vdir = get_project_video_dir("bd-proj")

    resp = sync_client.post(
        "/api/video/bulk-delete",
        json={"job_ids": ["d1", "d2", "missing"], "project": "bd-proj"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_jobs"] == 2
    assert body["deleted_files"] == 2
    assert not (vdir / "x.mp4").exists()
    assert not (vdir / "y.mp4").exists()
    assert "d1" not in video_router.jobs and "d2" not in video_router.jobs

    video_router.jobs.clear()
    video_router._load_jobs("bd-proj")
    assert video_router.jobs == {}
    assert json.loads((vdir.parent / "jobs.json").read_text()) == {}


def test_bulk_delete_skips_job_from_another_project(sync_client):
    _make_video("bulk-owner-a", "a.mp4", b"a")
    _make_video("bulk-owner-b", "b.mp4", b"b")
    video_router.jobs.clear()
    job_a = {
        "project": "bulk-owner-a",
        "videos": [{"status": "done", "file": "a.mp4"}],
    }
    job_b = {
        "project": "bulk-owner-b",
        "videos": [{"status": "done", "file": "b.mp4"}],
    }
    video_router.jobs.update({"job-a": job_a, "job-b": job_b})
    assert video_router._save_jobs("bulk-owner-a")
    assert video_router._save_jobs("bulk-owner-b")

    response = sync_client.post(
        "/api/video/bulk-delete",
        json={"job_ids": ["job-a", "job-b"], "project": "bulk-owner-a"},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted_jobs": 1, "deleted_files": 1}
    assert "job-a" not in video_router.jobs
    assert video_router.jobs["job-b"] == job_b
    assert (get_project_video_dir("bulk-owner-a") / "a.mp4").exists() is False
    assert (get_project_video_dir("bulk-owner-b") / "b.mp4").read_bytes() == b"b"

    video_router.jobs.clear()
    video_router._load_jobs("bulk-owner-a")
    video_router._load_jobs("bulk-owner-b")
    assert set(video_router.jobs) == {"job-b"}
    assert json.loads(
        (get_project_video_dir("bulk-owner-a").parent / "jobs.json").read_text()
    ) == {}
    assert json.loads(
        (get_project_video_dir("bulk-owner-b").parent / "jobs.json").read_text()
    ) == {"job-b": job_b}


def test_delete_job_requires_matching_project(sync_client):
    _make_video("single-owner-a", "a.mp4", b"a")
    _make_video("single-owner-b", "b.mp4", b"b")
    video_router.jobs.clear()
    job_a = {
        "project": "single-owner-a",
        "videos": [{"status": "done", "file": "a.mp4"}],
    }
    job_b = {
        "project": "single-owner-b",
        "videos": [{"status": "done", "file": "b.mp4"}],
    }
    video_router.jobs.update({"job-a": job_a, "job-b": job_b})
    assert video_router._save_jobs("single-owner-a")
    assert video_router._save_jobs("single-owner-b")

    response = sync_client.request(
        "DELETE", "/api/video/jobs/job-b", params={"project": "single-owner-a"}
    )

    assert response.status_code == 404
    assert video_router.jobs == {"job-a": job_a, "job-b": job_b}
    assert (get_project_video_dir("single-owner-b") / "b.mp4").read_bytes() == b"b"
    assert json.loads(
        (get_project_video_dir("single-owner-b").parent / "jobs.json").read_text()
    ) == {"job-b": job_b}

    video_router.jobs.clear()
    video_router._load_jobs("single-owner-b")
    assert video_router.jobs == {"job-b": job_b}

    same_project_response = sync_client.request(
        "DELETE", "/api/video/jobs/job-b", params={"project": "single-owner-b"}
    )
    assert same_project_response.status_code == 200
    assert video_router.jobs == {}
    assert not (get_project_video_dir("single-owner-b") / "b.mp4").exists()

    video_router.jobs.clear()
    video_router._load_jobs("single-owner-b")
    assert video_router.jobs == {}
    assert json.loads(
        (get_project_video_dir("single-owner-b").parent / "jobs.json").read_text()
    ) == {}


def test_bulk_delete_save_failure_restores_jobs_and_keeps_files(
    sync_client, monkeypatch,
):
    _make_video("bd-save-fail", "keep.mp4", b"keep")
    video_router.jobs.clear()
    job = {
        "project": "bd-save-fail",
        "videos": [{"status": "done", "file": "keep.mp4"}],
    }
    video_router.jobs["keep-job"] = job
    assert video_router._save_jobs("bd-save-fail")
    monkeypatch.setattr(video_router, "_save_jobs", lambda project: False)

    response = sync_client.post(
        "/api/video/bulk-delete",
        json={"job_ids": ["keep-job"], "project": "bd-save-fail"},
    )

    assert response.status_code == 503
    assert video_router.jobs == {"keep-job": job}
    assert (get_project_video_dir("bd-save-fail") / "keep.mp4").read_bytes() == b"keep"


def test_bulk_delete_no_ids_400(sync_client):
    resp = sync_client.post("/api/video/bulk-delete", json={"job_ids": []})
    assert resp.status_code == 400


def test_bulk_delete_tolerates_missing_files(sync_client):
    """A tracked file already gone from disk must not raise — safe_unlink no-ops
    and the job is still removed (graceful, was a hard unlink() raise before)."""
    _make_video("bd-miss", "present.mp4")
    video_router.jobs.clear()
    video_router.jobs["g1"] = {
        "project": "bd-miss",
        # 'ghost.mp4' was never written to disk; safe_unlink must tolerate it.
        "videos": [
            {"status": "done", "file": "present.mp4"},
            {"status": "done", "file": "ghost.mp4"},
        ],
    }
    vdir = get_project_video_dir("bd-miss")

    resp = sync_client.post(
        "/api/video/bulk-delete",
        json={"job_ids": ["g1"], "project": "bd-miss"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_jobs"] == 1
    # Only the file that actually existed counts as removed.
    assert body["deleted_files"] == 1
    assert not (vdir / "present.mp4").exists()
    assert "g1" not in video_router.jobs


def test_delete_job_tolerates_unlink_oserror(sync_client, monkeypatch):
    """If a file is locked/removed at unlink time (TOCTOU), delete_job returns
    gracefully instead of 500-ing — safe_unlink swallows the OSError."""
    _make_video("dj-race", "race.mp4")
    video_router.jobs.clear()
    video_router.jobs["r1"] = {
        "project": "dj-race",
        "videos": [{"status": "done", "file": "race.mp4"}],
    }

    # safe_unlink calls os.unlink inside services.fsutil — patch it there.
    import services.fsutil as fsutil

    def boom(path):
        raise OSError("Device or resource busy")

    monkeypatch.setattr(fsutil.os, "unlink", boom)

    resp = sync_client.request(
        "DELETE", "/api/video/jobs/r1", params={"project": "dj-race"}
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    # Job removed from memory even though the file couldn't be unlinked.
    assert "r1" not in video_router.jobs


def test_delete_last_job_persists_empty_state_across_restart(sync_client, isolated_projects_root):
    """Deleting the final job must clear an existing jobs.json durably."""
    project = "delete-last"
    video_router.jobs.clear()
    video_router.jobs["last"] = {
        "project": project,
        "videos": [{"status": "error", "error": "finished"}],
    }
    assert video_router._save_jobs(project)
    jobs_file = isolated_projects_root / project / "jobs.json"
    assert json.loads(jobs_file.read_text()) == {"last": video_router.jobs["last"]}

    response = sync_client.request(
        "DELETE", "/api/video/jobs/last", params={"project": project}
    )
    assert response.status_code == 200
    assert video_router.jobs == {}

    # Simulate a new process: the deleted job must not be resurrected.
    video_router.jobs.clear()
    video_router._load_jobs(project)
    assert video_router.jobs == {}
    assert json.loads(jobs_file.read_text()) == {}


def test_delete_job_save_failure_restores_job_and_keeps_file(sync_client, monkeypatch):
    _make_video("delete-save-fail", "keep.mp4", b"keep")
    video_router.jobs.clear()
    job = {
        "project": "delete-save-fail",
        "videos": [{"status": "done", "file": "keep.mp4"}],
    }
    video_router.jobs["keep-job"] = job
    assert video_router._save_jobs("delete-save-fail")
    monkeypatch.setattr(video_router, "_save_jobs", lambda project: False)

    response = sync_client.request(
        "DELETE", "/api/video/jobs/keep-job", params={"project": "delete-save-fail"}
    )

    assert response.status_code == 503
    assert video_router.jobs == {"keep-job": job}
    assert (get_project_video_dir("delete-save-fail") / "keep.mp4").read_bytes() == b"keep"


# --- color-correct (single) ----------------------------------------------


def test_color_correct_zero_cc_streams_original(sync_client):
    """Null/empty CC → original bytes streamed unchanged, no ffmpeg call."""
    _make_video("cc-proj", "src.mp4", b"ORIGINAL-BYTES")
    resp = sync_client.post(
        "/api/video/color-correct",
        json={"project": "cc-proj", "path": "src.mp4", "color_correction": None},
    )
    assert resp.status_code == 200
    assert resp.content == b"ORIGINAL-BYTES"
    assert 'filename="src_cc.mp4"' in resp.headers["content-disposition"]


def test_color_correct_runs_ffmpeg_when_cc_set(sync_client, monkeypatch):
    """A non-zero CC dict routes through run_color_correct (mocked) under the semaphore."""
    _make_video("cc-proj2", "src.mp4", b"ORIGINAL")
    called = {}

    async def fake_cc(src, dst, cc, scale=None):
        called["args"] = (src, dst, cc, scale)
        with open(dst, "wb") as f:
            f.write(b"CORRECTED")

    monkeypatch.setattr(video_router, "run_color_correct", fake_cc)

    resp = sync_client.post(
        "/api/video/color-correct",
        json={"project": "cc-proj2", "path": "src.mp4", "color_correction": {"brightness": 5}},
    )
    assert resp.status_code == 200
    assert resp.content == b"CORRECTED"
    assert called["args"][3] is None  # scale=None preserves dimensions


def test_color_correct_blocks_traversal(sync_client, isolated_projects_root):
    get_project_video_dir("cc-trav").mkdir(parents=True, exist_ok=True)
    resp = sync_client.post(
        "/api/video/color-correct",
        json={"project": "cc-trav", "path": "../../etc/passwd", "color_correction": None},
    )
    assert resp.status_code == 400


def test_color_correct_requires_project_and_path(sync_client):
    resp = sync_client.post(
        "/api/video/color-correct", json={"project": "", "path": "x.mp4"}
    )
    assert resp.status_code == 400
    resp = sync_client.post(
        "/api/video/color-correct", json={"project": "p", "path": ""}
    )
    assert resp.status_code == 400


# --- color-correct/bulk --------------------------------------------------


def test_color_correct_bulk_mixed_copy_and_encode(sync_client, monkeypatch):
    """Zero-CC items are copied raw; non-zero items run through (mocked) ffmpeg.
    Resulting ZIP carries one entry per item with the right _cc suffixing."""
    _make_video("ccb-proj", "raw.mp4", b"RAWBYTES")
    _make_video("ccb-proj", "graded.mp4", b"GRADEDSRC")

    async def fake_cc(src, dst, cc, scale=None):
        with open(dst, "wb") as f:
            f.write(b"GRADED-OUT")

    monkeypatch.setattr(video_router, "run_color_correct", fake_cc)

    resp = sync_client.post(
        "/api/video/color-correct/bulk",
        json={
            "project": "ccb-proj",
            "items": [
                {"path": "raw.mp4", "color_correction": None},
                {"path": "graded.mp4", "color_correction": {"contrast": 10}},
            ],
        },
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = set(zf.namelist())
    assert names == {"raw.mp4", "graded_cc.mp4"}
    assert zf.read("raw.mp4") == b"RAWBYTES"
    assert zf.read("graded_cc.mp4") == b"GRADED-OUT"


def test_color_correct_bulk_records_ffmpeg_errors(sync_client, monkeypatch):
    """A per-item ffmpeg failure lands in _errors.txt instead of aborting the batch."""
    _make_video("ccb-err", "ok.mp4", b"OKSRC")
    _make_video("ccb-err", "bad.mp4", b"BADSRC")

    async def fake_cc(src, dst, cc, scale=None):
        if "bad" in src:
            raise RuntimeError("encoder exploded")
        with open(dst, "wb") as f:
            f.write(b"OK-OUT")

    monkeypatch.setattr(video_router, "run_color_correct", fake_cc)

    resp = sync_client.post(
        "/api/video/color-correct/bulk",
        json={
            "project": "ccb-err",
            "items": [
                {"path": "ok.mp4", "color_correction": {"contrast": 5}},
                {"path": "bad.mp4", "color_correction": {"contrast": 5}},
            ],
        },
    )
    assert resp.status_code == 200
    zf = zipfile.ZipFile(BytesIO(resp.content))
    names = set(zf.namelist())
    assert "ok_cc.mp4" in names
    assert "_errors.txt" in names
    assert "encoder exploded" in zf.read("_errors.txt").decode()


def test_color_correct_bulk_too_many_items_400(sync_client):
    items = [{"path": f"v{i}.mp4"} for i in range(video_router._CC_BULK_MAX_ITEMS + 1)]
    resp = sync_client.post(
        "/api/video/color-correct/bulk",
        json={"project": "p", "items": items},
    )
    assert resp.status_code == 400


def test_color_correct_bulk_requires_project_and_items(sync_client):
    resp = sync_client.post("/api/video/color-correct/bulk", json={"items": [{"path": "x"}]})
    assert resp.status_code == 400
    resp = sync_client.post("/api/video/color-correct/bulk", json={"project": "p", "items": []})
    assert resp.status_code == 400


def test_color_correct_bulk_blocks_traversal(sync_client, isolated_projects_root):
    get_project_video_dir("ccb-trav").mkdir(parents=True, exist_ok=True)
    resp = sync_client.post(
        "/api/video/color-correct/bulk",
        json={
            "project": "ccb-trav",
            "items": [{"path": "../../../secret", "color_correction": {"contrast": 5}}],
        },
    )
    assert resp.status_code == 400


def test_distinct_submission_ids_keep_the_full_uuid(monkeypatch):
    import datetime
    import types
    import uuid
    from routers import video

    values = iter([uuid.UUID("a1b20000-0000-4000-8000-000000000001"),
                   uuid.UUID("a1b2ffff-ffff-4fff-8fff-ffffffffffff")])
    class Clock:
        @staticmethod
        def now():
            return datetime.datetime(2026, 10, 4, 3, 30)
    monkeypatch.setattr(video, "datetime", Clock)
    monkeypatch.setattr(video, "uuid", types.SimpleNamespace(uuid4=lambda: next(values)))
    first = video._make_job_id("grok", "same prompt twice")
    second = video._make_job_id("grok", "same prompt twice")
    assert first != second, "distinct paid submissions must not share the four-hex prefix ID"
    assert first.endswith("a1b20000000040008000000000000001")
    assert second.endswith("a1b2ffffffff4fff8fffffffffffffff")
