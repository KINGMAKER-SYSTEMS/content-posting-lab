"""Re-cut variety (operator rule 2026-09-30).

"When it recuts, it uses a different time cut than the previous one. It
recuts at a different section in a different time slice", lengths 5 to 9 s in
half-second steps, "just build it in there for everything": every re-cut of a
master lands on a different part of the footage at a different length than
the cuts just before it, and supply never stops when the windows run out.
"""

from dataclasses import replace

import pytest

import routers.control_plane as cp
from services import job_store_compaction as compaction
from services.control_plane_sources import (
    NO_CUT_TIME,
    SOURCE_MASTER_TOO_SHORT,
    SOURCE_WINDOWS_RESERVED_ELSEWHERE,
    CutUse,
    explain_empty_source_plan,
    plan_source_cuts,
    resolve_source_recipe,
    source_cut_durations,
    source_cut_is_planned,
    source_window_exclusions,
)
from tests.test_control_plane_source_execution import (  # noqa: F401 (fixture)
    offline_shipstream_manifest,
    publication,
)

PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"


def _recipe(duration_ms=600_000, floor_ms=0, cut_duration_ms=7_000, clip_speed=None):
    kwargs = {"cut_duration_ms": cut_duration_ms, "source_start_ms": floor_ms}
    if clip_speed is not None:
        kwargs["clip_speed"] = clip_speed
    recipe = resolve_source_recipe(publication(**kwargs))
    assert recipe is not None
    original = recipe.masters[0]
    master = replace(
        original, duration_ms=duration_ms, source_offset_ms=0,
        provenance={**original.provenance, "authority": PAGE_MASTER_AUTHORITY},
    )
    return replace(recipe, masters=(master,)), master


def _cut_one_at_a_time(recipe, count, page_id="acct:page"):
    """Simulate `count` single-clip jobs, each recorded with a later time."""
    served: set[str] = set()
    history: dict[str, CutUse] = {}
    cuts = []
    for run in range(count):
        (cut,) = plan_source_cuts(
            recipe, 1, served, seed=f"job-{run}", history=history, page_id=page_id,
        )
        cuts.append(cut)
        served.add(cut.slot_id)
        history[cut.slot_id] = CutUse(float(run), 0, page_id)
    return cuts, served, history


def test_consecutive_recuts_change_both_section_and_length():
    recipe, master = _recipe()
    cuts, _, _ = _cut_one_at_a_time(recipe, 60)
    assert cuts[0].duration_ms == 7_000, "the first cut uses the page's Cut length"
    for previous, current in zip(cuts, cuts[1:]):
        assert current.duration_ms != previous.duration_ms, (previous, current)
        # A different section: never within 30 s of the cut just before.
        assert abs(current.start_ms - previous.start_ms) >= 30_000, (previous, current)
    lengths = [cut.duration_ms for cut in cuts]
    assert not any(a == b == c for a, b, c in zip(lengths, lengths[1:], lengths[2:]))
    assert len(set(lengths)) == len(source_cut_durations(recipe)), "every length gets used"
    # Spread over the whole timeline, not one end of it.
    quartiles = {cut.start_ms * 4 // master.duration_ms for cut in cuts[:8]}
    assert quartiles == {0, 1, 2, 3}, [cut.start_ms for cut in cuts[:8]]


def test_one_multi_clip_plan_is_itself_spread_out():
    recipe, master = _recipe()
    plan = plan_source_cuts(recipe, 10, set(), seed="spread")
    assert len(plan) == 10
    lengths = [cut.duration_ms for cut in plan]
    assert all(a != b for a, b in zip(lengths, lengths[1:])), lengths
    assert len({cut.start_ms * 4 // master.duration_ms for cut in plan}) == 4
    for i, left in enumerate(plan):
        for right in plan[i + 1:]:
            assert not (left.start_ms < right.start_ms + right.duration_ms
                        and right.start_ms < left.start_ms + left.duration_ms)


def test_the_last_cut_is_read_from_its_timestamp_not_from_set_order():
    recipe, master = _recipe()
    sha = master.sha256
    served = {f"{sha}:300000:7000", f"{sha}:100000:8000"}
    # Newest cut: 7 s at 300 s. The next cut must move away from it and use
    # another length.
    newest_seven = {
        f"{sha}:300000:7000": CutUse(200.0, 0),
        f"{sha}:100000:8000": CutUse(100.0, 0),
    }
    (cut,) = plan_source_cuts(recipe, 1, served, seed="t", history=newest_seven)
    assert cut.duration_ms not in (7_000, 8_000)
    assert abs(cut.start_ms - 300_000) >= 120_000
    # The same windows with the 8 s cut newest steer away from 8 s at 100 s.
    newest_eight = {
        f"{sha}:300000:7000": CutUse(100.0, 0),
        f"{sha}:100000:8000": CutUse(200.0, 0),
    }
    (cut,) = plan_source_cuts(recipe, 1, served, seed="t", history=newest_eight)
    assert cut.duration_ms != 8_000
    assert abs(cut.start_ms - 100_000) >= 120_000


def test_a_used_window_is_never_reused_while_an_unused_one_exists():
    recipe, master = _recipe(duration_ms=20_000)
    durations = source_cut_durations(recipe)
    every = {
        (start, length)
        for length in durations
        for start in [*range(0, 20_000 - length + 1, 1_000), 20_000 - length]
    }
    cuts, _, _ = _cut_one_at_a_time(recipe, len(every))
    assert {(cut.start_ms, cut.duration_ms) for cut in cuts} == every
    for cut in cuts:
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)


def test_saturation_reuses_the_least_recently_cut_window_and_never_stops():
    recipe, master = _recipe(duration_ms=6_000)
    cuts, served, history = _cut_one_at_a_time(recipe, 5)
    assert len(set(cut.slot_id for cut in cuts)) == 5, "five distinct windows first"
    # Every window is cut. The next cuts come back oldest first.
    reused = []
    for run in range(5, 9):
        (cut,) = plan_source_cuts(
            recipe, 1, served, seed=f"job-{run}", history=history, page_id="acct:page",
        )
        reused.append(cut.slot_id)
        history[cut.slot_id] = CutUse(float(run), 0, "acct:page")
    assert reused == [cut.slot_id for cut in cuts[:4]]
    assert len(plan_source_cuts(recipe, 10, served, history=history)) == 1, (
        "capacity stays 1, not 0: one plan never holds overlapping cuts"
    )


def test_old_cuts_without_a_time_are_reused_first():
    recipe, master = _recipe(duration_ms=6_000)
    sha = master.sha256
    windows = [f"{sha}:0:5000", f"{sha}:1000:5000", f"{sha}:0:5500", f"{sha}:500:5500", f"{sha}:0:6000"]
    history = {slot: CutUse(float(index + 1), 0) for index, slot in enumerate(windows)}
    del history[f"{sha}:0:5500"]  # archived before timestamps existed
    (cut,) = plan_source_cuts(recipe, 1, set(windows), history=history)
    assert cut.slot_id == f"{sha}:0:5500"


def test_capacity_stays_above_zero_after_every_window_is_cut():
    recipe, master = _recipe(duration_ms=6_000)
    _, served, history = _cut_one_at_a_time(recipe, 12)
    assert len(plan_source_cuts(recipe, recipe.max_quantity, served, history=history)) == 1
    assert len(plan_source_cuts(recipe, recipe.max_quantity, served)) == 1


def test_only_genuinely_impossible_plans_are_empty_and_they_are_named():
    recipe, master = _recipe(duration_ms=4_999)
    assert plan_source_cuts(recipe, 1, set()) == []
    assert explain_empty_source_plan(recipe) == SOURCE_MASTER_TOO_SHORT

    recipe, master = _recipe(duration_ms=60_000)
    everything = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 0, "endMs": 9_007_199_254_740_991,
    }])
    assert plan_source_cuts(recipe, 1, set(), everything) == []
    assert explain_empty_source_plan(recipe, everything) == SOURCE_WINDOWS_RESERVED_ELSEWHERE


def test_other_pages_reservations_fall_back_to_this_pages_own_oldest_window():
    recipe, master = _recipe(duration_ms=60_000)
    sha = master.sha256
    served = {f"{sha}:10000:7000", f"{sha}:30000:6000", f"{sha}:45000:5000"}
    history = {
        f"{sha}:10000:7000": CutUse(30.0, 0, "acct:page"),
        f"{sha}:30000:6000": CutUse(10.0, 0, "acct:page"),
        f"{sha}:45000:5000": CutUse(5.0, 0, "acct:other"),
    }
    everything = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 0, "endMs": 9_007_199_254_740_991,
    }])
    (cut,) = plan_source_cuts(recipe, 1, served, everything, history=history, page_id="acct:page")
    assert cut.slot_id == f"{sha}:30000:6000", "this page's oldest, never another page's"
    # Without knowing the page, nothing is safe to reuse.
    assert plan_source_cuts(recipe, 1, served, everything, history=history) == []
    # A partial reservation is still respected while fresh footage remains.
    partial = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"], "startMs": 0, "endMs": 40_000,
    }])
    for cut in plan_source_cuts(recipe, 3, served, partial, history=history, page_id="acct:page"):
        assert cut.start_ms >= 40_000


def test_lengths_are_half_seconds_from_five_to_nine():
    recipe, master = _recipe()
    assert source_cut_durations(recipe) == tuple(range(5_000, 9_001, 500))
    cuts, _, _ = _cut_one_at_a_time(recipe, 40)
    assert max(cut.duration_ms for cut in cuts) <= 9_000
    assert {cut.duration_ms for cut in cuts} & {5_500, 6_500, 7_500, 8_500}, "half seconds are used"


@pytest.mark.parametrize("speed", [0.6, 0.75, 1.0, 1.25, 1.75])
def test_delivered_clip_stays_inside_the_worker_bounds_at_the_saved_speed(speed):
    recipe, _ = _recipe(clip_speed=speed)
    durations = source_cut_durations(recipe)
    assert durations
    for length in durations:
        assert 5_000 <= length <= 9_000
        assert 5_000 - 1 <= length / speed <= 9_000 + 1, (speed, length)


def test_jobs_queued_under_the_old_lengths_still_verify():
    # At 0.8x an 8 s window delivers 10 s, past the Worker's 9 s, so new plans
    # stop at 7 s. A job queued earlier with an 8 s window still renders.
    recipe, master = _recipe(clip_speed=0.8, cut_duration_ms=8_000)
    sha = master.sha256
    assert max(source_cut_durations(recipe)) == 7_000
    assert source_cut_is_planned(recipe, master, 0, 8_000, f"{sha}:0:8000")
    assert not source_cut_is_planned(recipe, master, 0, 8_500, f"{sha}:0:8500")


def test_archive_round_trip_keeps_when_and_by_whom_each_window_was_cut():
    recipe, master = _recipe()
    sha = master.sha256
    job = {
        "jobId": "j1", "pageId": "acct:page", "sourceKind": "dossier_source_dna",
        "status": "completed", "createdAt": "2026-09-30T10:00:00+00:00",
        "completedAt": "2026-09-30T10:05:00+00:00",
        "sourceLibraryId": recipe.source_library_id,
        "sourceLibraryHash": recipe.source_library_hash, "recipeVersion": "rv1",
        "sourceCuts": [
            {"slotId": f"{sha}:1000:7000", "masterSha256": sha, "startMs": 1_000, "durationMs": 7_000},
            {"slotId": f"{sha}:90000:8000", "masterSha256": sha, "startMs": 90_000, "durationMs": 8_000},
        ],
    }
    live_slots, live_history = cp._source_dna_cut_ledger({"jobs": {"j1": job}}, recipe, "rv1")
    index = compaction.empty_index()
    compaction.fold_into_index(index, job)
    archived = {"jobs": {}, compaction.ARCHIVE_INDEX_KEY: index}
    slots, history = cp._source_dna_cut_ledger(archived, recipe, "rv1")
    assert slots == live_slots
    assert history == live_history
    assert history[f"{sha}:90000:8000"] == CutUse(
        cp.cut_use_time("2026-09-30T10:05:00+00:00"), 1, "acct:page",
    )
    # An entry archived before these fields existed still reserves its
    # window and counts as the oldest cut.
    legacy = {key: value for key, value in index["sourceDnaCuts"][0].items()
              if key not in {"usedAt", "cutIndex", "pageId"}}
    old_store = {"jobs": {}, compaction.ARCHIVE_INDEX_KEY: {"sourceDnaCuts": [legacy]}}
    slots, history = cp._source_dna_cut_ledger(old_store, recipe, "rv1")
    assert f"{sha}:1000:7000" in slots
    assert history[f"{sha}:1000:7000"] == CutUse(NO_CUT_TIME, 0, None)
    (cut,) = plan_source_cuts(recipe, 1, slots, history=history)
    assert cut.slot_id not in slots


def test_running_jobs_use_their_creation_time():
    recipe, master = _recipe()
    sha = master.sha256
    store = {"jobs": {
        "done": {"pageId": "acct:page", "sourceKind": "dossier_source_dna", "status": "completed",
                 "createdAt": "2026-09-30T09:00:00+00:00", "completedAt": "2026-09-30T09:10:00+00:00",
                 "recipeVersion": "rv1",
                 "sourceCuts": [{"masterSha256": sha, "startMs": 1_000, "durationMs": 7_000}]},
        "queued": {"pageId": "acct:page", "sourceKind": "dossier_source_dna", "status": "queued",
                   "createdAt": "2026-09-30T09:20:00+00:00",
                   "sourceCuts": [{"masterSha256": sha, "startMs": 50_000, "durationMs": 6_000}]},
    }}
    _, history = cp._source_dna_cut_ledger(store, recipe, "rv1")
    assert history[f"{sha}:50000:6000"].at > history[f"{sha}:1000:7000"].at
