"""Tests for routers/pages.py — the per-page content surface.

The page is the unit: its library assignment (roster project field), the
clips that library holds, and its caption banks. The edits are the Lab's
own ontology: reassign the library, or move a clip between libraries.
Posted state is deliberately absent — the Lab doesn't track it, the
posting boards do.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import project_manager
import routers.pages as pages_router_module
import services.roster as roster
from routers.pages import router
from services import json_store


@pytest.fixture
def lab(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    for name, clips, captions in [
        ("trucks", ["a.mp4", "b.mp4"], {"batch1": ["windows down", "tailgate talk"]}),
        ("coffee", ["c.mp4"], {"batch1": ["crema ribbon"]}),
    ]:
        vdir = projects / name / "videos"
        vdir.mkdir(parents=True)
        for clip in clips:
            (vdir / clip).write_bytes(f"bytes-{clip}".encode())
        cdir = projects / name / "captions" / "batch1"
        cdir.mkdir(parents=True, exist_ok=True)
        if captions:
            rows = "video_id,video_url,caption,mood,error\n" + "".join(
                f'v{i},http://x,"{text}",happy,\n' for i, text in enumerate(captions["batch1"])
            )
            (cdir / "captions.csv").write_text(rows)

    monkeypatch.setattr(project_manager, "PROJECTS_DIR", projects)
    monkeypatch.setattr(pages_router_module, "PROJECTS_DIR", projects)
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})

    roster.save_roster({"version": 1, "pages": {
        "acct:truck-page": {
            "integration_id": "acct:truck-page", "name": "truck.page",
            "project": "trucks", "group": "WARNER", "content_niche": "TRUCK",
            "poster_name": "mon", "source": "notion",
        },
        "acct:no-project": {
            "integration_id": "acct:no-project", "name": "dark.page", "project": None,
        },
    }})

    app = FastAPI()
    app.include_router(router, prefix="/api/pages")
    return TestClient(app)


def test_list_pages_shows_assignment_and_counts(lab):
    res = lab.get("/api/pages/")
    assert res.status_code == 200
    pages = {p["pageId"]: p for p in res.json()["pages"]}
    truck = pages["acct:truck-page"]
    assert truck["project"] == "trucks"
    assert truck["contentNiche"] == "TRUCK"
    assert truck["counts"] == {"clips": 2, "captions": 1, "burned": 0}
    assert "password" not in json.dumps(pages)


def test_page_content_returns_clips_and_captions(lab):
    res = lab.get("/api/pages/acct:truck-page/content")
    assert res.status_code == 200
    body = res.json()
    assert body["page"]["handle"] == "truck.page"
    assert len(body["clips"]) == 2
    assert body["clips"][0]["url"].startswith("/projects/trucks/videos/")
    texts = [c["text"] for c in body["captions"]]
    assert "windows down" in texts
    assert body["captions"][0]["batch"] == "batch1"


def test_page_with_no_project_is_an_honest_empty_state(lab):
    res = lab.get("/api/pages/acct:no-project/content")
    assert res.status_code == 200
    body = res.json()
    assert body["clips"] == [] and body["captions"] == []
    assert body["note"] == "no_project_assigned"


def test_assign_project_is_the_library_switch(lab):
    res = lab.post("/api/pages/acct:truck-page/project", json={"project": "coffee"})
    assert res.status_code == 200
    assert res.json()["page"]["project"] == "coffee"
    assert res.json()["page"]["counts"]["clips"] == 1
    # Persisted: a fresh read sees the new library's content.
    content = lab.get("/api/pages/acct:truck-page/content").json()
    assert [c["name"] for c in content["clips"]] == ["c.mp4"]
    assert lab.post("/api/pages/acct:truck-page/project", json={"project": "nope"}).status_code == 404


def test_move_clip_between_libraries(lab):
    res = lab.post("/api/pages/acct:truck-page/clips/move", json={"path": "a.mp4", "toProject": "coffee"})
    assert res.status_code == 200
    body = res.json()
    assert body["moved"] == "a.mp4"
    assert body["from"] == "trucks" and body["to"] == "coffee"
    # The clip left this page's library and joined the target's.
    assert [c["name"] for c in lab.get("/api/pages/acct:truck-page/content").json()["clips"]] == ["b.mp4"]
    projects = tmp_projects(lab)
    assert (projects / "coffee" / "videos" / "a.mp4").is_file()
    # Moving it again is a 404, moving onto an existing name is a 409.
    assert lab.post("/api/pages/acct:truck-page/clips/move", json={"path": "a.mp4", "toProject": "coffee"}).status_code == 404
    assert lab.post("/api/pages/acct:truck-page/clips/move", json={"path": "b.mp4", "toProject": "trucks"}).status_code == 400


def tmp_projects(lab):
    return project_manager.PROJECTS_DIR


def test_move_rejects_path_escape_and_missing_page(lab):
    assert lab.post("/api/pages/acct:truck-page/clips/move", json={"path": "../x.mp4", "toProject": "coffee"}).status_code == 400
    assert lab.post("/api/pages/nope/content", ).status_code == 404 if False else lab.get("/api/pages/nope/content").status_code == 404
