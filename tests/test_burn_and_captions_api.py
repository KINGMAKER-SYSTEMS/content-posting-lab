from project_manager import (
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_video_dir,
)


def test_burn_list_endpoints(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Burn Suite"})
    assert created.status_code == 201

    video_dir = get_project_video_dir("burn-suite") / "provider-a"
    caption_dir = get_project_caption_dir("burn-suite") / "artist123"
    burn_dir = get_project_burn_dir("burn-suite") / "batch-x"

    video_dir.mkdir(parents=True, exist_ok=True)
    caption_dir.mkdir(parents=True, exist_ok=True)
    burn_dir.mkdir(parents=True, exist_ok=True)

    (video_dir / "clip.mp4").write_bytes(b"video")
    (caption_dir / "captions.csv").write_text(
        "video_id,video_url,caption,error\n"
        "123,https://tiktok.com/@artist/video/123,hello world,\n",
        encoding="utf-8",
    )
    (burn_dir / "burned_000.mp4").write_bytes(b"burned")

    videos_response = sync_client.get(
        "/api/burn/videos", params={"project": "burn-suite"}
    )
    assert videos_response.status_code == 200
    videos = videos_response.json()["videos"]
    assert len(videos) == 1
    assert videos[0]["name"] == "clip.mp4"

    captions_response = sync_client.get(
        "/api/burn/captions", params={"project": "burn-suite"}
    )
    assert captions_response.status_code == 200
    sources = captions_response.json()["sources"]
    assert len(sources) == 1
    assert sources[0]["username"] == "artist123"
    assert sources[0]["captions"][0]["text"] == "hello world"

    batches_response = sync_client.get(
        "/api/burn/batches", params={"project": "burn-suite"}
    )
    assert batches_response.status_code == 200
    batches = batches_response.json()["batches"]
    assert len(batches) == 1
    assert batches[0]["id"] == "batch-x"


def test_caption_export_endpoint(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Caption Suite"})
    assert created.status_code == 201

    username = "creator1"
    csv_dir = get_project_caption_dir("caption-suite") / username
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / "captions.csv"
    csv_path.write_text(
        "video_id,video_url,caption,error\n"
        "1,https://tiktok.com/@creator1/video/1,my caption,\n",
        encoding="utf-8",
    )

    response = sync_client.get(
        f"/api/captions/export/{username}", params={"project": "caption-suite"}
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "my caption" in response.text

    missing = sync_client.get(
        "/api/captions/export/missing-user", params={"project": "caption-suite"}
    )
    assert missing.status_code == 404
