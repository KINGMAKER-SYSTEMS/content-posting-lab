"""Unused-moment selection and variety within the same masters.

Owner 2026-11-12: duration, offset and recipe changes never unlock used moments.
When no disjoint interval fits, report exhaustion so unused same-source coverage
can be replenished; do not invent extra footage runway by overlapping cuts.
"""

from dataclasses import replace
import math

import pytest

import routers.control_plane as cp
from services import job_store_compaction as compaction
from services.control_plane_sources import (
    NO_CUT_TIME,
    SOURCE_MASTER_TOO_SHORT,
    SOURCE_WINDOWS_EXHAUSTED,
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
from tests.source_dna_fixture import isolate_source_dna_registry

PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    isolate_source_dna_registry(monkeypatch, tmp_path)


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


def _cut_one_at_a_time(recipe, count):
    """Simulate up to `count` single-clip jobs, each recorded with a later
    time; stops early when the planner returns nothing."""
    served: set[str] = set()
    history: dict[str, CutUse] = {}
    cuts = []
    for run in range(count):
        plan = plan_source_cuts(recipe, 1, served, seed=f"job-{run}", history=history)
        if not plan:
            break
        (cut,) = plan
        cuts.append(cut)
        served.add(cut.slot_id)
        history[cut.slot_id] = CutUse(float(run), 0)
    return cuts, served, history


def _assert_no_window_twice(cuts):
    """Every two cuts from the same master use disjoint half-open intervals."""
    for index, cut in enumerate(cuts):
        for other in cuts[index + 1:]:
            if cut.master.sha256 == other.master.sha256:
                assert cut.start_ms + cut.duration_ms <= other.start_ms or other.start_ms + other.duration_ms <= cut.start_ms


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


def test_new_offsets_are_not_used_to_reenter_previously_cut_seconds():
    recipe, master = _recipe(duration_ms=20_000)
    cuts, served, history = _cut_one_at_a_time(recipe, 100)
    assert 1 <= len(cuts) <= 4
    assert sum(cut.duration_ms for cut in cuts) <= master.duration_ms
    _assert_no_window_twice(cuts)
    assert plan_source_cuts(recipe, 1, served, history=history) == []
    for cut in cuts:
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)


def test_true_saturation_stops_with_a_named_reason_without_overlap_reuse():
    recipe, master = _recipe(duration_ms=6_000)
    cuts, served, history = _cut_one_at_a_time(recipe, 100)
    assert len(cuts) == 1
    _assert_no_window_twice(cuts)
    assert plan_source_cuts(recipe, 10, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED


def test_a_five_second_master_stops_on_the_second_job():
    # The shortest master: one 5 s window. A second job gets the named stop,
    # never the same window again.
    recipe, master = _recipe(duration_ms=5_000)
    cuts, served, history = _cut_one_at_a_time(recipe, 5)
    assert [(cut.start_ms, cut.duration_ms) for cut in cuts] == [(0, 5_000)]
    assert plan_source_cuts(recipe, 1, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED


def test_single_length_vocabulary_stops_when_only_overlapping_starts_remain():
    recipe, master = _recipe(duration_ms=8_000, clip_speed=0.56)
    assert source_cut_durations(recipe) == (5_000,)
    cuts, served, history = _cut_one_at_a_time(recipe, 100)
    assert len(cuts) == 1 and cuts[0].duration_ms == 5_000
    assert plan_source_cuts(recipe, 1, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED


def test_capacity_becomes_zero_when_no_disjoint_unused_window_fits():
    recipe, master = _recipe(duration_ms=6_000)
    assert len(plan_source_cuts(recipe, recipe.max_quantity, set())) == 1
    _, served, history = _cut_one_at_a_time(recipe, 1)
    assert plan_source_cuts(recipe, recipe.max_quantity, served, history=history) == []


def test_only_impossible_or_used_up_plans_are_empty_and_they_are_named():
    recipe, master = _recipe(duration_ms=4_999)
    assert plan_source_cuts(recipe, 1, set()) == []
    assert explain_empty_source_plan(recipe, set()) == SOURCE_MASTER_TOO_SHORT

    recipe, master = _recipe(duration_ms=60_000)
    everything = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 0, "endMs": 9_007_199_254_740_991,
    }])
    assert plan_source_cuts(recipe, 1, set(), everything) == []
    assert explain_empty_source_plan(recipe, set(), everything) == SOURCE_WINDOWS_RESERVED_ELSEWHERE


def test_other_pages_reservations_are_respected_and_never_bypassed():
    recipe, master = _recipe(duration_ms=60_000)
    sha = master.sha256
    served = {f"{sha}:10000:7000", f"{sha}:30000:6000"}
    history = {f"{sha}:10000:7000": CutUse(30.0, 0), f"{sha}:30000:6000": CutUse(10.0, 0)}
    partial = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"], "startMs": 0, "endMs": 40_000,
    }])
    for cut in plan_source_cuts(recipe, 3, served, partial, history=history):
        assert cut.start_ms >= 40_000


def test_an_end_on_last_frame_start_next_to_a_grid_start_counts_as_the_same_window():
    # 80.01 s master: the 5 s window ending on the last frame starts at
    # 75.01 s, 10 ms from the 75 s grid start; they decode the same frames.
    recipe, master = _recipe(duration_ms=80_010)
    sha = master.sha256
    served = {f"{sha}:75000:5000"}
    cuts, _, _ = _cut_one_at_a_time(recipe, 400)
    starts_5s = sorted(cut.start_ms for cut in cuts if cut.duration_ms == 5_000)
    assert len({75_000, 75_010} & set(starts_5s)) == 1, "only one of the two is ever cut"
    plan = plan_source_cuts(recipe, 400, served)
    assert not any(cut.duration_ms == 5_000 and abs(cut.start_ms - 75_000) < 250 for cut in plan)


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


def test_archive_round_trip_keeps_when_each_window_was_cut():
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
        cp.cut_use_time("2026-09-30T10:05:00+00:00"), 1,
    )
    # An entry archived before these fields existed still reserves its
    # window and counts as the oldest cut.
    legacy = {key: value for key, value in index["sourceDnaCuts"][0].items()
              if key not in {"usedAt", "cutIndex"}}
    old_store = {"jobs": {}, compaction.ARCHIVE_INDEX_KEY: {"sourceDnaCuts": [legacy]}}
    slots, history = cp._source_dna_cut_ledger(old_store, recipe, "rv1")
    assert f"{sha}:1000:7000" in slots
    assert history[f"{sha}:1000:7000"] == CutUse(NO_CUT_TIME, 0)
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


def _cut_in_jobs(recipe, jobs, quantity=10):
    """Simulate up to ``jobs`` replenish runs of ``quantity`` clips, in time
    order, until a run comes back empty."""
    served: set[str] = set()
    history: dict[str, CutUse] = {}
    cuts = []
    for run in range(jobs):
        batch = plan_source_cuts(recipe, quantity, served, seed=f"job-{run}", history=history)
        if not batch:
            break
        for order, cut in enumerate(batch):
            cuts.append(cut)
            served.add(cut.slot_id)
            history[cut.slot_id] = CutUse(float(run), order)
    return cuts, served, history


def test_eighty_second_master_spreads_disjoint_cuts_then_names_exhaustion():
    recipe, master = _recipe(duration_ms=80_000)
    cuts, served, history = _cut_in_jobs(recipe, 1_000)
    assert 1 < len(cuts) <= 80_000 // min(source_cut_durations(recipe))
    assert sum(cut.duration_ms for cut in cuts) <= master.duration_ms
    _assert_no_window_twice(cuts)
    for cut in cuts:
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)
    assert plan_source_cuts(recipe, 10, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED


def test_sub_second_starts_are_far_enough_apart_for_any_frame_rate_above_4_fps():
    from services.control_plane_sources import MIN_START_GAP_MS, SUB_SECOND_START_OFFSETS_MS
    offsets = sorted(SUB_SECOND_START_OFFSETS_MS)
    assert offsets == [0, 250, 500, 750] and MIN_START_GAP_MS == 250
    for fps in (5, 23.976, 24, 25, 29.97, 30, 50, 60):
        # An exact seek begins on the first frame at or after the start.
        frames = [math.ceil(offset * fps / 1_000 - 1e-9) for offset in offsets]
        assert len(set(frames)) == len(frames), (fps, frames)


def test_sampled_master_exact_fallback_finds_last_disjoint_unused_gap(monkeypatch):
    import services.control_plane_sources as sources
    monkeypatch.setattr(sources, "SAMPLED_CANDIDATES_PER_CUT", 1)
    recipe, master = _recipe(duration_ms=700_000)
    # All but the final eight seconds are used. Candidate enumeration still
    # exceeds the sampled threshold, but the untouched tail must be found.
    served = {f"{master.sha256}:0:692000"}
    (cut,) = plan_source_cuts(recipe, 1, served, seed="miss")
    assert cut.start_ms >= 692_000
    assert cut.start_ms + cut.duration_ms <= master.duration_ms
