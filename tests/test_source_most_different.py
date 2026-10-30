"""Most-different next window (Eric 10-28; MASTER-ITERATION-STRATEGY-1029 §3.2).

These rules now run ONLY on the Scrambler path (a page whose job creation
reserved per-cut spans; services.scrambler + ScramblerPlan): the planner picks,
for each drawn span, the window most different from the page's last few cuts:
(1) never within 120 s of the lane's last 3 completed starts while any such
window exists (a named relaxation when nothing farther exists), (2) prefer a
different master/origin than the previous cut (honouring the 1-in-5 backup
cap), (3) when a per-second dHash file is present, maximise the minimum
Hamming distance of the FULL candidate window (every origin second, the
master's origin offset applied exactly once) against the page's last few
cuts' full windows, falling back to (1)+(2) otherwise. Originality limits are
unchanged: a served slot is never re-cut and another page's reserved window is
never entered. Pages without a scrambler plan keep the base planner
byte-identically (tests/test_scrambler_planner_gate.py).
"""
import hashlib
import json
from dataclasses import replace

import pytest

import services.control_plane_sources as sources
from services.control_plane_sources import (
    BACKUP_VARIETY_EVERY,
    CutUse,
    ScramblerPlan,
    plan_source_cuts,
    source_cut_is_planned,
    source_window_exclusions,
)
from tests.test_source_recut_variety import _recipe
from tests.test_source_pool_roles import _pool
from tests.test_control_plane_source_execution import (  # noqa: F401 (fixture)
    _isolated_source_dna,  # autouse: main #279 isolated source-DNA registry
    offline_shipstream_manifest,
)

PAGE = "most-different-test-page"


@pytest.fixture(autouse=True)
def _offline_manifest(offline_shipstream_manifest):
    """Every test here builds a recipe; keep the ShipStream manifest offline."""
    return offline_shipstream_manifest


def _hex(i: int) -> str:
    return hashlib.sha256(("most-different-second-%d" % i).encode()).hexdigest()[:16]


def _write_fp(tmp_path, master, duration_ms: int):
    count = duration_ms // 1_000 + 2
    per = [_hex(i) for i in range(count)]
    mir = [_hex(i + 1_000_000) for i in range(count)]
    doc = {
        "schema": sources.MASTERS_DHASH_SCHEMA,
        "masters": {
            master.sha256: {"per_second": per, "mirrored_per_second": mir},
        },
    }
    path = tmp_path / "masters-dhash.json"
    path.write_text(json.dumps(doc))
    return path


def _plan_scrambled(recipe, quantity, served, exclusions=None, *, seed,
                    history=None, span_ms=7_000):
    """plan_source_cuts as the Scrambler calls it: one drawn span per cut."""
    return plan_source_cuts(
        recipe, quantity, served, exclusions, seed=seed, history=history,
        scrambler=ScramblerPlan(page_id=PAGE, spans=(span_ms,) * quantity),
    )


def _window_seconds(fp, master, start_ms, duration_ms):
    return sources._window_fingerprint_seconds(fp.get(master.sha256), master,
                                               start_ms, duration_ms)


def _window_dist(fps, master, start_ms, span_ms, recent_windows):
    """The full-window maximin score: min distance over every (candidate
    second, recent second) pair of every recent cut's FULL window."""
    candidate = _window_seconds(fps, master, start_ms, span_ms)
    best = None
    for other_master, other_start, other_duration in recent_windows:
        other = _window_seconds(fps, other_master, other_start, other_duration)
        if not other:
            continue
        distance = min(
            sources._fingerprint_pair_distance(a, b)
            for a in candidate for b in other
        )
        if best is None or distance < best:
            best = distance
    return best


def _single_cut_jobs(recipe, count, served=None, history=None, prefix="job",
                     span_ms=7_000):
    served = set(served or ())
    history = dict(history or {})
    cuts = []
    for run in range(count):
        plan = _plan_scrambled(recipe, 1, served, seed=f"{prefix}-{run}",
                               history=history, span_ms=span_ms)
        if not plan:
            break
        (cut,) = plan
        cuts.append(cut)
        served.add(cut.slot_id)
        history[cut.slot_id] = CutUse(float(run), 0)
    return cuts, served, history


def test_d1_no_two_consecutive_cuts_within_120s_and_full_spread():
    # 200 seeded single-cut plans on one 600 s master: no two consecutive cuts
    # sit within 120 s while a farther window exists, and the spread covers the
    # whole master (histogram).
    recipe, master = _recipe(duration_ms=600_000)
    cuts, _, _ = _single_cut_jobs(recipe, 200)
    assert len(cuts) == 200
    for previous, current in zip(cuts, cuts[1:]):
        assert abs(current.start_ms - previous.start_ms) >= 120_000, (previous, current)
    buckets = [0] * 5
    for cut in cuts:
        buckets[min(cut.start_ms // 120_000, 4)] += 1
    assert all(bucket > 0 for bucket in buckets), buckets


def test_d2_two_masters_switch_when_the_backup_cap_allows():
    recipe, primary, backup = _pool(primary_ms=120_000, backup_ms=120_000)
    cuts, _, _ = _single_cut_jobs(recipe, 30)
    roles = ["backup" if cut.master.sha256 == backup.sha256 else "primary" for cut in cuts]
    assert len(cuts) == 30
    assert 0 < roles.count("backup") <= 30 // BACKUP_VARIETY_EVERY
    # The 1-in-5 backup cap is never broken.
    for index in range(len(roles) - BACKUP_VARIETY_EVERY + 1):
        assert roles[index:index + BACKUP_VARIETY_EVERY].count("backup") <= 1, roles
    # Whenever the previous cut was a primary and the cap allows a backup, the
    # next cut is a different origin; after a backup the cap forces a primary.
    for index in range(1, len(roles)):
        if roles[index - 1] == "backup":
            assert roles[index] == "primary", (index, roles)
        elif "backup" not in roles[max(0, index - (BACKUP_VARIETY_EVERY - 1)):index]:
            assert roles[index] == "backup", (index, roles)


def test_d3_fingerprint_pick_maximises_full_window_min_distance(tmp_path, monkeypatch):
    # Among FRESH candidates the full-window fingerprint maximin is the
    # PRIMARY ordering (fresh-before-used is its only override): the pick
    # achieves the maximum min Hamming distance of the FULL candidate window
    # against the page's recent cuts' full windows - every second inside the
    # window counts, not just the start frame.
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    recipe, master = _recipe(duration_ms=300_000)
    sha = master.sha256
    # Recent cuts at 0 s (7 s) and 10 s (8 s) carry the fingerprint reference;
    # their footage (0-7 s, 10-18 s) is already cut, so candidates there are
    # USED windows - the maximin only rules the fresh remainder.
    served = {f"{sha}:0:7000", f"{sha}:10000:8000"}
    history = {
        f"{sha}:0:7000": CutUse(10.0, 0),
        f"{sha}:10000:8000": CutUse(20.0, 0),
    }
    monkeypatch.setenv(sources.MASTERS_DHASH_ENV, str(_write_fp(tmp_path, master, 300_000)))
    fps = sources.load_masters_dhash()
    assert master.sha256 in fps
    span = 5_000
    (cut,) = _plan_scrambled(recipe, 1, served, seed="d3", history=history,
                             span_ms=span)
    assert cut.duration_ms == span
    recent = [(master, 10_000, 8_000), (master, 0, 7_000)]
    candidates = list(range(0, 300_000 - span + 1, 1_000))
    distances = {start: _window_dist(fps, master, start, span, recent)
                 for start in candidates}
    # Fresh = the window touches no already-cut footage (0-7 s, 10-18 s);
    # FAR = at least 120 s from the recent starts 0 s / 10 s (s >= 130 s).
    # The pick comes from the far pass (far windows exist), so the maximin is
    # asserted over fresh AND far candidates - rule (1) legitimately excludes
    # a nearer window with a higher fingerprint distance.
    fresh_far = [s for s in candidates if s >= 130_000]
    assert fresh_far and cut.start_ms in fresh_far, cut.start_ms
    # The pick achieves the maximum full-window min distance among fresh+far
    # candidates (ties are broken deterministically by the seed, verified by
    # the replay below).
    max_distance = max(distances[start] for start in fresh_far)
    assert distances[cut.start_ms] == max_distance, (
        cut.start_ms, distances[cut.start_ms], max_distance)
    (replay,) = _plan_scrambled(recipe, 1, served, seed="d3", history=history,
                                span_ms=span)
    assert replay == cut


def test_d3b_min_not_mean_a_transient_duplicate_inside_the_window_loses(tmp_path, monkeypatch):
    # A window with a near-duplicate second inside it (min distance 0) must
    # lose to a uniformly-different window even if its average distance is the
    # best: the score is the MINIMUM over the whole window, not the mean.
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    recipe, master = _recipe(duration_ms=60_000)
    sha = master.sha256
    span = 5_000
    far = 0x1111111111111111          # the duplicated second's hash
    per = [_hex(i) for i in range(72)]
    # Second 30 duplicates second 5 (both `far`): any window covering 30 has
    # min distance 0 against a recent cut that covered second 5.
    per[5] = _hash64(far)
    per[30] = _hash64(far)
    mir = [_hex(i + 500) for i in range(len(per))]
    doc = {"schema": sources.MASTERS_DHASH_SCHEMA,
           "masters": {sha: {"per_second": per, "mirrored_per_second": mir}}}
    fp_path = tmp_path / "masters-dhash.json"
    fp_path.write_text(json.dumps(doc))
    monkeypatch.setenv(sources.MASTERS_DHASH_ENV, str(fp_path))
    served = {f"{sha}:0:8000"}
    history = {f"{sha}:0:8000": CutUse(10.0, 0)}
    (cut,) = _plan_scrambled(recipe, 1, served, seed="d3b", history=history,
                             span_ms=span)
    fps = sources.load_masters_dhash()
    recent = [(master, 0, 8_000)]
    # 28_000 covers seconds 28..32 (includes the duplicate at 30): min 0.
    assert _window_dist(fps, master, 28_000, span, recent) == 0
    # 40_000 covers seconds 40..44: all distinct from seconds 0..7.
    assert _window_dist(fps, master, 40_000, span, recent) > 0
    assert cut.start_ms != 28_000, "a near-duplicate second inside the window must lose"
    assert _window_dist(fps, master, cut.start_ms, span, recent) > 0


def _hash64(value: int) -> str:
    return f"{value:016x}"


def test_d4_originality_limits_are_never_broken_by_the_most_different_pick():
    # Property: over many seeds the BASE planner (no scrambler plan) never
    # re-cuts a served slot, never enters another page's reservation, never
    # overlaps inside one plan, and every cut verifies.
    recipe, master = _recipe(duration_ms=120_000)
    sha = master.sha256
    served = {f"{sha}:0:7000", f"{sha}:50000:6000", f"{sha}:110000:5000"}
    exclusions = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 20_000, "endMs": 40_000,
    }])
    for seed in range(50):
        plan = plan_source_cuts(recipe, 5, served, exclusions, seed=f"d4-{seed}")
        for cut in plan:
            assert cut.slot_id not in served, cut
            assert cut.start_ms + cut.duration_ms <= 20_000 or cut.start_ms >= 40_000, cut
            assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id)
        for left in plan:
            for right in plan:
                if left is right:
                    continue
                assert not (
                    left.start_ms < right.start_ms + right.duration_ms
                    and right.start_ms < left.start_ms + left.duration_ms
                ), (left, right)
    # Saturation: once every window of a tiny master is served, the planner
    # answers empty -- it never re-cuts a served slot to keep posting.
    tiny, _ = _recipe(duration_ms=6_000)
    served_tiny: set[str] = set()
    history_tiny: dict[str, CutUse] = {}
    for run in range(20):
        plan = plan_source_cuts(tiny, 1, served_tiny, seed=f"d4-drain-{run}", history=history_tiny)
        if not plan:
            break
        (cut,) = plan
        served_tiny.add(cut.slot_id)
        history_tiny[cut.slot_id] = CutUse(float(run), 0)
    else:
        raise AssertionError("the tiny master never drained")
    assert served_tiny, "the tiny master produced no cuts at all"
    assert plan_source_cuts(tiny, 1, served_tiny, history=history_tiny) == []
    assert plan_source_cuts(tiny, 10, served_tiny, history=history_tiny) == []


def test_d4b_scrambled_spans_hold_the_same_originality_limits():
    # The same property on the scrambler path: drawn spans never re-cut a
    # served slot, never enter a reservation, never overlap inside one plan,
    # and drain to an empty plan instead of re-cutting.
    recipe, master = _recipe(duration_ms=120_000)
    sha = master.sha256
    served = {f"{sha}:0:7000", f"{sha}:50000:6000", f"{sha}:110000:5000"}
    exclusions = source_window_exclusions([{
        "sourceIdentity": master.provenance["sourceUrl"],
        "startMs": 20_000, "endMs": 40_000,
    }])
    for seed in range(50):
        plan = _plan_scrambled(recipe, 5, served, exclusions, seed=f"d4b-{seed}",
                               span_ms=7_000)
        for cut in plan:
            assert cut.slot_id not in served, cut
            assert cut.start_ms + cut.duration_ms <= 20_000 or cut.start_ms >= 40_000, cut
        for left in plan:
            for right in plan:
                if left is right:
                    continue
                assert not (
                    left.start_ms < right.start_ms + right.duration_ms
                    and right.start_ms < left.start_ms + left.duration_ms
                ), (left, right)
    tiny, _ = _recipe(duration_ms=6_000)
    served_tiny: set[str] = set()
    history_tiny: dict[str, CutUse] = {}
    for run in range(20):
        plan = _plan_scrambled(tiny, 1, served_tiny, seed=f"d4b-drain-{run}",
                               history=history_tiny, span_ms=5_000)
        if not plan:
            break
        (cut,) = plan
        served_tiny.add(cut.slot_id)
        history_tiny[cut.slot_id] = CutUse(float(run), 0)
    else:
        raise AssertionError("the tiny scrambled master never drained")
    assert served_tiny, "the tiny scrambled master produced no cuts at all"
    assert _plan_scrambled(tiny, 1, served_tiny, history=history_tiny,
                           seed="d4b-drained", span_ms=5_000) == []


def test_d5_single_master_without_fingerprints_obeys_the_120s_rule():
    # No second master and no fingerprint file: rule (1) still holds, no crash,
    # and the planner logs the fallback once.
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    monkeypatch_delenv = pytest.MonkeyPatch()
    try:
        monkeypatch_delenv.delenv(sources.MASTERS_DHASH_ENV, raising=False)
        recipe, _ = _recipe(duration_ms=600_000)
        cuts, _, _ = _single_cut_jobs(recipe, 60)
        assert len(cuts) == 60
        for previous, current in zip(cuts, cuts[1:]):
            assert abs(current.start_ms - previous.start_ms) >= 120_000, (previous, current)
    finally:
        monkeypatch_delenv.undo()


def test_d6_far_rule_relaxes_only_when_no_far_window_exists(monkeypatch, caplog):
    # A master so small that no 7 s window sits 120 s from the last starts: the
    # pick relaxes with one named log, never silently.
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    monkeypatch.delenv(sources.MASTERS_DHASH_ENV, raising=False)
    recipe, master = _recipe(duration_ms=20_000)
    sha = master.sha256
    served = {f"{sha}:0:7000"}
    history = {f"{sha}:0:7000": CutUse(10.0, 0)}
    import logging
    with caplog.at_level(logging.WARNING, logger="services.control_plane_sources"):
        (cut,) = _plan_scrambled(recipe, 1, served, seed="d6", history=history,
                                 span_ms=7_000)
    assert cut.duration_ms == 7_000
    assert any("far-relaxed" in record.getMessage() for record in caplog.records), \
        [record.getMessage() for record in caplog.records]


def test_fingerprint_file_absent_falls_back_and_logs_once():
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    monkeypatch_delenv = pytest.MonkeyPatch()
    try:
        monkeypatch_delenv.delenv(sources.MASTERS_DHASH_ENV, raising=False)
        assert sources.load_masters_dhash() == {}
        assert "absent" in sources._dhash_logged
        sources._dhash_logged.clear()
        assert sources.load_masters_dhash() == {}
        # Logged once, not once per call.
        assert "absent" in sources._dhash_logged
    finally:
        monkeypatch_delenv.undo()


def test_fingerprint_file_wrong_schema_never_blocks(tmp_path, monkeypatch):
    sources._dhash_cache.clear()
    sources._dhash_logged.clear()
    bad = tmp_path / "bad-dhash.json"
    bad.write_text(json.dumps({"schema": "wrong.v1", "masters": {}}))
    monkeypatch.setenv(sources.MASTERS_DHASH_ENV, str(bad))
    recipe, _ = _recipe(duration_ms=120_000)
    assert sources.load_masters_dhash() == {}
    assert any("schema" in key for key in sources._dhash_logged)
    # A plan still comes back on rules (1)+(2).
    (cut,) = _plan_scrambled(recipe, 1, set(), seed="fallback", span_ms=7_000)
    assert cut.duration_ms == 7_000


def test_fingerprint_compares_both_normal_and_mirrored_hashes():
    # A mirrored copy of a frame is the same footage: distance must be 0.
    frame = (0x0123456789ABCDEF, 0xFEDCBA9876543210)
    mirror = (0xFEDCBA9876543210, 0x0123456789ABCDEF)
    assert sources._fingerprint_pair_distance(frame, mirror) == 0
    assert sources._hamming(0, 0) == 0
    assert sources._hamming(0, 0xFFFFFFFFFFFFFFFF) == 64


def test_window_fingerprint_is_master_local_origin_offset_never_applied():
    # The producer bundle (originality.masters-dhash.v1, the originality
    # lead's routine of 10-29) indexes per_second on the MASTER FILE's own
    # timeline: a window at library start 3 s reads master seconds 3..7, and
    # the master's absolute ORIGIN offset is never applied here - it is
    # applied exactly once, in the originality accounting (originStartMs /
    # window_allowed), so applying it in the fingerprint index too would
    # double-shift every window.
    recipe, master = _recipe(duration_ms=60_000)
    offset_master = replace(master, source_offset_ms=12_000)
    fp = {
        "per_second": [f"{i:016x}" for i in range(100)],
        "mirrored_per_second": [f"{i + 500:016x}" for i in range(100)],
    }
    seconds = sources._window_fingerprint_seconds(
        fp, offset_master, 3_000, 5_000)
    assert seconds == [(fp["per_second"][i], fp["mirrored_per_second"][i])
                       for i in (3, 4, 5, 6, 7)]
    # Full coverage only: a window whose tail runs past the arrays scores NO
    # distance at all, never a prefix-scored one.
    assert sources._window_fingerprint_seconds(
        fp, offset_master, 97_000, 5_000) == []
    assert sources._window_fingerprint_seconds(
        fp, offset_master, 99_500, 1_000) == []


def test_dhash_actual_read_respects_size_cap_after_stat_race(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from services import control_plane_sources as s
    path=tmp_path/"hashes.json"
    path.write_text(json.dumps({"schema":s.MASTERS_DHASH_SCHEMA,"masters":{"a"*64:{"per_second":["0"*16],"mirrored_per_second":["0"*16]}}}))
    real=Path.stat
    def tiny_stat(self,*a,**kw):
        result=real(self,*a,**kw)
        if self==path:
            return SimpleNamespace(st_size=1,st_mtime_ns=result.st_mtime_ns)
        return result
    monkeypatch.setenv(s.MASTERS_DHASH_ENV,str(path))
    monkeypatch.setattr(s,"MASTERS_DHASH_MAX_BYTES",4)
    monkeypatch.setattr(Path,"stat",tiny_stat)
    actual=s.load_masters_dhash()
    print("accepted over-cap fingerprint map",actual)
    assert actual=={}, "Advisory fingerprint feed that grows beyond cap must fall back, never read/accept unbounded data"
