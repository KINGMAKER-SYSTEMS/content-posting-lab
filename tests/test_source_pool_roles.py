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


def _master_row(**extra):
    value = json.loads(BTL.read_bytes())
    value["masters"].append({**value["masters"][0], "sourceId": "zz-backup-night-drive", "sha256": "d" * 64,
                             "storageKey": "source-dna/backup/" + "d" * 64 + ".mp4", **extra})
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_a_backup_may_declare_its_own_brightness_inside_the_bounds_only():
    library = parse_source_dna_manifest(_master_row(role="backup", gradeOverride={"brightness": 0.65}))
    assert library.masters[1].grade_override == {"brightness": 0.65}
    assert library.sha256 == parse_source_dna_manifest(BTL.read_bytes()).sha256
    for bad in ({"brightness": 0.3}, {"brightness": 1.2}, {"contrast": 1.1}, {}, {"brightness": True}):
        with pytest.raises(SourceDnaError):
            parse_source_dna_manifest(_master_row(role="backup", gradeOverride=bad))
    with pytest.raises(SourceDnaError):
        parse_source_dna_manifest(_master_row(role="primary", gradeOverride={"brightness": 0.65}))


def test_the_backup_grade_contract_matches_what_the_lab_writes():
    # The same bytes are pinned in the Worker
    # (control-plane-worker/test/fixtures/content-lab/backup-grade-receipt.v1.json),
    # whose check must admit them: the Lab output passes the Worker check.
    from services.clip_variation import GRADE_BANDS, cut_grade
    from services.source_treatment import source_treatment_receipt
    doc = json.loads((Path(__file__).parent / "fixtures/backup_grade_receipt.v1.json").read_text())
    recipe = doc["recipeRenderTreatment"]
    receipt = doc["sourceTreatment"]
    filters = cut_grade(doc["pageId"], "d" * 64 + ":120000:8000",
                        {**recipe["filters"], **doc["masterGradeOverride"]})
    again = source_treatment_receipt({"jobId": receipt["generationJobId"], "recipeSpecHash": receipt["recipeSpecHash"]},
                                     recipe, receipt["sourceSha256"], clip_speed=receipt["visualTreatment"]["clipSpeed"],
                                     clip_crop=receipt["visualTreatment"]["clipCrop"], filters=filters)
    assert again == receipt
    brightness = receipt["visualTreatment"]["filters"]["brightness"]
    assert abs(brightness - 0.65) <= GRADE_BANDS["brightness"], "jitter rides on top of the declared value"
    assert receipt["sourceRecipeTreatment"]["filters"]["brightness"] == 0.5, "the recipe stays the recipe"
