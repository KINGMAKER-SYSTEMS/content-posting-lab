import pytest
from httpx import ASGITransport, AsyncClient
from app import app
from project_manager import get_project_recreate_dir
from routers.recreate import _PROMPT_GEN_SYSTEM


@pytest.mark.anyio
async def test_list_recreate_jobs_empty():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/recreate/jobs", params={"project": "quick-test"})
        assert r.status_code == 200
        data = r.json()
        assert data["jobs"] == []


@pytest.mark.anyio
async def test_delete_nonexistent_job_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/fake-id", params={"project": "quick-test"})
        assert r.status_code == 404


@pytest.mark.anyio
async def test_delete_existing_job_removes_dir():
    job_dir = get_project_recreate_dir("quick-test") / "real-job"
    job_dir.mkdir(parents=True)
    (job_dir / "clip.mp4").write_bytes(b"x")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/real-job", params={"project": "quick-test"})
        assert r.status_code == 200
        assert r.json()["deleted"] is True
    assert not job_dir.exists()


@pytest.mark.anyio
async def test_delete_job_survives_rmtree_failure(monkeypatch):
    """A locked/permission-blocked dir must not 500 the DELETE route — the
    rmtree is best-effort (ignore_errors=True), so the route still returns 200."""
    import routers.recreate as recreate

    job_dir = get_project_recreate_dir("quick-test") / "locked-job"
    job_dir.mkdir(parents=True)

    def boom(path, ignore_errors=False, **kwargs):
        # Mirror shutil.rmtree semantics: only raise when errors aren't ignored.
        if not ignore_errors:
            raise PermissionError("dir is locked")

    monkeypatch.setattr(recreate.shutil, "rmtree", boom)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.delete("/api/recreate/jobs/locked-job", params={"project": "quick-test"})
        assert r.status_code == 200
        assert r.json()["deleted"] is True


def test_recreate_prompt_system_stays_minimal():
    system = _PROMPT_GEN_SYSTEM.lower()
    assert "one plain" in system
    assert "under 30 words" in system
    assert "style stacks" in system
    assert "2-4 sentence" not in system
