import pytest
from httpx import ASGITransport, AsyncClient
from app import app
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


def test_recreate_prompt_system_stays_minimal():
    system = _PROMPT_GEN_SYSTEM.lower()
    assert "one plain" in system
    assert "under 30 words" in system
    assert "style stacks" in system
    assert "2-4 sentence" not in system
