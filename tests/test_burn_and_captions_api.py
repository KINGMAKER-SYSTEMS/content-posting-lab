from project_manager import (
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_clips_dir,
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


def test_burn_folder_rename_happy_path(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Happy"})
    assert created.status_code == 201

    video_dir = get_project_video_dir("rename-happy") / "oldname"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "clip.mp4").write_bytes(b"video")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-happy"},
        json={"folder": "oldname", "new_name": "new name"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["old_folder"] == "oldname"
    # "new name" sanitizes to "new-name"
    assert body["new_folder"] == "new-name"

    # Disk state: old dir gone, new dir contains the file
    assert not (get_project_video_dir("rename-happy") / "oldname").exists()
    assert (get_project_video_dir("rename-happy") / "new-name" / "clip.mp4").exists()

    # videos list reflects the rename
    videos = sync_client.get(
        "/api/burn/videos", params={"project": "rename-happy"}
    ).json()["videos"]
    folders = {v["folder"] for v in videos}
    assert "new-name" in folders
    assert "oldname" not in folders
    # `created` field is now populated
    assert all("created" in v and isinstance(v["created"], int) for v in videos)


def test_burn_folder_rename_collision(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Collide"})
    assert created.status_code == 201

    root = get_project_video_dir("rename-collide")
    (root / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "beta").mkdir(parents=True, exist_ok=True)
    (root / "alpha" / "a.mp4").write_bytes(b"a")
    (root / "beta" / "b.mp4").write_bytes(b"b")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-collide"},
        json={"folder": "alpha", "new_name": "beta"},
    )
    assert resp.status_code == 409
    # Both dirs still present — no partial state
    assert (root / "alpha").exists()
    assert (root / "beta").exists()


def test_burn_folder_rename_rejects_virtual_and_root(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Virt"})
    assert created.status_code == 201

    # Virtual run_* folder
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "run_abc123", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Root
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "(root)", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Clips root
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "clips", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Path traversal in new_name
    (get_project_video_dir("rename-virt") / "foo").mkdir(parents=True, exist_ok=True)
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "foo", "new_name": "../evil"},
    )
    assert resp.status_code == 400
    assert (get_project_video_dir("rename-virt") / "foo").exists()


def test_burn_folder_rename_clips_preserves_prefix(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Clips"})
    assert created.status_code == 201

    clips_dir = get_project_clips_dir("rename-clips") / "job_xyz"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "clip_001.mp4").write_bytes(b"clip")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-clips"},
        json={"folder": "clips/job_xyz", "new_name": "rooftop-shoot"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_folder"] == "clips/rooftop-shoot"

    assert not (get_project_clips_dir("rename-clips") / "job_xyz").exists()
    assert (get_project_clips_dir("rename-clips") / "rooftop-shoot" / "clip_001.mp4").exists()
