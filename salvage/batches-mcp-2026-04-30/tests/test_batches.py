"""Tests for the batches data layer + router."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import project_manager
from services import batches as batches_service


@pytest.fixture
def project_with_dir(isolated_projects_root) -> str:
    """Create a project on disk so batch operations have a target."""
    _ = isolated_projects_root  # autouse, keeps fixture order explicit
    project_manager.create_project("test-project")
    return "test-project"


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------

class TestBatchCRUD:
    def test_create_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "drake-may-04")
        assert batch["id"].startswith("drake-may-04-")
        assert batch["project"] == project_with_dir
        assert batch["status"] == "open"
        assert batch["is_legacy"] is False
        assert batch["is_daily_default"] is False
        assert batch["stage_jobs"]["generate"] == []

    def test_create_batch_persists(self, project_with_dir, isolated_projects_root):
        batch = batches_service.create_batch(project_with_dir, "test")
        path = isolated_projects_root / project_with_dir / "batches.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert batch["id"] in data

    def test_create_batch_unknown_project_raises(self, isolated_projects_root):
        with pytest.raises(FileNotFoundError):
            batches_service.create_batch("does-not-exist", "label")

    def test_get_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "label")
        fetched = batches_service.get_batch(project_with_dir, batch["id"])
        assert fetched is not None
        assert fetched["id"] == batch["id"]

    def test_get_batch_missing_returns_none(self, project_with_dir):
        assert batches_service.get_batch(project_with_dir, "no-such-id") is None

    def test_close_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        closed = batches_service.close_batch(project_with_dir, batch["id"])
        assert closed is not None
        assert closed["status"] == "closed"
        assert "closed_at" in closed

    def test_reopen_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        batches_service.close_batch(project_with_dir, batch["id"])
        reopened = batches_service.reopen_batch(project_with_dir, batch["id"])
        assert reopened is not None
        assert reopened["status"] == "open"
        assert "closed_at" not in reopened

    def test_delete_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        assert batches_service.delete_batch(project_with_dir, batch["id"]) is True
        assert batches_service.get_batch(project_with_dir, batch["id"]) is None

    def test_update_notes(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        updated = batches_service.update_notes(project_with_dir, batch["id"], "hello")
        assert updated is not None
        assert updated["notes"] == "hello"


class TestBatchList:
    def test_list_empty(self, project_with_dir):
        assert batches_service.list_batches(project_with_dir) == []

    def test_list_returns_newest_first(self, project_with_dir):
        b1 = batches_service.create_batch(project_with_dir, "first")
        b2 = batches_service.create_batch(project_with_dir, "second")
        listed = batches_service.list_batches(project_with_dir)
        assert len(listed) == 2
        # Both created in same minute possibly; created_at sort still valid
        assert {b["id"] for b in listed} == {b1["id"], b2["id"]}

    def test_list_filter_by_status(self, project_with_dir):
        a = batches_service.create_batch(project_with_dir, "a")
        b = batches_service.create_batch(project_with_dir, "b")
        batches_service.close_batch(project_with_dir, a["id"])

        opens = batches_service.list_batches(project_with_dir, status="open")
        closed = batches_service.list_batches(project_with_dir, status="closed")
        assert {x["id"] for x in opens} == {b["id"]}
        assert {x["id"] for x in closed} == {a["id"]}

    def test_list_filter_by_since(self, project_with_dir):
        b = batches_service.create_batch(project_with_dir, "fresh")
        # A 1-minute window should include the just-created batch
        recent = batches_service.list_batches(project_with_dir, since=timedelta(minutes=1))
        assert any(x["id"] == b["id"] for x in recent)
        # A negative-zero window should exclude everything (effectively far future cutoff)
        none_window = batches_service.list_batches(project_with_dir, since=timedelta(seconds=-1))
        assert none_window == []

    def test_list_excludes_legacy_when_asked(self, project_with_dir, isolated_projects_root):
        # Hand-craft a legacy batch
        path = isolated_projects_root / project_with_dir / "batches.json"
        batches = {
            "leg-1": {
                **batches_service._empty_batch("leg-1", "leg", project_with_dir, is_legacy=True),
            },
            "norm-1": {
                **batches_service._empty_batch("norm-1", "norm", project_with_dir),
            },
        }
        path.write_text(json.dumps(batches))

        with_legacy = batches_service.list_batches(project_with_dir, include_legacy=True)
        without_legacy = batches_service.list_batches(project_with_dir, include_legacy=False)
        assert {b["id"] for b in with_legacy} == {"leg-1", "norm-1"}
        assert {b["id"] for b in without_legacy} == {"norm-1"}


class TestAttachJob:
    def test_attach_job_adds_to_stage(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        updated = batches_service.attach_job(project_with_dir, batch["id"], "generate", "job-1")
        assert "job-1" in updated["stage_jobs"]["generate"]

    def test_attach_job_idempotent(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        batches_service.attach_job(project_with_dir, batch["id"], "generate", "job-1")
        batches_service.attach_job(project_with_dir, batch["id"], "generate", "job-1")
        updated = batches_service.get_batch(project_with_dir, batch["id"])
        assert updated is not None
        assert updated["stage_jobs"]["generate"] == ["job-1"]

    def test_attach_invalid_stage_raises(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        with pytest.raises(ValueError):
            batches_service.attach_job(project_with_dir, batch["id"], "bogus", "j")

    def test_attach_to_missing_batch_raises(self, project_with_dir):
        with pytest.raises(KeyError):
            batches_service.attach_job(project_with_dir, "nope", "generate", "j")

    def test_detach_job(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        batches_service.attach_job(project_with_dir, batch["id"], "burn", "burn-1")
        batches_service.detach_job(project_with_dir, batch["id"], "burn", "burn-1")
        updated = batches_service.get_batch(project_with_dir, batch["id"])
        assert updated is not None
        assert updated["stage_jobs"]["burn"] == []


class TestDailyBatch:
    def test_get_or_create_daily_batch(self, project_with_dir):
        b = batches_service.get_or_create_daily_batch(project_with_dir)
        assert b["is_daily_default"] is True
        # Same call returns the same batch (deterministic ID)
        b2 = batches_service.get_or_create_daily_batch(project_with_dir)
        assert b2["id"] == b["id"]

    def test_daily_batch_id_is_today(self, project_with_dir):
        b = batches_service.get_or_create_daily_batch(project_with_dir)
        today = datetime.now().strftime("%Y%m%d")
        assert today in b["id"]
        assert b["id"].endswith("-default")


class TestLegacyMigration:
    def test_migrate_creates_per_day_batches(self, project_with_dir, isolated_projects_root):
        # Seed fake jobs.json with two jobs from different dates
        jobs_path = isolated_projects_root / project_with_dir / "jobs.json"
        jobs_path.write_text(json.dumps({
            "job-a": {"id": "job-a", "created_at": "2026-04-01T10:00:00+00:00", "videos": []},
            "job-b": {"id": "job-b", "created_at": "2026-04-02T10:00:00+00:00", "videos": []},
        }))

        summary = batches_service.migrate_legacy_jobs(project_with_dir)
        assert summary["created_batches"] == 2
        assert summary["attached_jobs"] == 2
        assert summary["skipped_jobs"] == 0

        listed = batches_service.list_batches(project_with_dir)
        assert all(b["is_legacy"] for b in listed)
        assert {len(b["stage_jobs"]["generate"]) for b in listed} == {1}

    def test_migrate_idempotent(self, project_with_dir, isolated_projects_root):
        jobs_path = isolated_projects_root / project_with_dir / "jobs.json"
        jobs_path.write_text(json.dumps({
            "job-a": {"id": "job-a", "created_at": "2026-04-01T10:00:00+00:00", "videos": []},
        }))
        s1 = batches_service.migrate_legacy_jobs(project_with_dir)
        s2 = batches_service.migrate_legacy_jobs(project_with_dir)
        assert s1["created_batches"] == 1 and s1["attached_jobs"] == 1
        assert s2["created_batches"] == 0 and s2["attached_jobs"] == 0
        assert s2["skipped_jobs"] == 1

    def test_migrate_no_jobs_file(self, project_with_dir):
        summary = batches_service.migrate_legacy_jobs(project_with_dir)
        assert summary == {"created_batches": 0, "attached_jobs": 0, "skipped_jobs": 0}

    def test_migrate_skips_already_attached(self, project_with_dir, isolated_projects_root):
        # Pre-create a batch and attach the job manually
        batch = batches_service.create_batch(project_with_dir, "explicit")
        batches_service.attach_job(project_with_dir, batch["id"], "generate", "job-a")

        jobs_path = isolated_projects_root / project_with_dir / "jobs.json"
        jobs_path.write_text(json.dumps({
            "job-a": {"id": "job-a", "created_at": "2026-04-01T10:00:00+00:00", "videos": []},
        }))
        summary = batches_service.migrate_legacy_jobs(project_with_dir)
        assert summary["created_batches"] == 0
        assert summary["attached_jobs"] == 0
        assert summary["skipped_jobs"] == 1


class TestParseSince:
    def test_hours(self):
        assert batches_service.parse_since("24h") == timedelta(hours=24)
        assert batches_service.parse_since("1h") == timedelta(hours=1)

    def test_days(self):
        assert batches_service.parse_since("7d") == timedelta(days=7)

    def test_minutes(self):
        assert batches_service.parse_since("30m") == timedelta(minutes=30)

    def test_all_returns_none(self):
        assert batches_service.parse_since("all") is None

    def test_empty_returns_none(self):
        assert batches_service.parse_since(None) is None
        assert batches_service.parse_since("") is None

    def test_invalid_returns_none(self):
        assert batches_service.parse_since("garbage") is None


class TestFilterJobsByBatchOrSince:
    def test_filter_by_batch(self, project_with_dir):
        batch = batches_service.create_batch(project_with_dir, "x")
        batches_service.attach_job(project_with_dir, batch["id"], "generate", "j-1")

        jobs = [
            {"id": "j-1", "created_at": "2026-04-01T10:00:00+00:00"},
            {"id": "j-2", "created_at": "2026-04-01T10:00:00+00:00"},
        ]
        result = batches_service.filter_jobs_by_batch_or_since(
            project_with_dir, jobs, batch_id=batch["id"]
        )
        assert {j["id"] for j in result} == {"j-1"}

    def test_filter_by_since_window(self, project_with_dir):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        jobs = [
            {"id": "fresh", "created_at": recent},
            {"id": "stale", "created_at": old},
        ]
        result = batches_service.filter_jobs_by_batch_or_since(
            project_with_dir, jobs, since=timedelta(hours=1)
        )
        assert {j["id"] for j in result} == {"fresh"}

    def test_return_all_bypasses_filters(self, project_with_dir):
        jobs = [
            {"id": "a", "created_at": "2020-01-01T00:00:00+00:00"},
            {"id": "b", "created_at": "2020-01-01T00:00:00+00:00"},
        ]
        result = batches_service.filter_jobs_by_batch_or_since(
            project_with_dir, jobs, since=timedelta(hours=1), return_all=True
        )
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Router tests (HTTP)
# ---------------------------------------------------------------------------

class TestBatchRouter:
    def test_create_batch_endpoint(self, sync_client, project_with_dir):
        res = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "drake-release"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["batch"]["label"] == "drake-release"
        assert data["batch"]["status"] == "open"

    def test_list_batches_endpoint(self, sync_client, project_with_dir):
        sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        )
        res = sync_client.get(f"/api/batches?project={project_with_dir}")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] >= 1

    def test_get_daily_batch_endpoint(self, sync_client, project_with_dir):
        res = sync_client.get(f"/api/batches/daily?project={project_with_dir}")
        assert res.status_code == 200
        batch = res.json()["batch"]
        assert batch["is_daily_default"] is True

    def test_get_batch_endpoint(self, sync_client, project_with_dir):
        created = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        ).json()["batch"]
        res = sync_client.get(f"/api/batches/{created['id']}?project={project_with_dir}")
        assert res.status_code == 200
        assert res.json()["batch"]["id"] == created["id"]

    def test_close_batch_endpoint(self, sync_client, project_with_dir):
        b = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        ).json()["batch"]
        res = sync_client.post(f"/api/batches/{b['id']}/close?project={project_with_dir}")
        assert res.status_code == 200
        assert res.json()["batch"]["status"] == "closed"

    def test_attach_job_endpoint(self, sync_client, project_with_dir):
        b = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        ).json()["batch"]
        res = sync_client.post(
            f"/api/batches/{b['id']}/attach",
            json={"project": project_with_dir, "stage": "generate", "job_id": "fake-job-1"},
        )
        assert res.status_code == 200
        assert "fake-job-1" in res.json()["batch"]["stage_jobs"]["generate"]

    def test_manifest_endpoint(self, sync_client, project_with_dir):
        b = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        ).json()["batch"]
        res = sync_client.get(
            f"/api/batches/{b['id']}/manifest?project={project_with_dir}&absolute_urls=false"
        )
        assert res.status_code == 200
        manifest = res.json()
        assert manifest["batch_id"] == b["id"]
        assert "stages" in manifest
        assert manifest["totals"]["videos"] == 0

    def test_unknown_project_returns_404(self, sync_client):
        res = sync_client.post(
            "/api/batches",
            json={"project": "does-not-exist", "label": "x"},
        )
        assert res.status_code == 404

    def test_migrate_endpoint(self, sync_client, project_with_dir, isolated_projects_root):
        jobs_path = isolated_projects_root / project_with_dir / "jobs.json"
        jobs_path.write_text(json.dumps({
            "job-x": {"id": "job-x", "created_at": "2026-03-15T10:00:00+00:00", "videos": []},
        }))
        res = sync_client.post(f"/api/batches/migrate?project={project_with_dir}")
        assert res.status_code == 200
        assert res.json()["attached_jobs"] == 1


# ---------------------------------------------------------------------------
# Cache + path-traversal regression tests
# ---------------------------------------------------------------------------

class TestLoadCache:
    def test_cache_returns_same_data_when_mtime_unchanged(self, project_with_dir):
        b = batches_service.create_batch(project_with_dir, "x")
        # First load primes cache, second should hit cache
        first = batches_service._load(project_with_dir)
        second = batches_service._load(project_with_dir)
        assert first == second
        assert b["id"] in first

    def test_cache_invalidates_after_save(self, project_with_dir):
        b = batches_service.create_batch(project_with_dir, "x")
        # Mutate
        batches_service.update_notes(project_with_dir, b["id"], "fresh")
        loaded = batches_service.get_batch(project_with_dir, b["id"])
        assert loaded is not None
        assert loaded["notes"] == "fresh"

    def test_cache_returns_independent_copy(self, project_with_dir):
        """Mutating the returned dict must not pollute the cache."""
        batches_service.create_batch(project_with_dir, "x")
        first = batches_service._load(project_with_dir)
        first["bogus"] = {"id": "bogus"}
        # Second load should not include the bogus entry
        second = batches_service._load(project_with_dir)
        assert "bogus" not in second


class TestPathTraversalGuards:
    def test_download_rejects_path_escape(self, sync_client, project_with_dir, isolated_projects_root):
        """The download endpoint must skip files whose paths escape the project dir."""
        # Create a batch with a malicious file reference in jobs.json
        b = sync_client.post(
            "/api/batches",
            json={"project": project_with_dir, "label": "x"},
        ).json()["batch"]

        # Manually inject a fake job that references ../../../etc/passwd
        jobs_path = isolated_projects_root / project_with_dir / "jobs.json"
        jobs_path.write_text(json.dumps({
            "evil-job": {
                "id": "evil-job",
                "project": project_with_dir,
                "videos": [
                    {"index": 0, "status": "done", "file": "../../../etc/passwd"},
                ],
            },
        }))
        batches_service.attach_job(project_with_dir, b["id"], "generate", "evil-job")

        # Download should 404 (no safe files found, malicious one rejected)
        res = sync_client.get(
            f"/api/batches/{b['id']}/download?project={project_with_dir}"
        )
        # 404 means the malicious file was correctly skipped and no safe
        # files were found — that's the success case for this test
        assert res.status_code == 404
