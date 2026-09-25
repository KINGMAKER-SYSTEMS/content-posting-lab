"""routers/video.py path confinement.

`project` was joined raw onto PROJECTS_DIR for prompts.json / jobs.json, so
`project=..` (or `../x`) read, overwrote or deleted those files outside the
projects root. The videos-dir containment checks compared string prefixes, so a
sibling such as `videos-evil/` passed as "inside" `videos/`.
"""

import json

import pytest

from project_manager import get_project_video_dir


@pytest.fixture
def outside(isolated_projects_root):
    """A directory beside projects/ holding prompts.json and jobs.json."""
    root = isolated_projects_root.parent
    (root / "prompts.json").write_text(json.dumps([{"prompt": "OUTSIDE"}]), encoding="utf-8")
    other = root / "other"
    other.mkdir()
    (other / "prompts.json").write_text(json.dumps([{"prompt": "OTHER"}]), encoding="utf-8")
    (other / "jobs.json").write_text(
        json.dumps({"j-outside": {"id": "j-outside", "project": "../other", "videos": []}}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def sync_client():
    """No lifespan needed for these routes."""
    from fastapi.testclient import TestClient

    import app as app_module

    return TestClient(app_module.app)


BAD_PROJECTS = ["..", "../other", "a/b", "a\\b", ".hidden", "", "x\x00y"]


@pytest.mark.parametrize("project", ["..", "../other"])
def test_list_prompts_refuses_traversal(sync_client, outside, project):
    r = sync_client.get("/api/video/prompts", params={"project": project})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("project", ["..", "../other"])
def test_clear_prompts_refuses_traversal(sync_client, outside, project):
    r = sync_client.delete("/api/video/prompts", params={"project": project})
    assert r.status_code == 400, r.text
    assert (outside / "prompts.json").exists()
    assert (outside / "other" / "prompts.json").exists()


def test_list_jobs_refuses_traversal(sync_client, outside):
    r = sync_client.get("/api/video/jobs", params={"project": "../other"})
    assert r.status_code == 400, r.text


def test_delete_job_refuses_traversal(sync_client, outside):
    r = sync_client.delete("/api/video/jobs/j-outside", params={"project": "../other"})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("endpoint", ["/api/video/bulk-delete", "/api/video/bulk-download"])
def test_bulk_endpoints_refuse_traversal(sync_client, outside, endpoint):
    r = sync_client.post(endpoint, json={"job_ids": ["j-outside"], "project": "../other"})
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("project", BAD_PROJECTS)
def test_delete_file_refuses_bad_project(sync_client, outside, project):
    r = sync_client.request("DELETE", "/api/video/file", params={"project": project, "path": "x.mp4"})
    assert r.status_code == 400, f"{project!r} -> {r.status_code}"


def test_delete_file_refuses_sibling_prefix_dir(sync_client, isolated_projects_root):
    video_dir = get_project_video_dir("sib")
    video_dir.mkdir(parents=True)
    evil = video_dir.parent / "videos-evil"
    evil.mkdir()
    victim = evil / "keep.mp4"
    victim.write_bytes(b"mp4")

    r = sync_client.request(
        "DELETE", "/api/video/file", params={"project": "sib", "path": "../videos-evil/keep.mp4"}
    )
    assert r.status_code == 400, r.text
    assert victim.exists()


def test_delete_file_refuses_symlink_out_of_videos(sync_client, isolated_projects_root):
    video_dir = get_project_video_dir("sym")
    video_dir.mkdir(parents=True)
    victim = isolated_projects_root / "page_roster.json"
    victim.write_text("{}", encoding="utf-8")
    link_dir = video_dir / "linked"
    link_dir.symlink_to(isolated_projects_root)

    r = sync_client.request(
        "DELETE", "/api/video/file", params={"project": "sym", "path": "linked/page_roster.json"}
    )
    assert r.status_code == 400, r.text
    assert victim.exists()


def test_color_correct_refuses_sibling_prefix_dir(sync_client, isolated_projects_root):
    video_dir = get_project_video_dir("ccsib")
    video_dir.mkdir(parents=True)
    evil = video_dir.parent / "videos-evil"
    evil.mkdir()
    (evil / "x.mp4").write_bytes(b"mp4")
    r = sync_client.post(
        "/api/video/color-correct",
        json={"project": "ccsib", "path": "../videos-evil/x.mp4", "color_correction": None},
    )
    assert r.status_code == 400, r.text


def test_valid_project_prompts_still_work(sync_client, isolated_projects_root):
    (isolated_projects_root / "good-proj").mkdir()
    (isolated_projects_root / "good-proj" / "prompts.json").write_text(
        json.dumps([{"prompt": "hi"}]), encoding="utf-8"
    )
    r = sync_client.get("/api/video/prompts", params={"project": "good-proj"})
    assert r.status_code == 200
    assert r.json() == [{"prompt": "hi"}]
