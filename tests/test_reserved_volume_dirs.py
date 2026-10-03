"""Service-state directories on the projects volume are never projects.

The Railway volume is mounted at /app/projects, so besides real projects it
holds service state: control_plane_generated/, control_plane_recipes/,
agenticnews_assets/, lost+found/ and operator-configured private roots such as
CONTENT_LAB_POST_RENDER_ROOT=/app/projects/_post_render (jobs.sqlite plus
private renders). Any route that took one of those names as `project` could
copy its files into a burn batch (/api/burn/overlay) and then deliver them via
/send-batch, /send or GET /projects/... . Names in the reserved set, and every
name starting with `_`, are refused wherever a project name is accepted.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import project_manager
import telegram_bot as tg_bot
from routers import burn as burn_router
from routers import telegram as tg_router
from services import json_store
from services import telegram as tg

RESERVED = ["_post_render", "control_plane_generated", "control_plane_recipes", "agenticnews_assets", "_anything"]


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture
def state_dirs(isolated_projects_root):
    root = isolated_projects_root
    for name in RESERVED:
        d = root / name
        (d / "clips").mkdir(parents=True)
        (d / "videos").mkdir()
        (d / "jobs.sqlite").write_bytes(b"SQLite format 3\x00")
        (d / "videos" / "private.mp4").write_bytes(b"mp4")
        batch = d / "burned" / "b1"
        batch.mkdir(parents=True)
        (batch / "burned_000.mp4").write_bytes(b"mp4")
    real = root / "real-proj" / "videos"
    real.mkdir(parents=True)
    (real / "v.mp4").write_bytes(b"mp4")
    return root


@pytest.mark.parametrize("name", RESERVED)
def test_sanitize_refuses_reserved(name):
    with pytest.raises(ValueError):
        project_manager.sanitize_project_name(name)


def test_sanitize_still_accepts_ordinary_names():
    assert project_manager.sanitize_project_name("test_project") == "test_project"
    assert project_manager.sanitize_project_name("Drake Release!!!") == "drake-release"


def test_list_projects_skips_reserved(state_dirs):
    names = {p["name"] for p in project_manager.list_projects()}
    assert "real-proj" in names
    assert not names & set(RESERVED)


def test_recent_videos_skip_reserved(client, state_dirs):
    r = client.get("/api/projects/videos/recent")
    assert r.status_code == 200, r.text
    projects = {v["project"] for v in r.json()["videos"]}
    assert projects == {"real-proj"}


def test_create_project_refuses_reserved(isolated_projects_root):
    with pytest.raises(ValueError):
        project_manager.create_project("_post_render")


# ── burn overlay ────────────────────────────────────────────────────────────


@pytest.fixture
def queued(monkeypatch):
    calls: list[tuple] = []

    async def _fake(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(burn_router, "_burn_background", _fake)
    return calls


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("video_path", ["clips/../jobs.sqlite", "private.mp4"])
def test_burn_overlay_refuses_reserved_project(client, queued, state_dirs, name, video_path):
    r = client.post(
        "/api/burn/overlay",
        json={"project": name, "batchId": "bx", "index": 0, "videoPath": video_path},
    )
    assert r.status_code == 400, r.text
    assert queued == []


# ── telegram /send, /send-batch, /assign-batch ──────────────────────────────


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    empty = tg._empty_config()
    empty["staging_group"] = {"chat_id": -100123, "name": "Staging", "topics": {}}
    tg.save_config(empty)
    tg.set_staging_topic("ig1", 7, "Page One")
    calls: list[str] = []
    monkeypatch.setattr(tg_bot, "get_bot", lambda: object())

    async def _send(chat_id, topic_id, file_path, caption=None):
        calls.append(file_path)
        return {"message_id": len(calls), "file_id": "f"}

    monkeypatch.setattr(tg_bot, "send_media_to_topic", _send)
    return calls


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("rel", ["videos/private.mp4", "burned/b1/burned_000.mp4"])
def test_send_refuses_reserved_dirs(client, state_dirs, sent, monkeypatch, name, rel):
    monkeypatch.setattr(tg_router, "PROJECT_ROOT", state_dirs.parent)
    r = client.post(
        "/api/telegram/send",
        json={"integration_id": "ig1", "file_path": str(state_dirs / name / rel)},
    )
    assert r.status_code == 400, r.text
    assert sent == []


@pytest.mark.parametrize("name", RESERVED)
@pytest.mark.parametrize("endpoint", ["/api/telegram/send-batch", "/api/telegram/assign-batch"])
def test_batches_refuse_reserved_project(client, state_dirs, sent, name, endpoint):
    r = client.post(
        endpoint,
        json={"integration_id": "ig1", "integration_ids": ["ig1"], "project": name, "batch_id": "b1"},
    )
    assert r.status_code == 400, r.text
    assert sent == []


def test_send_real_project_media_still_works(client, state_dirs, sent, monkeypatch):
    monkeypatch.setattr(tg_router, "PROJECT_ROOT", state_dirs.parent)
    r = client.post(
        "/api/telegram/send",
        json={"integration_id": "ig1", "file_path": str(state_dirs / "real-proj" / "videos" / "v.mp4")},
    )
    assert r.status_code == 200, r.text
    assert len(sent) == 1


# ── video router ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", RESERVED)
def test_video_prompts_refuse_reserved_project(client, state_dirs, name):
    assert client.get("/api/video/prompts", params={"project": name}).status_code == 400
    assert client.delete("/api/video/prompts", params={"project": name}).status_code == 400
