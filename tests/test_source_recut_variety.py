"""Re-cut variety (operator rule 2026-09-30).

"When it recuts, it uses a different time cut than the previous one. It
recuts at a different section in a different time slice", lengths 5 to 9 s in
half-second steps, "just build it in there for everything": every re-cut of a
master lands on a different part of the footage at a different length than
the cuts just before it, and supply never stops when the windows run out.
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
    """No two cuts of one master and length start under 250 ms apart."""
    by_length = {}
    for cut in cuts:
        by_length.setdefault((cut.master.sha256, cut.duration_ms), []).append(cut.start_ms)
    for starts in by_length.values():
        starts.sort()
        assert all(b - a >= 250 for a, b in zip(starts, starts[1:])), starts


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


def test_every_whole_second_window_is_cut_before_any_sub_second_one():
    recipe, master = _recipe(duration_ms=20_000)
    durations = source_cut_durations(recipe)
    every = {
        (start, length)
        for length in durations
        for start in [*range(0, 20_000 - length + 1, 1_000), 20_000 - length]
    }
    cuts, _, _ = _cut_one_at_a_time(recipe, len(every) + 20)
    windows = [(cut.start_ms, cut.duration_ms) for cut in cuts]
    last_whole = max(i for i, window in enumerate(windows) if window in every)
    assert every <= set(windows)
    # A sub-second start comes early only to avoid repeating the last length.
    assert sum(window not in every for window in windows[:last_whole]) <= 3
    _assert_no_window_twice(cuts)
    for cut in cuts:
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)


def test_true_saturation_stops_with_a_named_reason_and_never_repeats_a_window():
    # A 6 s master holds 9 windows: 5 s at 0/0.25/0.5/0.75/1 s, 5.5 s at
    # 0/0.25/0.5 s and 6 s at 0. Once each is cut, nothing more is planned (a
    # repeat would render the same bytes, which the Worker refuses) and the
    # reason is named so the Worker can fetch new footage.
    recipe, master = _recipe(duration_ms=6_000)
    cuts, served, history = _cut_one_at_a_time(recipe, 100)
    assert len(cuts) == 9
    _assert_no_window_twice(cuts)
    assert {cut.start_ms % 1_000 for cut in cuts} == {0, 250, 500, 750}
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


def test_a_single_length_vocabulary_moves_the_start_and_then_stops():
    # At 0.56x only 5 s satisfies both the source and delivered 5-9 s bound,
    # so the length cannot change: every cut is still a new start, and the
    # master stops with the named reason instead of repeating one.
    recipe, master = _recipe(duration_ms=8_000, clip_speed=0.56)
    assert source_cut_durations(recipe) == (5_000,)
    cuts, served, history = _cut_one_at_a_time(recipe, 100)
    assert [cut.start_ms for cut in cuts[:4]] and len(cuts) == len({cut.start_ms for cut in cuts})
    _assert_no_window_twice(cuts)
    assert len(cuts) == 13  # starts 0..3 s at 0, 0.25, 0.5, 0.75 s (3.25..3.75 do not fit)
    assert plan_source_cuts(recipe, 1, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED


def test_capacity_is_zero_only_when_every_window_is_cut():
    recipe, master = _recipe(duration_ms=6_000)
    _, served, history = _cut_one_at_a_time(recipe, 8)
    assert len(plan_source_cuts(recipe, recipe.max_quantity, served, history=history)) == 1
    _, served, history = _cut_one_at_a_time(recipe, 9)
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


def test_an_80_second_master_cuts_every_start_once_then_stops_by_name():
    # rumi58651-shaped page: one 80 s master cut in runs of 10. Every whole
    # second x length window first, then half-second starts, then quarter and
    # three-quarter starts; each window exactly once, the length changing
    # every cut; then a named stop, never a repeat.
    recipe, master = _recipe(duration_ms=80_000)
    durations = source_cut_durations(recipe)

    def fits(offset, length):
        return (80_000 - length - offset) // 1_000 + 1

    whole = sum(fits(0, length) + (length % 1_000 != 0) for length in durations)
    half = sum(fits(500, length) - (length % 1_000 != 0) for length in durations)
    quarter = sum(fits(250, length) + fits(750, length) for length in durations)
    cuts, served, history = _cut_in_jobs(recipe, 1_000)
    assert len(cuts) == whole + half + quarter
    _assert_no_window_twice(cuts)

    def level(cut):
        if cut.start_ms % 1_000 == 0 or cut.start_ms == 80_000 - cut.duration_ms:
            return 0
        return {500: 1}.get(cut.start_ms % 1_000, 2)

    levels = [level(cut) for cut in cuts]
    # Coarse to fine: a finer start is used early only to change the length.
    last = {level: len(levels) - 1 - levels[::-1].index(level) for level in (0, 1)}
    assert sum(level > 0 for level in levels[:last[0]]) <= 20
    assert sum(level > 1 for level in levels[:last[1]]) <= 20
    lengths = [cut.duration_ms for cut in cuts]
    # The length repeats only near the very end, when one length is all that
    # is left uncut.
    repeats = [i for i, (a, b) in enumerate(zip(lengths, lengths[1:])) if a == b]
    assert len(repeats) <= 10 and all(i > len(lengths) - 40 for i in repeats), repeats
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


def test_a_sampled_master_finds_its_last_uncut_window_exactly(monkeypatch):
    # Above FULL_SCAN_CANDIDATES a level is sampled. When the sample misses
    # the few windows left, an exact pass finds them: the planner never
    # reports a master used up while a never-cut window remains.
    import services.control_plane_sources as sources
    monkeypatch.setattr(sources, "SAMPLED_CANDIDATES_PER_CUT", 1)
    recipe, master = _recipe(duration_ms=700_000)
    sha = master.sha256
    durations = source_cut_durations(recipe)
    every = [
        (start, length)
        for length in durations
        for start in [*range(0, 700_000 - length + 1, 1_000), 700_000 - length]
    ]
    assert len(every) > sources.FULL_SCAN_CANDIDATES
    left = (412_000, 7_500)
    served = {f"{sha}:{start}:{length}" for start, length in every if (start, length) != left}
    (cut,) = plan_source_cuts(recipe, 1, served, seed="miss")
    assert (cut.start_ms, cut.duration_ms) == left
