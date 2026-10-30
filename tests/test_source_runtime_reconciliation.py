"""Legacy jobs are reconciled only after explicit, revision-bound retirement."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import source_runtime_reconciliation as reconcile
from services.generation_recovery import runner_lock
from tests.test_control_plane_source_imports import lab, _body, HEADERS


JOB = "cpl-1111111111111111"
RUNTIME = "a" * 32
PAGE = "acct:runtime-reconciliation"


@pytest.fixture
def history(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    generation = private / "generated"
    generation.mkdir(mode=0o700)
    job = {"jobId": JOB, "pageId": PAGE, "runtimeId": RUNTIME,
           "sourceKind": "dossier_source_dna", "status": "running", "clips": [],
           "createdAt": "2026-10-01T00:00:00+00:00", "token": "never-print-this-token",
           "sourceLibraryId": "library", "sourceLibraryHash": "hash", "recipeVersion": "v1",
           "sourceCuts": [{"slotId": "slot1", "masterSha256": "a" * 64,
                           "startMs": 0, "durationMs": 5000}]}
    store = {"version": 1, "jobs": {JOB: job}, "byIdempotency": {"original-key": JOB},
             "served": {"historical": True}, "unrelatedHistory": [1, 2, 3]}
    path = private / "jobs.json"
    path.write_text(json.dumps(store))
    return path, generation, store


def inspect(history):
    path, root, _ = history
    return reconcile.inspect_job(path, JOB, retired_runtime=RUNTIME, generation_root=root)


def apply(history, **overrides):
    path, root, _ = history
    args = dict(retired_runtime=RUNTIME, generation_root=root,
                expected_job_sha256=inspect(history)["jobRevisionSha256"],
                runtime_stopped_confirmed=True)
    args.update(overrides)
    return reconcile.reconcile_job(path, JOB, **args)


def as_import(history):
    path, root, store = history
    parent = root / PAGE / "source-imports" / JOB
    parent.mkdir(parents=True, mode=0o700)
    media = parent / "original.mp4"
    media.write_bytes(b"old bytes retained exactly")
    store["jobs"][JOB].update(sourceKind="page_source_import", artifactRoot=str(parent))
    path.write_text(json.dumps(store))
    return parent, media


def test_dry_run_is_secret_free_and_makes_no_files(history, capsys):
    path, root, _ = history
    before, entries = path.read_bytes(), set(path.parent.rglob("*"))
    result = inspect(history)
    assert result["status"] == "ready"
    assert reconcile.main(["--store", str(path), "--job-id", JOB,
                           "--retired-runtime", RUNTIME]) == 0
    assert "never-print-this-token" not in capsys.readouterr().out
    assert path.read_bytes() == before
    assert set(path.parent.rglob("*")) == entries


def test_read_only_inventory_lists_only_safe_candidate_metadata(history, capsys):
    path, _, _ = history
    before = path.read_bytes()
    result = reconcile.list_legacy_jobs(path)
    assert result["status"] == "read_only_inventory"
    assert len(result["jobs"]) == 1 and result["jobs"][0]["runtimeId"] == RUNTIME
    assert reconcile.main(["--store", str(path), "--list"]) == 0
    output = capsys.readouterr().out
    assert "never-print-this-token" not in output
    assert "sourceCuts" not in output
    assert path.read_bytes() == before
    assert not Path(str(path) + ".lock").exists()


def test_inventory_does_not_print_malformed_runtime_values(history):
    path, _, store = history
    store["jobs"][JOB]["runtimeId"] = "malformed-private-value"
    path.write_text(json.dumps(store))
    result = reconcile.list_legacy_jobs(path)
    assert result["jobs"] == [] and result["unknownOrInvalid"] == 1
    assert "malformed-private-value" not in json.dumps(result)


def test_apply_requires_external_retirement_confirmation(history):
    with pytest.raises(reconcile.ReconciliationRefused, match="runtime_retirement_not_confirmed"):
        apply(history, runtime_stopped_confirmed=False)


def test_source_reconciliation_releases_only_retired_unpublished_windows(history):
    from routers import control_plane as cp
    path, _, original = history
    recipe = SimpleNamespace(masters=[SimpleNamespace(sha256="a" * 64)],
                             source_library_id="library", source_library_hash="hash")
    assert "a" * 64 + ":0:5000" in cp._source_dna_cut_ledger(original, recipe, "v1")[0]
    result = apply(history)
    assert result["status"] == "reconciled"
    saved = json.loads(path.read_text())
    assert saved["jobs"][JOB]["status"] == "failed"
    assert saved["jobs"][JOB]["error"] == "generation_runtime_restarted"
    assert cp._source_dna_cut_ledger(saved, recipe, "v1")[0] == set()
    for key in ("byIdempotency", "served", "unrelatedHistory"):
        assert saved[key] == original[key]
    before = copy.deepcopy(original["jobs"][JOB])
    after = copy.deepcopy(saved["jobs"][JOB])
    for field in ("status", "error", "completedAt"):
        before.pop(field, None)
        after.pop(field, None)
    assert before == after


def test_busy_claim_still_refuses_after_operator_confirmation(history):
    path, _, _ = history
    before = path.read_bytes()
    with runner_lock(path, JOB) as owned:
        assert owned
        with pytest.raises(reconcile.ReconciliationRefused, match="runner_still_active"):
            apply(history)
    assert path.read_bytes() == before


def test_changed_job_refuses_the_reviewed_revision(history):
    path, _, store = history
    revision = inspect(history)["jobRevisionSha256"]
    store["jobs"][JOB]["progress"] = 10
    path.write_text(json.dumps(store))
    before = path.read_bytes()
    with pytest.raises(reconcile.ReconciliationRefused, match="job_changed_retry_dry_run"):
        apply(history, expected_job_sha256=revision)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value,status", [
    ("runtimeId", "a-live-runtime", "runtime_not_selected"),
    ("sourceKind", "generated", "not_a_source_job"),
    ("status", "completed", "terminal_history_unchanged"),
    ("status", "cancelled", "terminal_history_unchanged"),
    ("status", "failed", "terminal_history_unchanged"),
    ("sourceClaimVersion", 1, "claimed_or_malformed_requires_normal_recovery"),
    ("sourceClaimVersion", True, "claimed_or_malformed_requires_normal_recovery"),
])
def test_other_jobs_and_claimed_or_malformed_rows_remain_untouched(history, field, value, status):
    path, _, store = history
    store["jobs"][JOB][field] = value
    path.write_text(json.dumps(store))
    before = path.read_bytes()
    assert inspect(history)["status"] == status
    assert reconcile.reconcile_job(path, JOB, retired_runtime=RUNTIME,
                                   expected_job_sha256="a" * 64,
                                   runtime_stopped_confirmed=True)["status"] == status
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["dossier_source_dna", "page_source_import"])
def test_existing_delivered_artifacts_are_not_failed_or_moved(history, kind):
    path, _, store = history
    if kind == "page_source_import":
        as_import(history)
    store["jobs"][JOB]["clips"] = [{"path": "published.mp4"}]
    path.write_text(json.dumps(store))
    with pytest.raises(reconcile.ReconciliationRefused, match="existing_artifacts_require_reconciliation"):
        inspect(history)


@pytest.mark.parametrize("status", ["running", "restartable_failure"])
def test_import_adoption_preserves_old_bytes_in_a_separate_attempt(history, status):
    path, root, store = history
    parent, media = as_import(history)
    if status == "restartable_failure":
        store["jobs"][JOB].update(status="failed", error="source_import_runtime_restarted")
        path.write_text(json.dumps(store))
    assert apply(history)["status"] == "reconciled"
    job = json.loads(path.read_text())["jobs"][JOB]
    assert job["status"] == "failed" and job["error"] == "source_import_runtime_restarted"
    assert job["sourceImportClaimVersion"] == 1
    assert Path(job["artifactRoot"]) == parent / job["sourceImportAttemptId"]
    assert Path(job["artifactRoot"]).is_dir()
    assert list(Path(job["artifactRoot"]).iterdir()) == []
    assert media.read_bytes() == b"old bytes retained exactly"
    assert job["token"] == store["jobs"][JOB]["token"]


def test_import_layout_mismatch_refuses_without_creating_attempt(history):
    path, root, store = history
    parent, _ = as_import(history)
    store["jobs"][JOB]["artifactRoot"] = str(root / "another-job")
    path.write_text(json.dumps(store))
    before = set(parent.iterdir())
    with pytest.raises(reconcile.ReconciliationRefused, match="artifact_root_invalid"):
        inspect(history)
    assert set(parent.iterdir()) == before


def test_import_page_symlink_is_not_followed(history):
    path, root, store = history
    other = root / "another-page"
    other.mkdir()
    (root / PAGE).symlink_to(other, target_is_directory=True)
    job = store["jobs"][JOB]
    job.update(sourceKind="page_source_import",
               artifactRoot=str(root / PAGE / "source-imports" / JOB))
    path.write_text(json.dumps(store))
    with pytest.raises(reconcile.ReconciliationRefused, match="artifact_root_invalid"):
        inspect(history)
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("revision", [None, "", "short", "g" * 64])
def test_apply_refuses_an_absent_or_invalid_reviewed_revision(history, revision):
    path, _, _ = history
    before = path.read_bytes()
    with pytest.raises(reconcile.ReconciliationRefused, match="expected_revision_invalid"):
        apply(history, expected_job_sha256=revision)
    assert path.read_bytes() == before


def test_write_failure_removes_only_its_empty_new_attempt(history, monkeypatch):
    parent, media = as_import(history)
    path, _, _ = history
    before = path.read_bytes()
    def fail(*_):
        raise OSError("fixture write fault")
    monkeypatch.setattr(reconcile, "atomic_save", fail)
    with pytest.raises(OSError, match="fixture write fault"):
        apply(history)
    assert path.read_bytes() == before
    assert list(parent.iterdir()) == [media]


def test_owned_volume_directory_need_not_be_chmodded_private(history):
    path, _, _ = history
    path.parent.chmod(0o755)
    assert apply(history)["status"] == "reconciled"
    assert path.stat().st_mode & 0o077 == 0


def test_group_writable_store_directory_refuses_without_job_change(history):
    path, _, _ = history
    path.parent.chmod(0o775)
    before = path.read_bytes()
    with pytest.raises(reconcile.ReconciliationRefused, match="store_directory_not_owned"):
        apply(history)
    assert path.read_bytes() == before


def test_reconciled_import_can_use_the_real_same_key_retry(lab):
    from routers import control_plane as cp
    client, intent, revision, _ = lab
    created = client.post("/api/control-plane/v1/source-imports", headers=HEADERS,
                          json=_body(intent, revision)).json()
    job_id = created["jobId"]
    store = cp._load_jobs()
    job = store["jobs"][job_id]
    root = Path(job["artifactRoot"])
    parent = root.parent
    root.rmdir()
    original = parent / "old-source.mp4"
    original.write_bytes(b"retained legacy media")
    job.update(status="running", runtimeId=RUNTIME, artifactRoot=str(parent))
    job.pop("sourceImportClaimVersion")
    job.pop("sourceImportAttemptId")
    cp.atomic_save(cp._jobs_path(), store)
    plan = reconcile.inspect_job(cp._jobs_path(), job_id, retired_runtime=RUNTIME,
                                 generation_root=cp._generation_root())
    assert reconcile.reconcile_job(cp._jobs_path(), job_id, retired_runtime=RUNTIME,
        generation_root=cp._generation_root(), expected_job_sha256=plan["jobRevisionSha256"],
        runtime_stopped_confirmed=True)["status"] == "reconciled"
    adopted = cp._load_jobs()["jobs"][job_id]
    response = client.post("/api/control-plane/v1/source-imports", headers=HEADERS,
                           json=_body(intent, revision))
    assert response.status_code == 200 and response.json()["status"] == "queued"
    current = cp._load_jobs()["jobs"][job_id]
    assert current["sourceImportAttemptId"] != adopted["sourceImportAttemptId"]
    assert Path(current["artifactRoot"]) != Path(adopted["artifactRoot"])
    assert original.read_bytes() == b"retained legacy media"
