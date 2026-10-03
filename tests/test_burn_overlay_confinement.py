"""POST /api/burn/overlay source-path containment.

The check compared string prefixes: `str(resolved).startswith(str(project_root))`.
A project named `p` has root `<projects>/p`, which is a string prefix of
`<projects>/page_roster.json`; `c` is a prefix of `<projects>/cookies.txt`. With
videoPath `clips/../../page_roster.json` the "burn" queued a copy of the roster
into `projects/p/burned/<batch>/burned_000.mp4`, a media path that /send,
/send-batch and GET /projects/... all serve. /api/burn is not behind any key
while APP_API_KEY is unset, so this server-side containment is the real guard.

The background burn is stubbed: a refused request must never queue it.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
from routers import burn as burn_router


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture
def queued(monkeypatch):
    calls: list[tuple] = []

    async def _fake_burn_background(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(burn_router, "_burn_background", _fake_burn_background)
    return calls


@pytest.fixture
def volume_state(isolated_projects_root):
    root = isolated_projects_root
    (root / "page_roster.json").write_text('{"pages": {}}', encoding="utf-8")
    (root / "cookies.txt").write_text("# cookies", encoding="utf-8")
    (root / "control_plane_jobs.json").write_text("{}", encoding="utf-8")
    return root


def _overlay(client, project, video_path, batch_id="b1"):
    return client.post(
        "/api/burn/overlay",
        json={"project": project, "batchId": batch_id, "index": 0, "videoPath": video_path},
    )


@pytest.mark.parametrize(
    "project,video_path",
    [
        ("p", "clips/../../page_roster.json"),
        ("p", "../../page_roster.json"),
        ("c", "clips/../../cookies.txt"),
        ("c", "../../cookies.txt"),
        ("control", "clips/../../control_plane_jobs.json"),
    ],
)
def test_overlay_refuses_sibling_prefix_state_files(client, queued, volume_state, project, video_path):
    (volume_state / project / "clips").mkdir(parents=True, exist_ok=True)
    (volume_state / project / "videos").mkdir(parents=True, exist_ok=True)
    r = _overlay(client, project, video_path)
    assert r.status_code == 400, r.text
    assert queued == []
    assert not (volume_state / project / "burned" / "b1" / "burned_000.mp4").exists()


def test_overlay_refuses_symlink_out_of_project(client, queued, volume_state):
    clips = volume_state / "sym" / "clips"
    clips.mkdir(parents=True)
    (clips / "roster.mp4").symlink_to(volume_state / "page_roster.json")
    r = _overlay(client, "sym", "clips/roster.mp4")
    assert r.status_code == 400, r.text
    assert queued == []


@pytest.mark.parametrize("batch_id", ["../../x", "..", "a/b", ".hidden"])
def test_overlay_refuses_traversing_batch_id(client, queued, volume_state, batch_id):
    videos = volume_state / "bp" / "videos"
    videos.mkdir(parents=True)
    (videos / "v.mp4").write_bytes(b"mp4")
    r = _overlay(client, "bp", "v.mp4", batch_id=batch_id)
    assert r.status_code == 400, r.text
    assert queued == []


@pytest.mark.parametrize("video_path", ["v.mp4", "clips/c.mp4"])
def test_overlay_accepts_project_media(client, queued, volume_state, video_path):
    videos = volume_state / "good" / "videos"
    videos.mkdir(parents=True)
    (videos / "v.mp4").write_bytes(b"mp4")
    clips = volume_state / "good" / "clips"
    clips.mkdir(parents=True)
    (clips / "c.mp4").write_bytes(b"mp4")
    r = _overlay(client, "good", video_path)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert len(queued) == 1


def test_is_within(tmp_path):
    from services.fsutil import is_within

    root = tmp_path / "p"
    root.mkdir()
    (tmp_path / "page_roster.json").write_text("{}")
    (root / "a.mp4").write_bytes(b"")
    (tmp_path / "p-evil").mkdir()
    assert is_within(root / "a.mp4", root)
    assert not is_within(root, root)
    assert not is_within(tmp_path / "page_roster.json", root)
    assert not is_within(root / ".." / "page_roster.json", root)
    assert not is_within(tmp_path / "p-evil" / "x", root)
