"""Primary and backup masters (Eric 2026-10-22).

A page's primary source is cut as much as possible. A backup is cut only when
the primary has no fresh window, or as a variety cut at most one in five.
Adding a backup never moves the library version the Worker binds clips to.
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from services.control_plane_sources import (
    BACKUP_VARIETY_EVERY, CutUse, plan_source_cuts,
)
from services.source_dna_registry import SourceDnaError, parse_source_dna_manifest
from tests.test_source_recut_variety import _recipe
from tests.test_control_plane_source_execution import offline_shipstream_manifest  # noqa: F401 (fixture)

BTL = Path(__file__).resolve().parents[1] / "recipes/source-dna/pov-night-core-between-the-lines66-automation-v1.json"


def _with_backup(raw: bytes, role="backup") -> bytes:
    value = json.loads(raw)
    backup = {**value["masters"][0], "sourceId": "zz-backup-night-drive", "sha256": "d" * 64,
              "storageKey": "source-dna/pov-night-core/backup/" + "d" * 64 + ".mp4", "filename": "IMG_0766.mp4"}
    if role is not None:
        backup["role"] = role
    value["masters"].append(backup)
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_role_is_optional_and_a_backup_does_not_move_the_library_version():
    raw = BTL.read_bytes()
    current = parse_source_dna_manifest(raw)
    assert [master.role for master in current.masters] == ["primary"]
    pooled = parse_source_dna_manifest(_with_backup(raw))
    assert [master.role for master in pooled.masters] == ["primary", "backup"]
    assert pooled.sha256 == current.sha256, "the Worker's bound version stays the primary's"
    # A changed primary does move it.
    value = json.loads(raw)
    value["masters"][0]["durationMs"] -= 1000
    moved = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert parse_source_dna_manifest(moved).sha256 != current.sha256


def test_a_role_must_be_primary_or_backup_and_a_library_keeps_a_primary():
    raw = BTL.read_bytes()
    with pytest.raises(SourceDnaError):
        parse_source_dna_manifest(_with_backup(raw, role="reserve"))
    value = json.loads(raw)
    value["masters"][0]["role"] = "backup"
    with pytest.raises(SourceDnaError):
        parse_source_dna_manifest((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _pool(primary_ms=600_000, backup_ms=900_000):
    recipe, primary = _recipe(duration_ms=primary_ms)
    backup = replace(primary, source_id="zz-backup", sha256="d" * 64, duration_ms=backup_ms,
                     storage_key="source-dna/backup/" + "d" * 64 + ".mp4", role="backup")
    return replace(recipe, masters=(primary, backup)), primary, backup


def _run(recipe, jobs, per_job=1):
    served, history, cuts = set(), {}, []
    for run in range(jobs):
        plan = plan_source_cuts(recipe, per_job, served, seed=f"job-{run}", history=history)
        for order, cut in enumerate(plan):
            served.add(cut.slot_id)
            history[cut.slot_id] = CutUse(float(run), order)
            cuts.append(cut)
    return cuts


def test_the_primary_is_cut_first_and_a_backup_only_as_rare_variety():
    recipe, primary, backup = _pool()
    cuts = _run(recipe, 60)
    roles = ["backup" if cut.master.sha256 == backup.sha256 else "primary" for cut in cuts]
    assert len(cuts) == 60
    assert 0 < roles.count("backup") <= 60 // BACKUP_VARIETY_EVERY
    for index in range(len(roles) - BACKUP_VARIETY_EVERY + 1):
        assert roles[index:index + BACKUP_VARIETY_EVERY].count("backup") <= 1, roles


def test_the_backup_carries_the_page_once_the_primary_has_no_fresh_window():
    recipe, primary, backup = _pool(primary_ms=6_000)
    cuts = _run(recipe, 30)
    primaries = [cut for cut in cuts if cut.master.sha256 == primary.sha256]
    assert len(cuts) == 30, "supply never stops while a backup has footage"
    assert primaries and cuts[-1].master.sha256 == backup.sha256
    # Every primary cut came before the primary ran dry, except variety backups.
    assert cuts[-5:] == [cut for cut in cuts[-5:] if cut.master.sha256 == backup.sha256]


def test_a_library_without_backups_plans_exactly_as_before():
    recipe, _ = _recipe()
    assert all(master.role == "primary" for master in recipe.masters)
    first = plan_source_cuts(recipe, 5, set(), seed="same")
    assert first == plan_source_cuts(recipe, 5, set(), seed="same")
    assert all(cut.master.role == "primary" for cut in first)
