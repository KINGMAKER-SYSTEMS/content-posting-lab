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
    ):
        entry = jobs[job_id]["videos"][index]
        filename = f"fake_{index}.mp4"
        (output_dir / filename).write_bytes(b"fake video")
        entry["status"] = "done"
        entry["file"] = filename
        entry["url"] = f"{url_prefix}/{filename}"

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
