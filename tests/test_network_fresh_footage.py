"""Owner 2026-11-12: same masters/aesthetic, unused footage moments only.

These staged regression tests cover local cut history and canonical reader
eligibility. They do not certify a complete production history feed or legacy
canonical execution enforcement; those remain explicit release dependencies.
"""
from copy import deepcopy

import pytest
import routers.control_plane as cp
from services.control_plane_sources import _Lane, plan_source_cuts, source_cut_durations
from tests.test_source_time_frames import (
    _long_page_master_recipe, _isolated_source_dna, offline_shipstream_manifest,  # noqa: F401
)
from tests.test_scrambler_originality_contract_independent import (
    feed, show, allowed, SHA_A, SHA_B,
)


@pytest.mark.parametrize("prior_duration", [5_000, 7_000, 9_000])
@pytest.mark.parametrize("candidate_duration", [5_000, 6_500, 9_000])
def test_another_duration_does_not_unlock_used_master_moment(prior_duration, candidate_duration):
    recipe, master = _long_page_master_recipe()
    lane = _Lane(recipe, master, [(210_000, prior_duration)], {}, None,
                 source_cut_durations(recipe))
    assert lane.is_taken(210_000, candidate_duration)
    assert lane.is_taken(210_000 + prior_duration - 1, candidate_duration)
    assert not lane.is_taken(210_000 + prior_duration, candidate_duration)


@pytest.mark.parametrize("status", ["queued", "running", "completed"])
def test_real_job_ledger_excludes_entire_used_interval_and_keeps_master(status):
    recipe, master = _long_page_master_recipe()
    store = {"jobs": {"older": {
        "sourceKind": "dossier_source_dna", "status": status,
        "pageId": "some-other-page", "recipeVersion": "old-recipe",
        "sourceCuts": [{"masterSha256": master.sha256,
                        "startMs": 210_000, "durationMs": 10_000}],
    }}}
    slots, history = cp._source_dna_cut_ledger(store, recipe, "current-recipe")
    before = deepcopy(recipe)
    cuts = plan_source_cuts(recipe, 3, slots, history=history, seed="same-master-fresh")
    assert len(cuts) == 3
    assert all(cut.master == master for cut in cuts)
    assert all(cut.start_ms >= 220_000 for cut in cuts)
    assert recipe == before


def test_exhausted_moments_do_not_fall_back_to_recut_or_finer_offset():
    recipe, master = _long_page_master_recipe(duration_ms=220_000)
    slots = {f"{master.sha256}:210000:10000"}
    assert plan_source_cuts(recipe, 1, slots, seed="exhausted") == []


def test_long_master_continues_with_disjoint_unused_moments():
    recipe, master = _long_page_master_recipe()
    before = deepcopy(recipe)
    slots = set()
    all_cuts = []
    for run in range(3):
        cuts = plan_source_cuts(recipe, 5, slots, seed=f"fresh-run-{run}")
        assert len(cuts) == 5
        for cut in cuts:
            assert cut.master == master
            assert all(cut.start_ms + cut.duration_ms <= old.start_ms
                       or old.start_ms + old.duration_ms <= cut.start_ms
                       for old in all_cuts)
            all_cuts.append(cut)
        slots.update(cut.slot_id for cut in cuts)
    assert recipe == before


@pytest.mark.parametrize("page", ["@pilot", "@other"])
@pytest.mark.parametrize("days", [0, 8, 30, 90, 3650])
def test_any_prior_canonical_show_stays_used_without_age_or_page_reset(tmp_path, page, days):
    index = feed(tmp_path, [show(days=days, page=page, sha=SHA_A)])
    assert not allowed(index, sha=SHA_B)
    # Same original master remains usable at a genuinely different moment.
    assert allowed(index, start=620_000, end=623_000, sha=SHA_B)


@pytest.mark.parametrize("page", ["@pilot", "@other"])
def test_lost_prior_window_never_ages_into_freshness(tmp_path, page):
    index = feed(tmp_path, [show(days=3650, page=page, start=None, end=None)])
    assert not allowed(index)
    assert not allowed(index, start=620_000, end=623_000)


from tests.test_control_plane_source_execution import lab, headers, job_body  # noqa: F401,E402
from tests.test_scrambler_executor_behavior import (  # noqa: E402,F401
    _wire_executor, isolated_scrambler_state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_state", ["queued", "running", "completed", "archived"])
async def test_old_queued_legacy_overlap_is_skipped_before_media_and_safe_sibling_delivers(
        lab, monkeypatch, tmp_path, prior_state):
    client, _, _ = lab
    first = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                        headers=headers("legacy-prior-history")).json()["jobId"]
    second = client.post("/api/control-plane/v1/jobs", json=job_body(2),
                         headers=headers("legacy-old-queued")).json()["jobId"]
    store = cp._load_jobs()
    old_cut = deepcopy(store["jobs"][first]["sourceCuts"][0])
    safe_cut = deepcopy(store["jobs"][second]["sourceCuts"][1])
    if prior_state == "archived":
        from services import job_store_compaction as compaction
        prior = store["jobs"].pop(first)
        prior["status"] = "completed"
        index = compaction.empty_index()
        compaction.fold_into_index(index, prior)
        store[compaction.ARCHIVE_INDEX_KEY] = index
        with cp.lock_for(cp._jobs_path()):
            cp._save_jobs(store)
    else:
        cp._update_job(first, status=prior_state)
    cp._update_job(second, sourceCuts=[old_cut, safe_cut])
    observed = _wire_executor(monkeypatch, tmp_path, [second])
    fetched = []
    cached = cp._cached_source_master

    async def record_fetch(*args):
        fetched.append(args)
        return await cached(*args)

    monkeypatch.setattr(cp, "_cached_source_master", record_fetch)
    await cp._run_dossier_source(second)
    final = cp._load_jobs()["jobs"][second]
    assert final["status"] == "completed", final.get("error")
    assert final["sourceCuts"] == [safe_cut]
    assert len(fetched) == len(observed["renders"]) == 1
    assert len(final["clips"]) == 1


@pytest.mark.asyncio
async def test_all_old_queued_overlap_fails_typed_before_any_media(lab, monkeypatch, tmp_path):
    client, _, _ = lab
    first = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                        headers=headers("legacy-all-prior")).json()["jobId"]
    second = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                         headers=headers("legacy-all-old")).json()["jobId"]
    old_cut = deepcopy(cp._load_jobs()["jobs"][first]["sourceCuts"][0])
    cp._update_job(second, sourceCuts=[old_cut])
    touched = []

    async def forbidden_media(*args):
        touched.append(args)
        raise AssertionError("used footage must refuse before media acquisition")

    monkeypatch.setattr(cp, "_cached_source_master", forbidden_media)
    await cp._run_dossier_source(second)
    final = cp._load_jobs()["jobs"][second]
    assert final["status"] == "failed"
    assert final["error"] == "source_fresh_cut_needed"
    assert touched == []
    assert not final.get("clips")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_bounds", [
    {"startMs": None}, {"startMs": True}, {"durationMs": None},
    {"durationMs": 0}, {"startMs": -1}, {"durationMs": 10**9},
])
async def test_same_master_lost_prior_bounds_refuse_before_media(
        lab, monkeypatch, bad_bounds):
    client, _, _ = lab
    prior = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                        headers=headers("legacy-unknown-prior")).json()["jobId"]
    current = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                          headers=headers("legacy-unknown-current")).json()["jobId"]
    cut = deepcopy(cp._load_jobs()["jobs"][prior]["sourceCuts"][0])
    cut.update(bad_bounds)
    cp._update_job(prior, sourceCuts=[cut])
    touched = []

    async def forbidden_media(*args):
        touched.append(args)
        raise AssertionError("unknown prior bounds cannot establish freshness")

    monkeypatch.setattr(cp, "_cached_source_master", forbidden_media)
    await cp._run_dossier_source(current)
    final = cp._load_jobs()["jobs"][current]
    assert final["status"] == "failed"
    assert final["error"] == "source_freshness_history_unknown"
    assert not touched and not final.get("clips")
