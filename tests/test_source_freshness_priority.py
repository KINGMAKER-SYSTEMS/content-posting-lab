"""Fresh-footage-first (2026-10-21): genuinely unused footage beats length variety.

The planner must prefer a window that overlaps no already-cut footage over a
re-cut of used footage, even when the fresh window repeats the master's last
length. Length variety is a secondary concern: it orders two *fresh* windows,
not a fresh window against a re-cut.
"""

from dataclasses import replace

from services.control_plane_sources import (
    CutUse,
    plan_source_cuts,
    resolve_source_recipe,
    source_cut_durations,
)
from tests.test_control_plane_source_execution import (  # noqa: F401 (fixture)
    offline_shipstream_manifest,
    publication,
)

PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"


def _recipe(duration_ms=600_000, floor_ms=0, cut_duration_ms=7_000):
    recipe = resolve_source_recipe(
        publication(cut_duration_ms=cut_duration_ms, source_start_ms=floor_ms)
    )
    assert recipe is not None
    original = recipe.masters[0]
    master = replace(
        original, duration_ms=duration_ms, source_offset_ms=0,
        provenance={**original.provenance, "authority": PAGE_MASTER_AUTHORITY},
    )
    return replace(recipe, masters=(master,)), master


def test_fresh_last_length_beats_overlapping_different_length():
    # A 10 s master with one 5 s cut at 0..5 s. The only genuinely unused
    # footage is 5..10 s, which fits exactly one 5 s window -- the same length
    # as the last cut. An overlapping 6 s window at 4 s (4..10 s, re-cutting
    # 1 s of used footage) also fits. The fresh 5 s window must win even though
    # it repeats the master's last length.
    recipe, master = _recipe(duration_ms=10_000)
    assert source_cut_durations(recipe)[0] == 5_000
    sha = master.sha256
    served = {f"{sha}:0:5000"}
    history = {f"{sha}:0:5000": CutUse(200.0, 0)}

    plan = plan_source_cuts(recipe, 1, served, seed="fresh-first", history=history)

    assert len(plan) == 1
    (cut,) = plan
    assert (cut.start_ms, cut.duration_ms) == (5_000, 5_000), (
        f"expected the genuinely fresh 5 s window at 5 s, got "
        f"{cut.start_ms} ms at {cut.duration_ms} ms"
    )


def test_freshness_never_loses_to_a_different_length_recut():
    # Same shape, but ask for a multi-clip plan: the fresh window must be the
    # first pick, before any re-cut, even though it repeats the last length.
    recipe, master = _recipe(duration_ms=20_000)
    sha = master.sha256
    served = {
        f"{sha}:0:5000", f"{sha}:1000:5000", f"{sha}:2000:5000",
        f"{sha}:3000:5000", f"{sha}:4000:5000",
    }
    history = {
        slot: CutUse(float(i), 0) for i, slot in enumerate(sorted(served))
    }
    plan = plan_source_cuts(recipe, 2, served, seed="fresh-first", history=history)
    assert plan, "a fresh window must still be found"
    first = plan[0]
    # The five 5 s cuts span 0..9 s; the first pick must be disjoint from it.
    assert first.start_ms >= 9_000, first
