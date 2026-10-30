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
from tests.source_dna_fixture import isolate_source_dna_registry

_BTL_FILE = Path(__file__).resolve().parents[1] / "recipes/source-dna/pov-night-core-between-the-lines66-automation-v2.json"


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    isolate_source_dna_registry(monkeypatch, tmp_path)


class _PrimaryOnly:
    """BTL's library as it was before any backup: its primary masters only."""

    @staticmethod
    def read_bytes() -> bytes:
        value = json.loads(_BTL_FILE.read_bytes())
        value["masters"] = [row for row in value["masters"] if row.get("role", "primary") == "primary"]
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


BTL = _PrimaryOnly


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
    assert abs(brightness - 0.5) <= GRADE_BANDS["brightness"], "jitter rides on the page look, never a backup override"
    assert 0.46 <= brightness <= 0.5, "the receipt stays at or below the 0.5 page look (never brighter)"
    assert receipt["sourceRecipeTreatment"]["filters"]["brightness"] == 0.5, "the recipe stays the recipe"
    # The backup is a real BTL backup in the registry, and it declares no
    # override: it renders at the page look (source_dna_registry._grade_override).
    declared = {master.sha256: master.grade_override
                for master in parse_source_dna_manifest(_BTL_FILE.read_bytes()).masters if master.role == "backup"}
    assert declared[doc["backupMasterSha256"]] is None
    assert doc["masterGradeOverride"] == {}, "no override means the recipe look"


def test_a_backup_cut_grade_receipt_stays_inside_the_worker_band_of_the_page_look():
    # Lead 2026-10-26: backups render at the page look. Eric's gb20 rule means a
    # cut is never brighter than the page look, and the Worker admits a backup
    # grade only inside GRADE_BANDS of the recipe (brightness 0.46-0.54 around
    # 0.5). A 0.65 override would be refused (a wasted encode), so it is gone.
    from services.clip_variation import GRADE_BANDS, cut_grade
    from services.source_treatment import source_treatment_receipt
    library = parse_source_dna_manifest(_BTL_FILE.read_bytes())
    backup = next(master for master in library.masters if master.role == "backup")
    assert backup.grade_override is None
    recipe = {"filters": {"brightness": 0.5, "contrast": 1.05}, "clipSpeed": 1.0}
    filters = cut_grade(library.page_id, f"{backup.sha256}:120000:8000", dict(recipe["filters"]))
    receipt = source_treatment_receipt({"jobId": "grade-band-1", "recipeSpecHash": "sha256:" + "a" * 64},
                                       recipe, backup.sha256, clip_speed=0.88,
                                       clip_crop={"zoom": 1.55, "focusX": 0.47, "focusY": 0.52},
                                       filters=filters)
    brightness = receipt["visualTreatment"]["filters"]["brightness"]
    assert abs(brightness - 0.5) <= GRADE_BANDS["brightness"]
    assert 0.46 <= brightness <= 0.5, "a cut is never brighter than the 0.5 page look"


def test_btl_lists_its_night_backup_without_moving_its_version():
    library = parse_source_dna_manifest(_BTL_FILE.read_bytes())
    roles = {master.filename: (master.role, master.grade_override) for master in library.masters}
    assert roles["IMG_0766.mp4"] == ("backup", None)
    assert [master.role for master in library.masters].count("primary") == 1
    assert library.sha256 == "1c99ac5f394ba9a329120d2be11468fccd758ca4eb981d3513426a3e149303fb", \
        "the version the Worker binds BTL's clips to (its primary-only library bytes)"


def _btl_catalog_version(monkeypatch, manifest_dir):
    import services.dossier_ingredients as ingredients
    from services.master_pages_contract import intent_hash
    from services.shipstream_source_manifest import ShipStreamSourceMissing
    from tests.master_pages_fixtures import master_pages

    def missing(*_args, **_kwargs):
        raise ShipStreamSourceMissing("no vault manifest in this test")

    monkeypatch.setattr(ingredients, "SOURCE_DNA_MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(ingredients, "load_shipstream_source_projection", missing)
    page = "acct:between-the-lines66"
    intent, _ = master_pages(page, handle="between.the.lines66", content_niche="POV — Night Core",
                             content_engine="sourced_video", vault_url="https://shipstream.test/vault/between.the.lines66")
    revision = intent_hash(intent)
    catalog = ingredients.build_dossier_ingredient_catalog(page, intent, revision)
    fmt = next(entry for entry in catalog["formats"] if entry["formatId"] == "pov-night-core")
    selection = next(entry for entry in fmt["productionSelections"]
                     if entry["sourceLibraryId"] == "pov-night-core-between-the-lines66-automation-v2")
    resolved = ingredients.selected_dossier_catalog_version(page, intent, revision, "pov-night-core",
                                                            dict(selection), load_shipstream=False)
    assert resolved == selection["catalogVersion"]
    return resolved


def test_btl_catalog_version_is_the_same_with_and_without_its_backups(monkeypatch, tmp_path):
    # The catalog version is what a page's recipe publication binds; a moved
    # version makes every BTL job "executor unavailable" and every clip stale.
    with_backups = _btl_catalog_version(monkeypatch, _BTL_FILE.parent)
    for path in _BTL_FILE.parent.glob("*.json"):
        value = json.loads(path.read_bytes())
        value["masters"] = [row for row in value["masters"] if row.get("role", "primary") == "primary"]
        (tmp_path / path.name).write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    without = _btl_catalog_version(monkeypatch, tmp_path)
    assert with_backups == without == "sha256:c53ccc349810f423e03b57723ea312181bd8b54a9f765c50570f5bd612546e2d"


def test_a_declared_backup_grade_is_refused_on_a_page_that_does_not_vary():
    value = json.loads(_master_row(role="backup", gradeOverride={"brightness": 0.65}))
    value["pageId"] = "acct:not-varied"
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(SourceDnaError, match="clip-varying"):
        parse_source_dna_manifest(raw)
    value["masters"][1].pop("gradeOverride")
    parse_source_dna_manifest((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


# Backups render at the page look (lead 2026-10-26), so none declares a grade
# override. Both sides pin this exact set of backup hashes; grade_override None.
BTL_BACKUP_SHAS = {
    "ddbf980600f44e250dc3a076b0f9f3afa1f334520a2c9abb0221ba3d24ae6a63": None,  # IMG_0766
    "b716e9d6986d300a407be3c0080abc1fc1cc943421c2d6e909d761d88f13f26b": None,  # IMG_4566
    "34b8726404321c9a8078d37a0e00afeabe11255082becce06d76ac51171b797b": None,  # IMG_4569
    "dddada2a63cfe26a0f9d053cd26e67b891f4dd92103648e9b72525434a2a36a8": None,  # IMG_6775 0-1121
    "8526541b2588e91cacd2e500a566b22eed4ba90dc6062efed05d85673788996c": None,  # IMG_6775 1121-2239
    "be30bd5fd73edf765c00256b7d8df09133890736c96f1910e22301b2972b639d": None,  # IMG_6776 0-761
    "f20c57eb6486147e162ea46fbcd3f2f42286980f73b61ab5c20aea6ee57a08ad": None,  # IMG_6776 761-1516
}


def test_btl_backups_are_exactly_the_declared_night_footage():
    library = parse_source_dna_manifest(_BTL_FILE.read_bytes())
    backups = {master.sha256: master.grade_override for master in library.masters if master.role == "backup"}
    assert set(backups) == set(BTL_BACKUP_SHAS)
    assert all(override is None for override in backups.values()), "backups render at the page look: no override"
    primary = [master for master in library.masters if master.role == "primary"]
    assert [master.source_id for master in primary] == ["yt-okw0bu2qfvu-600-1200"]
    # A split file keeps one origin: its parts share an identity and continue its timeline.
    parts = sorted((master for master in library.masters if master.filename.startswith("IMG_6775")),
                   key=lambda master: master.source_offset_ms)
    assert parts[0].provenance["sourceUrl"] == parts[1].provenance["sourceUrl"]
    assert parts[1].source_offset_ms >= parts[0].source_offset_ms + parts[0].duration_ms
