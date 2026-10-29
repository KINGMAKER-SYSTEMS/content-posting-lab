"""between.the.lines66 moves to a new primary master in a NEW library id (10-29).

The old primary (3816ff5c, Drive IMG_4962 origin) is shared with other pages, so
BTL must stop cutting it. The Worker admits clips by sourceLibraryId only, so a
new primary under the SAME id would keep the old clips eligible: the new master
goes into pov-night-core-between-the-lines66-automation-v2, and BTL's Dossier is
re-published onto v2. v1 stays registered, unchanged, until that re-publish.
"""
import json
from dataclasses import replace
from pathlib import Path

from services.control_plane_sources import CutUse, plan_source_cuts
from services.source_dna_registry import parse_source_dna_manifest
from tests.test_source_recut_variety import _recipe
from tests.test_control_plane_source_execution import offline_shipstream_manifest  # noqa: F401 (fixture)

DIR = Path(__file__).resolve().parents[1] / "recipes/source-dna"
V1 = "pov-night-core-between-the-lines66-automation-v1"
V2 = "pov-night-core-between-the-lines66-automation-v2"
NEW = "76c34024438df3239bfdd286deb091280f0226ba63b22a0d09b93fdfebaac649"
OLD = "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"


def _load(library_id):
    raw = (DIR / f"{library_id}.json").read_bytes()
    return raw, parse_source_dna_manifest(raw, library_id)


def test_v2_has_the_new_primary_and_never_the_old_one():
    raw, v2 = _load(V2)
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert raw == canonical.encode()
    assert v2.page_id == "acct:between-the-lines66" and v2.format_slug == "pov-night-core"
    primaries = [m for m in v2.masters if m.role == "primary"]
    assert [m.sha256 for m in primaries] == [NEW]
    new = primaries[0]
    assert (new.bytes, new.duration_ms, new.source_offset_ms) == (180_564_887, 600_000, 600_000)
    assert new.storage_key == f"source-dna/pov-night-core/tt-warner-between-the-lines66/{NEW}.mp4"
    assert OLD not in {m.sha256 for m in v2.masters}


def test_v2_keeps_the_same_backups_and_v1_is_untouched():
    _, v1 = _load(V1)
    _, v2 = _load(V2)
    assert [m.sha256 for m in v1.masters if m.role == "primary"] == [OLD]
    assert ({m.sha256 for m in v1.masters if m.role == "backup"}
            == {m.sha256 for m in v2.masters if m.role == "backup"})
    assert v2.sha256 != v1.sha256, "a new id and a new version, so the Worker re-binds on re-publish"


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
