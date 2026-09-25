"""Self-healing sourced-video supply: every replenish run cuts NEW time frames.

Operator rule (2026-09-25): only the exact posted clip may never post again; a
different time frame of the same master (another start or another length) is
a new video. A long master therefore holds hundreds of cuts, and a replenish
run must never spend renders re-cutting a time frame this page already has.
"""

from dataclasses import replace

import routers.control_plane as cp
from services.control_plane_sources import (
    MASTER_WINDOWS_EXHAUSTED,
    plan_source_cuts,
    resolve_source_recipe,
    source_cut_durations,
    source_cut_is_planned,
)
from tests.test_control_plane_source_execution import (  # noqa: F401 (fixture)
    PAGE_ID,
    headers,
    job_body,
    lab,
    publication,
)

PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"


def _long_page_master_recipe(duration_ms=600_000, floor_ms=210_000):
    """A tender.acres-shaped page: one 600 s page master, 210 s source floor."""
    recipe = resolve_source_recipe(publication(
        cut_duration_ms=7_000, source_start_ms=floor_ms,
    ))
    assert recipe is not None
    original = recipe.masters[0]
    master = replace(
        original, duration_ms=duration_ms, source_offset_ms=0,
        provenance={**original.provenance, "authority": PAGE_MASTER_AUTHORITY},
    )
    return replace(recipe, masters=(master,)), master


def _overlaps(left, right):
    return (
        left.master.sha256 == right.master.sha256
        and left.start_ms < right.start_ms + right.duration_ms
        and right.start_ms < left.start_ms + left.duration_ms
    )


def _runs(recipe, count, quantity=10):
    served: set[str] = set()
    runs = []
    for run in range(count):
        cuts = plan_source_cuts(recipe, quantity, served, seed=f"run-{run}")
        runs.append(cuts)
        served |= {cut.slot_id for cut in cuts}
    return runs, served


def test_repeated_runs_on_one_master_cut_disjoint_new_windows():
    recipe, _ = _long_page_master_recipe()
    runs, served = _runs(recipe, 3)
    cuts = [cut for run in runs for cut in run]
    assert [len(run) for run in runs] == [10, 10, 10]
    assert len(served) == 30, "a time frame was planned twice"
    # 390 s of usable footage: the first thirty cuts are all fresh footage.
    for i, left in enumerate(cuts):
        for right in cuts[i + 1:]:
            assert not _overlaps(left, right), (left, right)


def test_lengths_vary_within_the_format_bounds():
    recipe, master = _long_page_master_recipe()
    runs, _ = _runs(recipe, 5)
    lengths = {cut.duration_ms for run in runs for cut in run}
    assert lengths <= set(source_cut_durations(recipe)) == {6_000, 7_000, 8_000, 9_000}
    assert len(lengths) >= 3, lengths
    # The executor's cutDurationMs contract bounds the source window to 5-9 s.
    assert all(5_000 <= length <= 9_000 for length in lengths)
    for run in runs:
        for cut in run:
            assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)


def test_the_page_floor_and_master_bounds_are_honored():
    recipe, master = _long_page_master_recipe()
    runs, _ = _runs(recipe, 20)
    for cut in (cut for run in runs for cut in run):
        assert cut.master.source_offset_ms + cut.start_ms >= 210_000
        assert cut.start_ms + cut.duration_ms <= master.duration_ms


def test_already_cut_windows_are_never_re_emitted():
    recipe, master = _long_page_master_recipe(duration_ms=240_000, floor_ms=200_000)
    sha = master.sha256
    # Time frames of earlier jobs: current ids and a legacy 9-second-grid id
    # (its length is recovered from the legacy rotation it was cut at).
    served = {f"{sha}:200000:7000", f"{sha}:205000:6000", f"{sha}:216000"}
    runs = []
    while batch := plan_source_cuts(recipe, 10, served, seed=f"drain-{len(runs)}"):
        runs.append(batch)
        emitted = {cut.slot_id for cut in batch}
        assert not emitted & served
        served |= emitted
    frames = [(int(s.split(":")[1]), int(s.split(":")[2])) for s in served if s.count(":") == 2]
    assert (200_000, 7_000) in frames and (205_000, 6_000) in frames
    assert not any(start == 216_000 and length == _legacy_length(recipe, master, 216_000)
                   for batch in runs for start, length in [(c.start_ms, c.duration_ms) for c in batch])


def _legacy_length(recipe, master, start_ms):
    from services.control_plane_sources import planned_source_cut_duration
    return planned_source_cut_duration(recipe, master, start_ms)


def test_a_seed_reproduces_its_plan_and_each_run_differs():
    recipe, _ = _long_page_master_recipe()
    first = plan_source_cuts(recipe, 10, set(), seed="a1b2c3d4e5f60718")
    again = plan_source_cuts(recipe, 10, set(), seed="a1b2c3d4e5f60718")
    other = plan_source_cuts(recipe, 10, set(), seed="0000000000000001")
    assert [cut.slot_id for cut in first] == [cut.slot_id for cut in again]
    assert {cut.slot_id for cut in first} != {cut.slot_id for cut in other}


def test_a_long_master_sustains_a_month_of_supply_at_full_run_size():
    # A page needs ~150 clips a month; the Worker asks for up to the Lab's
    # per-run ceiling (10). A 600 s master with a 210 s floor must keep
    # filling every run with unique time frames well past that.
    recipe, _ = _long_page_master_recipe()
    runs, served = _runs(recipe, 20)
    assert all(len(run) == 10 for run in runs)
    assert len(served) == 200


def test_job_records_its_seed_and_the_plan_is_reproducible(lab):
    client, _, _ = lab
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(4),
        headers=headers("time-frame-seed"),
    )
    assert created.status_code == 200, created.text
    job = cp._load_jobs()["jobs"][created.json()["jobId"]]
    recipe = cp._dossier_source_recipe(publication())
    replayed = plan_source_cuts(recipe, 4, set(), seed=job["cutPlanSeed"])
    assert [cut["slotId"] for cut in job["sourceCuts"]] == [cut.slot_id for cut in replayed]


def test_new_recipe_revision_does_not_recut_seven_of_ten_windows(lab):
    # Production shape: a page's dossier is republished (new recipeVersion,
    # same treatment) after a 7-clip replenish completed. The next 10-clip
    # run used to walk the same grid from the same first slot and re-cut
    # those 7 exact time frames: 7 renders whose bytes were already admitted
    # (Worker `duplicates: 7`) and only 3 new clips.
    client, _, _ = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(7),
        headers=headers("seven-of-ten-first"),
    )
    assert first.status_code == 200, first.text
    cp._update_job(first.json()["jobId"], status="completed")

    revision = publication(recipe_version="dossier-republished0000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=revision,
        headers=headers("seven-of-ten-register"),
    ).status_code == 200
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(10, revision),
        headers=headers("seven-of-ten-second"),
    )
    assert second.status_code == 200, second.text
    store = cp._load_jobs()["jobs"]
    before = {(cut["masterSha256"], cut["startMs"], cut["durationMs"])
              for cut in store[first.json()["jobId"]]["sourceCuts"]}
    after = [(cut["masterSha256"], cut["startMs"], cut["durationMs"])
             for cut in store[second.json()["jobId"]]["sourceCuts"]]
    assert len(after) == 10
    repeated = [frame for frame in after if frame in before]
    assert repeated == [], f"{len(repeated)} of 10 time frames re-cut"


def test_a_tiny_master_reports_master_windows_exhausted(lab, monkeypatch):
    client, _, _ = lab
    resolve = cp._dossier_source_recipe

    def tiny(payload):
        recipe = resolve(payload)
        # 6 s of footage holds exactly one 6-8 s time frame: 0-6 s.
        return replace(recipe, masters=tuple(
            replace(master, duration_ms=6_000) for master in recipe.masters
        ))

    monkeypatch.setattr(cp, "_dossier_source_recipe", tiny)
    only = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("tiny-master-only"),
    )
    assert only.status_code == 200, only.text
    cp._update_job(only.json()["jobId"], status="completed")
    capability = client.get(
        "/api/control-plane/v1/capabilities", headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert capability[0]["maxQuantity"] == 0
    exhausted = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("tiny-master-exhausted"),
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == MASTER_WINDOWS_EXHAUSTED == "master_windows_exhausted"


def test_a_24_hour_master_plans_and_answers_capacity_within_100_ms():
    # Review of #165: scoring every candidate cost ~0.7-12 s on a 24 h master
    # (the registry maximum) under the jobs lock, on create and on every
    # capability poll. A long master now samples fresh footage and stops at
    # the requested count.
    import time
    from services.control_plane_sources import source_window_exclusions

    recipe, master = _long_page_master_recipe(duration_ms=86_400_000, floor_ms=0)
    served: set[str] = set()
    run = 0
    while len(served) < 400:
        served |= {cut.slot_id for cut in plan_source_cuts(recipe, 10, served, seed=f"prior-{run}")}
        run += 1
    frames = sorted((int(s.split(":")[1]), int(s.split(":")[2])) for s in served)

    def timed(**kwargs):
        started = time.perf_counter()
        cuts = plan_source_cuts(recipe, 10, served, **kwargs)
        return cuts, time.perf_counter() - started

    created, create_seconds = timed(seed="job-seed")
    capacity, capacity_seconds = timed()
    assert create_seconds < 0.1, create_seconds
    assert capacity_seconds < 0.1, capacity_seconds
    assert len(created) == len(capacity) == 10
    for cut in created:
        assert cut.slot_id not in served
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)
        # Fresh footage: no overlap with any earlier cut, none within the plan.
        assert not any(
            start < cut.start_ms + cut.duration_ms and cut.start_ms < start + length
            for start, length in frames
        )
    assert not any(
        _overlaps(left, right)
        for i, left in enumerate(created) for right in created[i + 1:]
    )
    assert [c.slot_id for c in created] == [c.slot_id for c in timed(seed="job-seed")[0]]

    # Another page holding the whole source leaves nothing, also without a scan.
    everything = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 0, "endMs": 9_007_199_254_740_991,
    }])
    started = time.perf_counter()
    assert plan_source_cuts(recipe, 10, served, everything) == []
    assert time.perf_counter() - started < 0.1


def test_reserved_windows_are_skipped_on_long_and_short_masters_alike():
    from services.control_plane_sources import source_window_exclusions
    for duration_ms in (120_000, 7_200_000):
        recipe, master = _long_page_master_recipe(duration_ms=duration_ms, floor_ms=0)
        excluded = source_window_exclusions([{
            "sourceIdentity": master.provenance["sourceUrl"],
            "startMs": 10_000, "endMs": duration_ms - 30_000,
        }])
        cuts = []
        served: set[str] = set()
        while batch := plan_source_cuts(recipe, 10, served, excluded, seed=f"x{len(cuts)}"):
            cuts.extend(batch)
            served |= {cut.slot_id for cut in batch}
        assert cuts
        for cut in cuts:
            assert cut.start_ms + cut.duration_ms <= 10_000 or cut.start_ms >= duration_ms - 30_000
