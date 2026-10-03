"""Job management keeps current and legacy jobs within their clip directory."""

import json
from urllib.parse import quote
from zipfile import ZipFile

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routers import clipper


def _client(monkeypatch, clipper_dir):
    app = FastAPI()
    app.include_router(clipper.router, prefix="/api/clipper")
    monkeypatch.setattr(clipper, "_get_clipper_dir", lambda project: clipper_dir)
    return TestClient(app)


def test_encoded_dotdot_ids_are_rejected_without_deletion(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    project_sentinel = clipper_dir.parent / "keep.txt"
    project_sentinel.write_text("keep")
    removed = []
    monkeypatch.setattr(clipper, "safe_rmtree", lambda path: removed.append(path) or True)
    client = _client(monkeypatch, clipper_dir)

    for encoded in ("%2E%2E", "%2e%2e"):
        response = client.delete(f"/api/clipper/jobs/{encoded}")
        assert response.status_code == 400, response.text
        assert project_sentinel.read_text() == "keep"
        assert removed == []


def test_symlinked_job_directory_is_refused(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    job_id = "clip-project-09291234-abcd"
    (clipper_dir / job_id).symlink_to(target, target_is_directory=True)
    removed = []
    monkeypatch.setattr(clipper, "safe_rmtree", lambda path: removed.append(path) or True)
    client = _client(monkeypatch, clipper_dir)

    response = client.delete(f"/api/clipper/jobs/{job_id}")
    assert response.status_code == 400, response.text
    assert (target / "keep.txt").read_text() == "keep"
    assert removed == []


@pytest.mark.parametrize("job_id", [
    "clip-project-09291234-abcd",
    "0123456789ab",
    "0123456789abcdef0123456789abcdef",
    "01234567-89ab-cdef-0123-456789abcdef",
    "foo..bar",
    "older job .. saved",
])
def test_legacy_direct_child_can_be_renamed_downloaded_and_deleted(monkeypatch, tmp_path, job_id):
    clipper_dir = tmp_path / "project" / "clips"
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "clip_000.mp4").write_bytes(b"legacy clip")
    monkeypatch.setattr(clipper.r2, "is_configured", lambda: False)
    client = _client(monkeypatch, clipper_dir)
    route = f"/api/clipper/jobs/{quote(job_id, safe='')}"

    renamed = client.patch(f"{route}/rename", json={"label": "Saved clip"})
    assert renamed.status_code == 200, renamed.text
    assert json.loads((job_dir / "job_meta.json").read_text()) == {"label": "Saved clip"}

    downloaded = client.get(f"{route}/download-all")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "application/zip"
    with ZipFile(job_dir / "clips.zip") as archive:
        assert archive.namelist() == ["clip_000.mp4"]
        assert archive.read("clip_000.mp4") == b"legacy clip"

    deleted = client.delete(route)
    assert deleted.status_code == 200, deleted.text
    assert not job_dir.exists()


@pytest.mark.parametrize("operation", ["rename", "download"])
@pytest.mark.parametrize("job_id", ["%2E%2E", "linked-job"])
def test_rename_and_download_reject_traversal_and_symlink_jobs(monkeypatch, tmp_path, operation, job_id):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "clip_000.mp4").write_bytes(b"private clip")
    (clipper_dir / "linked-job").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, clipper_dir)
    route = f"/api/clipper/jobs/{job_id}"

    response = (client.patch(f"{route}/rename", json={"label": "Forbidden"})
                if operation == "rename" else client.get(f"{route}/download-all"))

    assert response.status_code == 400, response.text
    assert not (outside / "job_meta.json").exists()
    assert not (outside / "clips.zip").exists()
    assert (outside / "clip_000.mp4").read_bytes() == b"private clip"


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("invalid path"), RuntimeError("symlink loop")])
def test_path_resolution_errors_return_400(monkeypatch, tmp_path, error):
    def fail_resolution(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(clipper.Path, "resolve", fail_resolution)
    with pytest.raises(HTTPException) as raised:
        clipper._clip_job_path(tmp_path, "job")
    assert raised.value.status_code == 400


def test_null_byte_job_id_returns_400(tmp_path):
    with pytest.raises(HTTPException) as raised:
        clipper._clip_job_path(tmp_path, "job\x00suffix")
    assert raised.value.status_code == 400


def test_missing_job_stays_404_for_every_management_route(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    route = "/api/clipper/jobs/missing"
    assert client.delete(route).status_code == 404
    assert client.patch(f"{route}/rename", json={"label": "Missing"}).status_code == 404
    assert client.get(f"{route}/download-all").status_code == 404


def _upload(client, route, job_id, content):
    if route == "upload-stream":
        return client.post("/api/clipper/upload-stream", params={"job_id": job_id}, content=content)
    return client.post("/api/clipper/upload", data={"job_id": job_id},
                       files={"file": ("video.mp4", content, "video/mp4")})


@pytest.mark.parametrize("route", ["upload", "upload-stream"])
@pytest.mark.parametrize("job_id", ["..", "../outside", "..\\outside", "linked-job", "job\x00suffix"])
def test_upload_cannot_write_through_traversal_or_job_symlink(monkeypatch, tmp_path, route, job_id):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    private_source = outside / "source.mp4"
    private_source.write_bytes(b"keep outside")
    (clipper_dir / "linked-job").symlink_to(outside, target_is_directory=True)
    client = _client(monkeypatch, clipper_dir)

    response = _upload(client, route, job_id, b"overwrite")

    assert response.status_code == 400, response.text
    assert private_source.read_bytes() == b"keep outside"
    assert not (clipper_dir.parent / "source.mp4").exists()
    assert sorted(child.name for child in clipper_dir.iterdir()) == ["linked-job"]


@pytest.mark.parametrize("route", ["upload", "upload-stream"])
@pytest.mark.parametrize("job_id", ["foo..bar", "0123456789ab", "clip-project-09291234-abcd", ""])
def test_upload_accepts_legacy_ids_and_retries_same_job(monkeypatch, tmp_path, route, job_id):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    client = _client(monkeypatch, clipper_dir)

    first = _upload(client, route, job_id, b"first upload")
    assert first.status_code == 200, first.text
    actual_id = first.json()["job_id"]
    if job_id:
        assert actual_id == job_id
    source = clipper_dir / actual_id / "source.mp4"
    assert source.read_bytes() == b"first upload"

    retry = _upload(client, route, actual_id, b"retry upload")
    assert retry.status_code == 200, retry.text
    assert retry.json()["job_id"] == actual_id
    assert source.read_bytes() == b"retry upload"
    assert list(clipper_dir.iterdir()) == [source.parent]


def test_real_minted_job_directory_is_deleted(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "project" / "clips"
    job_id = "clip-project-09291234-abcd"
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "clip_000.mp4").write_bytes(b"clip")
    client = _client(monkeypatch, clipper_dir)

    response = client.delete(f"/api/clipper/jobs/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True, "job_id": job_id}
    assert not job_dir.exists()


def test_legacy_uuid_job_directory_is_deleted(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "project" / "clips"
    job_id = "0123456789ab"
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True)
    client = _client(monkeypatch, clipper_dir)

    response = client.delete(f"/api/clipper/jobs/{job_id}")
    assert response.status_code == 200, response.text
    assert not job_dir.exists()


def test_path_components_are_rejected(monkeypatch, tmp_path):
    clipper_dir = tmp_path / "project" / "clips"
    clipper_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    removed = []
    monkeypatch.setattr(clipper, "safe_rmtree", lambda path: removed.append(path) or True)
    client = _client(monkeypatch, clipper_dir)

    for job_id in ("..", "../outside", "..\\outside"):
        # Encoded slashes can reach the route as path components depending on
        # the ASGI client, so exercise the shared guard directly as well.
        try:
            clipper._clip_job_path(clipper_dir, job_id)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"accepted unsafe job ID {job_id!r}")
    assert (outside / "keep.txt").read_text() == "keep"
    assert removed == []
