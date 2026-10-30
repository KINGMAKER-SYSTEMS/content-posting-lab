"""Kernel mount/entrypoint evidence gates the bounded legacy startup repair."""

import asyncio
import json
import os
from pathlib import Path
import runpy
import sys
import types

import pytest
from fastapi.testclient import TestClient

from services import source_runtime_reconciliation as reconcile
from services.generation_recovery import runner_lock


JOB = "cpl-1111111111111111"
OLD = "a" * 32
CURRENT = "b" * 32
MAIN = Path(reconcile.__file__).resolve().parents[1] / "main.py"


@pytest.fixture
def volume(tmp_path):
    root = tmp_path / "volume"
    root.mkdir()
    generation = root / "control_plane_generated"
    generation.mkdir()
    store = root / "control_plane_jobs.json"
    env = {key: "platform-value" for key in (
        "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_DEPLOYMENT_ID", "RAILWAY_VOLUME_NAME")}
    env["RAILWAY_VOLUME_MOUNT_PATH"] = str(root)
    mounts = f"1 0 0:1 / / rw - overlay overlay rw\n2 1 8:1 / {root} rw - ext4 /dev/vdb rw\n"
    return root, generation, store, env, mounts


def proof(volume, **changes):
    root, generation, store, env, mounts = volume
    args = dict(environ=env, mountinfo=mounts, command=["python", str(MAIN)],
                device_for=lambda _: os.makedev(8, 1))
    args.update(changes)
    return reconcile.railway_volume_proof(store, generation, **args)


def history(volume, **updates):
    _, _, store, _, _ = volume
    row = {"jobId": JOB, "pageId": "acct:retired-source", "runtimeId": OLD,
           "sourceKind": "dossier_source_dna", "status": "running", "clips": [],
           "sourceCuts": [{"slotId": "retained-audit-window"}], "token": "do-not-log"}
    row.update(updates)
    store.write_text(json.dumps({"version": 1, "jobs": {JOB: row},
                                 "byIdempotency": {"original-key": JOB}, "served": {}}))
    return row


def install_real_proof_fixture(volume, monkeypatch, **changes):
    actual = reconcile.railway_volume_proof
    _, _, _, env, mounts = volume
    args = dict(environ=env, mountinfo=mounts, command=["python", str(MAIN)],
                device_for=lambda _: os.makedev(8, 1))
    args.update(changes)
    monkeypatch.setattr(reconcile, "railway_volume_proof",
                        lambda path, generation: actual(path, generation, **args))


def test_complete_runtime_and_real_block_mount_evidence_qualifies(volume):
    assert proof(volume) == "verified_serial_railway_volume"


@pytest.mark.parametrize("key", ["RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID",
                                 "RAILWAY_DEPLOYMENT_ID", "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_MOUNT_PATH"])
def test_missing_platform_identity_never_qualifies(volume, key):
    env = dict(volume[3])
    env.pop(key)
    assert proof(volume, environ=env) == "platform_volume_identity_unavailable"


def test_environment_markers_without_a_kernel_mount_do_not_qualify(volume):
    assert proof(volume, mountinfo="1 0 0:1 / / rw - overlay overlay rw\n") == "volume_not_mounted"


@pytest.mark.parametrize("filesystem,source", [("overlay", "overlay"), ("tmpfs", "tmpfs"),
                                               ("nfs", "server:/shared"), ("fuse", "remote")])
def test_foreign_or_ephemeral_filesystems_do_not_prove_exclusive_railway_volume(volume, filesystem, source):
    mounts = volume[4].replace("ext4 /dev/vdb", f"{filesystem} {source}")
    assert proof(volume, mountinfo=mounts) == "volume_block_identity_unverified"


def test_foreign_nested_filesystem_is_refused(volume):
    generation = volume[1]
    mounts = volume[4] + f"3 2 0:55 / {generation} rw - nfs server:/shared rw\n"
    assert proof(volume, mountinfo=mounts) == "source_data_on_nested_filesystem"


def test_mount_record_must_match_actual_data_device(volume):
    assert proof(volume, device_for=lambda _: os.makedev(8, 2)) == "source_data_device_mismatch"


@pytest.mark.parametrize("command", [["python", str(MAIN), "--workers", "2"],
                                     ["uvicorn", "main:app", "--workers", "2"],
                                     ["gunicorn", "app:app"], ["python", "other_main.py"]])
def test_multiple_or_unrecognized_worker_entrypoints_cannot_reconcile(volume, command):
    assert proof(volume, command=command) == "single_process_entrypoint_unverified"


def test_real_entrypoint_pins_one_worker_even_when_environment_requests_multiple(monkeypatch):
    import uvicorn
    fake = types.ModuleType("app")
    fake.app = object()
    monkeypatch.setitem(sys.modules, "app", fake)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    runpy.run_path(str(MAIN), run_name="__main__")
    assert calls[0][1]["workers"] == 1
    config = uvicorn.Config(fake.app, **calls[0][1])
    assert config.workers == 1 and config.reload is False


@pytest.mark.parametrize("updates", [{"runtimeId": CURRENT}, {"runtimeId": "unknown-owner"},
                                    {"sourceClaimVersion": True}, {"sourceClaimVersion": 1},
                                    {"clips": [{"path": "delivered.mp4"}]}, {"status": "completed"}])
def test_current_unknown_claimed_and_delivered_jobs_are_unchanged(volume, monkeypatch, updates):
    history(volume, **updates)
    install_real_proof_fixture(volume, monkeypatch)
    store = volume[2]
    before = store.read_bytes()
    result = reconcile.reconcile_railway_startup(store, volume[1], CURRENT)
    assert result["reconciled"] == 0
    assert store.read_bytes() == before


def test_live_claim_stays_held_even_with_qualified_platform_evidence(volume, monkeypatch):
    history(volume)
    install_real_proof_fixture(volume, monkeypatch)
    before = volume[2].read_bytes()
    with runner_lock(volume[2], JOB) as owned:
        assert owned
        result = reconcile.reconcile_railway_startup(volume[2], volume[1], CURRENT)
    assert result["reconciled"] == 0 and result["manual"] == 1
    assert volume[2].read_bytes() == before


def test_unverified_runtime_performs_no_writes(volume, monkeypatch):
    history(volume)
    install_real_proof_fixture(volume, monkeypatch, mountinfo="")
    store = volume[2]
    before = store.read_bytes()
    result = reconcile.reconcile_railway_startup(store, volume[1], CURRENT)
    assert result["status"] == "skipped_unverified_runtime" and result["reconciled"] == 0
    assert store.read_bytes() == before
    assert not Path(str(store) + ".lock").exists()


@pytest.mark.parametrize("qualified", [False, True, "proof-error"])
def test_real_application_lifespan_repairs_before_workers_or_keeps_serving_without_proof(
    volume, monkeypatch, qualified,
):
    import app as app_module
    import routers.control_plane as cp
    import services.telegram as telegram
    import telegram_bot
    import scraper.frame_extractor as extractor
    import services.abn_assets as assets
    import scripts.migrate_abn_assets as migration

    history(volume)
    install_real_proof_fixture(volume, monkeypatch, mountinfo=volume[4] if qualified is True else "")
    if qualified == "proof-error":
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("fixture kernel evidence unavailable")
        monkeypatch.setattr(reconcile, "railway_volume_proof", unavailable)
    monkeypatch.setattr(cp, "_jobs_path", lambda: volume[2])
    monkeypatch.setattr(cp, "_generation_root_path", lambda: volume[1])
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", CURRENT)
    monkeypatch.setattr(extractor, "sweep_private_cookie_jars", lambda: None)
    monkeypatch.setattr(assets, "assert_migration_complete", lambda: None)
    monkeypatch.setattr(migration, "verify_voice_symlinks", lambda: [])
    async def nothing(*_args, **_kwargs):
        return None
    monkeypatch.setattr(app_module.agenticnews_db, "init_db", nothing)
    monkeypatch.setattr(app_module, "_refresh_tool_status", nothing)
    monkeypatch.setattr(app_module, "_log_startup_validation", lambda *_: None)
    monkeypatch.setattr(telegram, "get_bot_token", lambda: None)
    monkeypatch.setattr(telegram_bot, "start_sounds_bot", nothing)
    monkeypatch.setattr(telegram_bot, "stop_sounds_bot", nothing)
    monkeypatch.setattr(telegram_bot, "stop_bot", nothing)
    monkeypatch.setattr(cp, "start_compaction_scheduler", lambda: None)
    monkeypatch.setattr(cp, "stop_compaction_scheduler", lambda: None)
    monkeypatch.setattr(cp, "shutdown_dossier_generation", nothing)
    started = []
    def workers():
        state = json.loads(volume[2].read_text())["jobs"][JOB]["status"]
        assert state == ("failed" if qualified is True else "running")
        started.append(state)
        return None
    monkeypatch.setattr(app_module, "start_post_render_workers", workers)
    with TestClient(app_module.app) as client:
        assert client.get("/api/health").status_code == 200
    assert started == (["failed"] if qualified is True else ["running"])
