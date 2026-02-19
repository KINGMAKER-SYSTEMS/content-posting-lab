from project_manager import (
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_video_dir,
)


def test_projects_crud_and_stats(sync_client):
    created = sync_client.post("/api/projects", json={"name": "My Launch"})
    assert created.status_code == 201
    created_payload = created.json()["project"]
    assert created_payload["name"] == "my-launch"

    single = sync_client.get("/api/projects/my-launch")
    assert single.status_code == 200
    assert single.json()["project"]["name"] == "my-launch"

    video_dir = get_project_video_dir("my-launch")
    caption_dir = get_project_caption_dir("my-launch")
    burn_dir = get_project_burn_dir("my-launch")

    (video_dir / "clip.mp4").write_bytes(b"video")
    (caption_dir / "captions.csv").write_text(
        "video_id,video_url,caption,error\n1,u,c,\n"
    )
    (burn_dir / "burned_000.mp4").write_bytes(b"burned")

    stats = sync_client.get("/api/projects/my-launch/stats")
    assert stats.status_code == 200
    stats_payload = stats.json()
    assert stats_payload["videos"]["count"] == 1
    assert stats_payload["captions"]["count"] == 1
    assert stats_payload["burned"]["count"] == 1

    deleted = sync_client.delete("/api/projects/my-launch")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = sync_client.get("/api/projects/my-launch")
    assert missing.status_code == 404


def test_projects_reject_path_traversal(sync_client):
    response = sync_client.post("/api/projects", json={"name": "../../etc"})
    assert response.status_code == 400


def test_projects_list_returns_default(sync_client):
    response = sync_client.get("/api/projects")
    assert response.status_code == 200
    names = [project["name"] for project in response.json()["projects"]]
    assert "quick-test" in names
