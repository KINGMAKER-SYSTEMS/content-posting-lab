from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import project_manager
from app import app
from routers import burn as burn_router
from routers import captions as captions_router
from routers import projects as projects_router
from routers import video as video_router


@pytest.fixture
def temp_project_dir():
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(autouse=True)
def isolated_projects_root(monkeypatch, tmp_path):
    base_dir = tmp_path / "workspace"
    projects_dir = base_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(project_manager, "BASE_DIR", base_dir)
    monkeypatch.setattr(project_manager, "PROJECTS_DIR", projects_dir)

    monkeypatch.setattr(projects_router, "BASE_DIR", base_dir)
    monkeypatch.setattr(projects_router, "PROJECTS_DIR", projects_dir)

    monkeypatch.setattr(burn_router, "BASE_DIR", base_dir)
    monkeypatch.setattr(burn_router, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(burn_router, "FONT_DIR", base_dir / "fonts")

    monkeypatch.setattr(app_module, "PROJECTS_DIR", projects_dir)

    yield projects_dir


@pytest.fixture(autouse=True)
def reset_in_memory_state():
    video_router.jobs.clear()
    captions_router._ws_clients.clear()
    yield
    video_router.jobs.clear()
    captions_router._ws_clients.clear()


@pytest.fixture
async def async_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def sync_client():
    with TestClient(app) as client:
        yield client
