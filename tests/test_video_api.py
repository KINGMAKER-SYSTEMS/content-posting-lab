import json
import time
import zipfile
from io import BytesIO

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
    """write_text raising OSError (e.g. disk full / unwritable) is caught,
    not propagated — an in-flight render must not crash the request."""
    video_router.jobs.clear()
    video_router.jobs["job-x"] = {"project": "save-proj", "videos": []}

    def boom(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(video_router.Path, "write_text", boom)
    # Must not raise.
    video_router._save_jobs("save-proj")


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
