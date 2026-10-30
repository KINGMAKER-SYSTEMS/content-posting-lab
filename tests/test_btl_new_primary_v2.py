"""between.the.lines66's new primary master lives only in automation-v2 (10-29).

The retired old primary (3816ff5c, Drive IMG_4962 origin) is shared with other
pages, so BTL must stop cutting it network-wide. The retired automation-v1
library is deleted from the registry, and the class-level RETIRED_MASTER_SHAS
guard refuses a retired master if anyone re-adds it. BTL's only registered
library is pov-night-core-between-the-lines66-automation-v2 (primary 76c34024).
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from services.control_plane_sources import CutUse, plan_source_cuts
from services.source_dna_registry import (
    MANIFEST_DIR,
    parse_source_dna_manifest,
)
from tests.test_source_recut_variety import _recipe
from tests.test_control_plane_source_execution import offline_shipstream_manifest  # noqa: F401 (fixture)
from tests.source_dna_fixture import isolate_source_dna_registry


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    isolate_source_dna_registry(monkeypatch, tmp_path)

DIR = Path(__file__).resolve().parents[1] / "recipes/source-dna"
V2 = "pov-night-core-between-the-lines66-automation-v2"
NEW = "76c34024438df3239bfdd286deb091280f0226ba63b22a0d09b93fdfebaac649"
OLD = "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"
BTL_PAGE = "acct:between-the-lines66"

BTL_BACKUP_SHAS = {
    "ddbf980600f44e250dc3a076b0f9f3afa1f334520a2c9abb0221ba3d24ae6a63",  # IMG_0766
    "b716e9d6986d300a407be3c0080abc1fc1cc943421c2d6e909d761d88f13f26b",  # IMG_4566
    "34b8726404321c9a8078d37a0e00afeabe11255082becce06d76ac51171b797b",  # IMG_4569
    "dddada2a63cfe26a0f9d053cd26e67b891f4dd92103648e9b72525434a2a36a8",  # IMG_6775 0-1121
    "8526541b2588e91cacd2e500a566b22eed4ba90dc6062efed05d85673788996c",  # IMG_6775 1121-2239
    "be30bd5fd73edf765c00256b7d8df09133890736c96f1910e22301b2972b639d",  # IMG_6776 0-761
    "f20c57eb6486147e162ea46fbcd3f2f42286980f73b61ab5c20aea6ee57a08ad",  # IMG_6776 761-1516
}


def _load(library_id):
    raw = (DIR / f"{library_id}.json").read_bytes()
    return raw, parse_source_dna_manifest(raw, library_id)


def test_v2_has_the_new_primary_and_never_the_old_one():
    raw, v2 = _load(V2)
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert raw == canonical.encode()
    assert v2.page_id == BTL_PAGE and v2.format_slug == "pov-night-core"
    primaries = [m for m in v2.masters if m.role == "primary"]
    assert [m.sha256 for m in primaries] == [NEW]
    new = primaries[0]
    assert (new.bytes, new.duration_ms, new.source_offset_ms) == (180_564_887, 600_000, 600_000)
    assert new.storage_key == f"source-dna/pov-night-core/tt-warner-between-the-lines66/{NEW}.mp4"
    assert OLD not in {m.sha256 for m in v2.masters}


def test_v2_keeps_the_same_backups_and_never_the_retired_master():
    _, v2 = _load(V2)
    assert {m.sha256 for m in v2.masters if m.role == "backup"} == BTL_BACKUP_SHAS
    # The retired master is neither primary nor backup: a primary swap back to
    # 3816ff5c would trip the class guard (test_source_dna_registry.py), and a
    # re-add as a backup would too.
    assert OLD not in {m.sha256 for m in v2.masters}


def test_no_registered_between_library_contains_the_retired_master():
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        if library.page_id != BTL_PAGE:
            continue
        assert OLD not in {
            master.sha256 for master in library.masters
        }, f"{library.library_id} still registers the retired master"


def test_btl_plans_its_cuts_from_the_new_master():
    _, v2 = _load(V2)
    recipe, _ = _recipe()
    recipe = replace(recipe, masters=v2.masters)
    served, history, cuts = set(), {}, []
    for run in range(20):
        for order, cut in enumerate(plan_source_cuts(recipe, 1, served, seed=f"btl-{run}", history=history)):
            served.add(cut.slot_id)
            history[cut.slot_id] = CutUse(float(run), order)
            cuts.append(cut)
    assert cuts
    from_new = [cut for cut in cuts if cut.master.sha256 == NEW]
    assert len(from_new) >= len(cuts) * 3 // 4, "the primary carries the page; backups are rare variety"
    assert all(cut.master.sha256 != OLD for cut in cuts)
