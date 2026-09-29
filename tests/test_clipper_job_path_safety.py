"""DELETE /jobs/{job_id} must only remove a real, minted job directory."""

from fastapi import FastAPI
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
