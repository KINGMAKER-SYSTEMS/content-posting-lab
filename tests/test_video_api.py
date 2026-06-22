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


def test_save_jobs_is_atomic(isolated_projects_root):
    """_save_jobs writes through atomic_save: valid JSON, no .tmp sidecar left.

    A direct write_text() could leave jobs.json truncated on a mid-write crash;
    routing through services.json_store.atomic_save (tmp + fsync + os.replace)
    is the guard. This pins the wiring so it can't regress to a raw write.
    """
    video_router.jobs.clear()
    video_router.jobs["job-1"] = {
        "project": "atomic-jobs",
        "videos": [{"status": "done"}],
    }
    video_router._save_jobs("atomic-jobs")

    p = video_router._jobs_path("atomic-jobs")
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["job-1"]["project"] == "atomic-jobs"
    # atomic_save renames its tmp away — none may linger in the project dir
    assert not list(p.parent.glob("*.tmp"))


def test_save_jobs_unwritable_swallows_error(isolated_projects_root, monkeypatch):
    """atomic_save raising OSError (e.g. disk full / unwritable) is caught,
    not propagated — an in-flight render must not crash the request."""
    video_router.jobs.clear()
    video_router.jobs["job-x"] = {"project": "save-proj", "videos": []}

    def boom(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(video_router, "atomic_save", boom)
    # Must not raise.
    video_router._save_jobs("save-proj")


def test_save_jobs_writes_atomically_no_tmp_leak(isolated_projects_root):
    """_save_jobs goes through atomic_save (tmp + fsync + replace): the final
    jobs.json exists and no .tmp scratch file is left behind."""
    video_router.jobs.clear()
    video_router.jobs["atomic-1"] = {
        "project": "atomic-proj",
        "videos": [{"status": "done", "file": "a.mp4"}],
    }
    video_router._save_jobs("atomic-proj")

    proj_dir = isolated_projects_root / "atomic-proj"
    assert (proj_dir / "jobs.json").exists()
    # No orphaned tmp file from the atomic write.
    assert not any(proj_dir.glob("jobs.json.*.tmp"))
