"""Different master files cannot reserve the same underlying source seconds."""
from dataclasses import replace
import json
import pytest
from services.control_plane_sources import ScramblerPlan, plan_source_cuts
from services.scrambler_originality import load_originality_index
from tests.test_source_recut_variety import _recipe
from tests.test_control_plane_source_execution import PAGE_ID, offline_shipstream_manifest
from tests.test_control_plane_source_execution import _isolated_source_dna  # noqa: F401 - autouse: main #279 isolated source-DNA registry


@pytest.fixture(autouse=True)
def offline(offline_shipstream_manifest):
    return offline_shipstream_manifest


def test_same_job_never_reserves_overlapping_origin_via_two_master_shas(tmp_path):
    recipe, original = _recipe(duration_ms=5000)
    a = replace(original, sha256="a" * 64, source_id="copy-a")
    b = replace(original, sha256="b" * 64, source_id="copy-b", role="backup")
    recipe = replace(recipe, masters=(a, b))
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    origins = {master.sha256: {"page": "@pilot", "origin_kind": "drive",
        "origin_id": "SameCaseSensitiveOrigin", "origin_start_s": 600,
        "origin_end_s": 605} for master in (a, b)}
    (tmp_path / "origins.json").write_text(json.dumps(origins))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    index = load_originality_index(usage, retired_path=retired)
    # Both are real possible Scrambler spans: 6s*.8=4.8s and 9s*.5=4.5s.
    # A five-second origin cannot fit both without overlapping, regardless
    # of their master filenames, roles, start jitter, or treatment.
    cuts = plan_source_cuts(recipe, 2, set(), seed="same-origin-two-copies",
        scrambler=ScramblerPlan(page_id=PAGE_ID, spans=(4800, 4500),
            originality=index, page_handles=frozenset({"@pilot"})))
    windows = [index.canonical_origin_window(c.master.sha256, c.start_ms, c.duration_ms)
               for c in cuts]
    assert len(cuts) <= 1, windows


def test_pending_commitment_holds_aliases_until_exact_post_observed(tmp_path, monkeypatch):
    from services import scrambler as core
    from services.control_plane_sources import scrambler_pending_windows
    from datetime import datetime, timezone, timedelta
    ledger = tmp_path / "private.json"
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(ledger))
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "0")
    recipe, master = _recipe(duration_ms=20000)
    a = replace(master, sha256="a"*64, source_id="copy-a")
    b = replace(master, sha256="b"*64, source_id="copy-b")
    recipe = replace(recipe, masters=(b,))
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    origins = {m.sha256: {"page":"@pilot", "origin_kind":"drive",
        "origin_id":"Canonical", "origin_start_s":600, "origin_end_s":620}
        for m in (a,b)}
    (tmp_path / "origins.json").write_text(json.dumps(origins))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    draw = core.reserve_job_cuts("other-page", "prior-job", 1, path=ledger)[0]
    start = 600000 + draw.offset_ms
    end = start + draw.window_ms()
    core.enrich_job_reservations("other-page", "prior-job", [{
        "cutIndex":0, "masterSha256":a.sha256, "masterSourceId":a.source_id,
        "libraryStartMs":0, "originStartMs":start, "originEndMs":end}], path=ledger)
    index = load_originality_index(usage, retired_path=retired)
    holds = scrambler_pending_windows(index, "new-job")
    assert holds == (("drive","Canonical",start,end),)
    # Different page, different SHA, different source duration still collides.
    assert not plan_source_cuts(recipe, 1, set(), seed="held-alias",
        scrambler=ScramblerPlan(PAGE_ID, (20000,), index,
            frozenset({"@pilot"}), holds))
    asset = "c"*64
    core.mark_cut_rendered("other-page", "prior-job", 0, asset_sha256=asset, path=ledger)
    assert scrambler_pending_windows(index, "new-job") == holds
    posted_at = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    row = {"page":"@pilot", "posted_at":posted_at, "asset_sha":asset,
        "origin_kind":"drive", "origin_id":"Canonical",
        "origin_start_s":start/1000, "origin_end_s":end/1000}
    # Wrong asset cannot free the hold.
    usage.write_text(json.dumps({**row,"asset_sha":"d"*64})+"\n")
    wrong = load_originality_index(usage, retired_path=retired)
    assert scrambler_pending_windows(wrong, "new-job") == holds
    usage.write_text(json.dumps(row)+"\n")
    posted = load_originality_index(usage, retired_path=retired)
    assert scrambler_pending_windows(posted, "new-job") == ()
    # Eight days later, one genuine prior show permits the second own show.
    assert posted.window_allowed(PAGE_ID, b.sha256, start, end,
        page_handles=frozenset({"@pilot"}))[0]
    # Durable actual-post proof survives a rotated feed; never render-time proof.
    usage.write_text("")
    rotated = load_originality_index(usage, retired_path=retired)
    assert scrambler_pending_windows(rotated, "new-job") == ()
    observation = core.live_reservation_rows(path=ledger)[0]["postingObservation"]
    assert observation["posted_at"] == posted_at
    assert observation["assetSha256"] == asset


@pytest.mark.parametrize("fractional", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_adjacent_windows_cannot_share_origin_second_bins(tmp_path, fractional, pending):
    # Exact five-second master extents force precisely adjacent windows:
    # [600.2,605.2) and [605.2,610.2) are disjoint in milliseconds but share
    # second605. The integer-boundary control must remain usable.
    recipe, original = _recipe(duration_ms=5000)
    a = replace(original, sha256="a" * 64, source_id="left-copy")
    b = replace(original, sha256="b" * 64, source_id="right-copy")
    shift = 0.2 if fractional else 0
    recipe = replace(recipe, masters=(b,) if pending else (a, b))
    origins = {a.sha256: {"page":"@pilot", "origin_kind":"drive",
        "origin_id":"AdjacentCanonical", "origin_start_s":600+shift,
        "origin_end_s":605+shift}, b.sha256: {"page":"@pilot", "origin_kind":"drive",
        "origin_id":"AdjacentCanonical", "origin_start_s":605+shift,
        "origin_end_s":610+shift}}
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    (tmp_path / "origins.json").write_text(json.dumps(origins))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    index = load_originality_index(usage, retired_path=retired)
    holds = (("drive", "AdjacentCanonical", int((600+shift)*1000),
              int((605+shift)*1000)),) if pending else ()
    quantity = 1 if pending else 2
    cuts = plan_source_cuts(recipe, quantity, set(), seed="adjacent-fractional",
        scrambler=ScramblerPlan(PAGE_ID, (5000,)*quantity, index,
            frozenset({"@pilot"}), holds))
    assert len(cuts) == (quantity - 1 if fractional else quantity), cuts


def test_recent_origin_start_is_shared_across_master_aliases(tmp_path, monkeypatch):
    from services import control_plane_sources as sources
    recipe, original = _recipe(duration_ms=300000)
    a = replace(original, sha256="a"*64, source_id="history-copy")
    b = replace(original, sha256="b"*64, source_id="fresh-alias")
    recipe = replace(recipe, masters=(a,b))
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    (tmp_path / "origins.json").write_text(json.dumps({m.sha256: {
        "page":"@pilot", "origin_kind":"drive", "origin_id":"SameUpstream",
        "origin_start_s":600, "origin_end_s":900} for m in (a,b)}))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    index = load_originality_index(usage, retired_path=retired)
    # Alias B's near starts have a maximal visual score against the prior A
    # cut. They MUST still lose to legal >=120s-away windows on either file.
    zeros = (0,)*300
    near_best = ((1<<64)-1,)*120 + (0,)*180
    monkeypatch.setattr(sources, "load_masters_dhash", lambda: {
        a.sha256:{"per_second":zeros,"mirrored_per_second":zeros},
        b.sha256:{"per_second":near_best,"mirrored_per_second":near_best}})
    history = {f"{a.sha256}:0:5000": sources.CutUse(1.0, 0)}
    cuts = plan_source_cuts(recipe, 1, set(), seed="canonical-near-alias",
        history=history, scrambler=ScramblerPlan(PAGE_ID, (5000,), index,
            frozenset({"@pilot"})))
    assert len(cuts) == 1
    window = index.canonical_origin_window(cuts[0].master.sha256,
        cuts[0].start_ms, cuts[0].duration_ms)
    assert window[2] - 600000 >= 120000, window


def test_removed_alias_spacing_survives_posting_and_producer_map_rotation(tmp_path, monkeypatch):
    from services import scrambler as core
    from services.control_plane_sources import scrambler_recent_canonical_starts
    ledger = tmp_path / "private.json"
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(ledger))
    recipe, master = _recipe(duration_ms=130000)
    old_sha, new_sha = "a"*64, "b"*64
    recipe = replace(recipe, masters=(replace(master, sha256=new_sha, source_id="new-copy"),))
    draw = core.reserve_job_cuts(PAGE_ID, "old-alias-job", 1, path=ledger)[0]
    start = 600000 + draw.offset_ms
    core.enrich_job_reservations(PAGE_ID, "old-alias-job", [{
        "cutIndex":0, "masterSha256":old_sha, "masterSourceId":"removed-copy",
        "libraryStartMs":0, "originStartMs":start, "originEndMs":start+draw.window_ms(),
        "originKind":"drive", "originId":"CanonicalCaseSensitive"}], path=ledger)
    core.mark_cut_rendered(PAGE_ID, "old-alias-job", 0, asset_sha256="c"*64, path=ledger)
    core.mark_posting_observed(PAGE_ID, "old-alias-job", 0, asset_sha256="c"*64,
        posted_at="2026-10-01T12:00:00Z", page_handle="@pilot", path=ledger)
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    # Producer has rotated OLD SHA away. Only the new alias is mapped now.
    (tmp_path / "origins.json").write_text(json.dumps({new_sha:{"page":"@pilot",
        "origin_kind":"drive", "origin_id":"CanonicalCaseSensitive",
        "origin_start_s":600, "origin_end_s":730}}))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    index = load_originality_index(usage, retired_path=retired)
    starts = scrambler_recent_canonical_starts(index, PAGE_ID, "new-job")
    assert len(starts) == 1 and starts[0].master_sha256 == old_sha
    assert starts[0].start_ms == start
    assert scrambler_recent_canonical_starts(index, "another-page", "new-job") == ()
    assert scrambler_recent_canonical_starts(index, PAGE_ID, "old-alias-job") == ()
    cuts = plan_source_cuts(recipe, 1, set(), seed="removed-alias-near-start",
        scrambler=ScramblerPlan(PAGE_ID, (5000,), index, frozenset({"@pilot"}),
            prior_canonical_starts=starts))
    assert len(cuts) == 1
    window = index.canonical_origin_window(new_sha, cuts[0].start_ms, cuts[0].duration_ms)
    assert abs(window[2] - start) >= 120000, window
    # A conflicting surviving producer mapping must not overwrite the durable
    # identity, even when the actual-post observation was already copied.
    origins = json.loads((tmp_path / "origins.json").read_text())
    origins[old_sha] = {"page":"@pilot", "origin_kind":"drive",
        "origin_id":"ConflictingIdentity", "origin_start_s":600, "origin_end_s":730}
    (tmp_path / "origins.json").write_text(json.dumps(origins))
    conflict = load_originality_index(usage, retired_path=retired)
    with pytest.raises(core.ScramblerHistoryCorrupt, match="binding_mismatch"):
        scrambler_recent_canonical_starts(conflict, PAGE_ID, "new-job")
