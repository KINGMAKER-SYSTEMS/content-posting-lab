import os

import pytest
from fastapi import HTTPException

from project_manager import (
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_video_dir,
)
from routers import pipeline as pipeline_router


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


def test_projects_list_counts_nested_generated_videos(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Nested Clips"})
    assert created.status_code == 201

    video_dir = get_project_video_dir("nested-clips")
    nested = video_dir / "hailuo" / "prompt-bucket"
    nested.mkdir(parents=True)
    (nested / "clip_000.mp4").write_bytes(b"video")

    response = sync_client.get("/api/projects")
    assert response.status_code == 200
    project = next(p for p in response.json()["projects"] if p["name"] == "nested-clips")
    assert project["video_count"] == 1
    assert project["last_activity"] is not None


def test_recent_videos_returns_newest_across_projects(sync_client):
    sync_client.post("/api/projects", json={"name": "Older Project"})
    sync_client.post("/api/projects", json={"name": "Newer Project"})

    older_dir = get_project_video_dir("older-project") / "wan"
    newer_dir = get_project_video_dir("newer-project") / "hailuo"
    older_dir.mkdir(parents=True)
    newer_dir.mkdir(parents=True)
    older = older_dir / "older.mp4"
    newer = newer_dir / "newer.mp4"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    response = sync_client.get("/api/projects/videos/recent?limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [v["name"] for v in payload["videos"]] == ["newer.mp4", "older.mp4"]
    assert payload["videos"][0]["project"] == "newer-project"
    assert payload["videos"][0]["url"] == "/projects/newer-project/videos/hailuo/newer.mp4"


# ── pipeline._mint_random_alias — CF email alias minting ──────────────────────
#
# Covers the three previously-untested branches of _mint_random_alias:
#   1. collision detection when desired_local already exists -> 409
#   2. destination_override routing (verified pass-through vs unverified -> 409)
#   3. CF rule-create failure -> surfaced as 502 (NOT silently swallowed)

VERIFIED_DEST = "henry@risingtidesent.com"


class _FakeResp:
    """Minimal stand-in for an httpx.Response used by _mint_random_alias."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Replaces httpx.AsyncClient inside the destinations GET so we never
    hit the network. Returns a fixed list of verified destinations."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeResp(
            {"result": [{"email": VERIFIED_DEST, "verified": True}]}
        )


def _patch_cf(monkeypatch, *, existing_aliases=None, create_raises=False):
    """Wire up the CF-facing dependencies of _mint_random_alias to fakes."""
    monkeypatch.setattr(
        pipeline_router,
        "cf_get_config",
        lambda: {
            "configured": True,
            "account_id": "acct",
            "token": "tok",
            "domain": "risingtidesviral.com",
        },
    )
    monkeypatch.setattr(pipeline_router.httpx, "AsyncClient", _FakeAsyncClient)

    async def _fake_list_rules():
        return [
            {"matchers": [{"value": a}]} for a in (existing_aliases or [])
        ]

    monkeypatch.setattr(pipeline_router, "cf_list_rules", _fake_list_rules)

    created = {}

    async def _fake_create_rule(alias_local, destination):
        if create_raises:
            raise RuntimeError("cloudflare 500")
        created["alias_local"] = alias_local
        created["destination"] = destination
        return {"id": "rule-123"}

    monkeypatch.setattr(pipeline_router, "cf_create_rule", _fake_create_rule)
    return created


async def test_mint_alias_collision_returns_409(monkeypatch):
    """A desired_local that already exists must 409, not silently re-use."""
    _patch_cf(
        monkeypatch,
        existing_aliases=["samb-truck-04@risingtidesviral.com"],
    )
    with pytest.raises(HTTPException) as exc:
        await pipeline_router._mint_random_alias(desired_local="samb-truck-04")
    assert exc.value.status_code == 409
    assert "already taken" in exc.value.detail


async def test_mint_alias_unverified_override_returns_409(monkeypatch):
    """destination_override that isn't verified on CF must 409."""
    _patch_cf(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await pipeline_router._mint_random_alias(
            destination_override="stranger@example.com",
            desired_local="samb-truck-05",
        )
    assert exc.value.status_code == 409
    assert "not verified" in exc.value.detail


async def test_mint_alias_verified_override_is_used(monkeypatch):
    """A verified destination_override routes the rule to that address."""
    created = _patch_cf(monkeypatch)
    info = await pipeline_router._mint_random_alias(
        destination_override=VERIFIED_DEST,
        desired_local="samb-truck-06",
    )
    assert info["alias"] == "samb-truck-06@risingtidesviral.com"
    assert info["destination"] == VERIFIED_DEST
    assert created["destination"] == VERIFIED_DEST
    assert info["rule_id"] == "rule-123"


async def test_mint_alias_cf_create_failure_raises_502(monkeypatch):
    """A failed CF rule create must surface as 502 — not swallowed."""
    _patch_cf(monkeypatch, create_raises=True)
    with pytest.raises(HTTPException) as exc:
        await pipeline_router._mint_random_alias(desired_local="samb-truck-07")
    assert exc.value.status_code == 502
    assert "CF rule create failed" in exc.value.detail


async def test_mint_alias_random_local_skips_collisions(monkeypatch):
    """With no desired_local, a colliding random alias retries to a free one."""
    # Force the first generated random local to collide, then succeed.
    locals_seq = iter(["acct-aaaaaaaa", "acct-bbbbbbbb"])
    monkeypatch.setattr(
        pipeline_router, "_random_alias_local", lambda: next(locals_seq)
    )
    created = _patch_cf(
        monkeypatch,
        existing_aliases=["acct-aaaaaaaa@risingtidesviral.com"],
    )
    info = await pipeline_router._mint_random_alias()
    assert info["alias"] == "acct-bbbbbbbb@risingtidesviral.com"
    assert created["alias_local"] == "acct-bbbbbbbb"
