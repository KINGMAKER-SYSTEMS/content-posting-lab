"""Independent failure/release invariants across real persisted operations."""
from datetime import datetime, timedelta, timezone
import pytest
from services import scrambler as s


def test_releasing_pending_support_cannot_leave_an_over_cap_rendered_reverse(tmp_path, monkeypatch):
    monkeypatch.setenv('SCRAMBLER_FAST_SHARE', '0.1')
    monkeypatch.delenv('SCRAMBLER_PAGE_FAST_SHARE', raising=False)
    path = tmp_path / 'history.json'
    page, jobs = 'acct:release-proof', []
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # The real draw algorithm can emit a reverse only if prior counts support
    # it. Reserve concurrent jobs, render the reverse first, then fail all
    # still-pending jobs. A cap based on reversible credits is unsound.
    for i in range(150):
        job = f'job-{i}'
        draw = s.reserve_job_cuts(page, job, 1, path=path,
            now=(now + timedelta(days=i)).isoformat())[0]
        jobs.append(job)
        if draw.treatment == 'reverse':
            s.mark_cut_rendered(page, job, 0, path=path, asset_sha256='a' * 64,
                                rendered_at=(now + timedelta(days=i)).isoformat())
            for failed_job in jobs[:-1]:
                s.release_job_reservations(page, failed_job, path=path, reason='encode_failed')
            break
    rows = s.scrambler_history_rows(page, path=path)
    rendered = [r for r in rows if r['state'] == s.RENDERED]
    reverses = sum(r['treatment'] == 'reverse' for r in rendered)
    assert reverses <= len(rendered) // 15


def test_skipped_first_cut_cleanup_keeps_later_rendered_cut_readable(tmp_path):
    path = tmp_path / 'history.json'
    page, job = 'acct:backup-proof', 'backup-skip'
    s.reserve_job_cuts(page, job, 2, path=path, now='2026-10-29T12:00:00+00:00')
    s.mark_cut_rendered(page, job, 1, path=path, asset_sha256='b' * 64,
                        rendered_at='2026-10-29T12:01:00+00:00')
    s.release_job_reservations(page, job, path=path, reason='first_backup_unavailable')
    rows = s.scrambler_history_rows(page, path=path)
    assert any(r['cutIndex'] == 1 and r['state'] == s.RENDERED for r in rows)
    assert not any(r['cutIndex'] == 0 and r['state'] == s.RESERVED for r in rows)


def test_forced_exhaustive_backstop_honors_all_fast_page_setting(tmp_path, monkeypatch):
    monkeypatch.setenv('SCRAMBLER_FAST_SHARE', '1')
    monkeypatch.delenv('SCRAMBLER_PAGE_FAST_SHARE', raising=False)
    monkeypatch.setattr(s, 'MAX_TRIES', 0)
    draw = s.reserve_job_cuts('acct:fast-proof', 'backstop', 1,
        path=tmp_path / 'history.json', now='2026-10-29T12:00:00+00:00')[0]
    assert draw.speed in (1.1, 1.2, 1.3)


def test_released_operation_never_redraws_or_resurrects(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "job", 2, path=path)
    s.release_job_reservations("page", "job", reason="failed", path=path)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerHistoryCorrupt, match="already_released"):
        s.reserve_job_cuts("page", "job", 2, path=path)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="already_released"):
        s.mark_cut_rendered("page", "job", 0, asset_sha256="a" * 64, path=path)
    assert path.read_bytes() == before


def test_capacity_never_prunes_or_splits_operation_identity(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "history.json"
    monkeypatch.setattr(s, "SCRAMBLER_HISTORY_HORIZON", 3)
    s.reserve_job_cuts("page", "old", 2, path=path)
    s.release_job_reservations("page", "old", reason="failed", path=path)
    s.reserve_job_cuts("page", "live", 1, path=path)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerHistoryCorrupt, match="rows_exceeded"):
        s.reserve_job_cuts("page", "overflow", 1, path=path)
    assert path.read_bytes() == before
    assert [(r["jobId"], r["cutIndex"]) for r in s.scrambler_history_rows("page", path=path)] == [("old", 0), ("old", 1), ("live", 0)]


def test_all_page_live_copies_and_actual_posting_evidence(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    for page in ("one", "two"):
        s.reserve_job_cuts(page, "job", 1, path=path)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="asset_mismatch"):
        s.mark_posting_observed("one", "job", 0, asset_sha256="a" * 64,
            posted_at="2026-10-29T12:00:00Z", page_handle="@actual", path=path)
    s.mark_cut_rendered("one", "job", 0, asset_sha256="a" * 64, path=path)
    for sha, timestamp, handle in (("b" * 64, "2026-10-29T12:00:00Z", "@actual"),
                                   ("a" * 64, "bad", "@actual"),
                                   ("a" * 64, "2026-10-29T12:00:00Z", "bad\nhandle")):
        with pytest.raises(s.ScramblerHistoryCorrupt):
            s.mark_posting_observed("one", "job", 0, asset_sha256=sha,
                posted_at=timestamp, page_handle=handle, path=path)
    for _ in range(2):
        s.mark_posting_observed("one", "job", 0, asset_sha256="a" * 64,
            posted_at="2026-10-29T12:00:00Z", page_handle="@actual", path=path)
    rows = s.live_reservation_rows(path=path)
    assert {r["pageId"] for r in rows} == {"one", "two"}
    assert rows[0]["postingObservation"] == {"assetSha256": "a" * 64,
        "posted_at": "2026-10-29T12:00:00Z", "page": "@actual"}
    rows[0]["postingObservation"]["page"] = "@mutated"
    assert s.live_reservation_rows(path=path)[0]["postingObservation"]["page"] == "@actual"
    s.release_job_reservations("two", "job", reason="failed", path=path)
    assert {r["pageId"] for r in s.live_reservation_rows(path=path)} == {"one"}


def test_history_read_is_bounded_and_durable_default_required(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "job", 1, path=path)
    monkeypatch.setattr(s, "SCRAMBLER_HISTORY_MAX_BYTES", 16)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="bytes_exceeded"):
        s.scrambler_history_rows("page", path=path)
    monkeypatch.delenv("SCRAMBLER_HISTORY_PATH", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="durable_volume_unavailable"):
        s.scrambler_history_path()
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "missing"))
    with pytest.raises(s.ScramblerHistoryCorrupt, match="durable_volume_unavailable"):
        s.scrambler_history_path()
    monkeypatch.setenv("SCRAMBLER_HISTORY_PATH", str(path))
    assert s.scrambler_history_path() == path


def test_planned_binding_is_immutable_and_released_enrichment_refuses(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "job", 1, path=path)
    item = {"cutIndex": 0, "masterSha256": "a" * 64, "masterSourceId": "origin",
            "libraryStartMs": 0, "originStartMs": 600000, "originEndMs": 607000}
    s.enrich_job_reservations("page", "job", [item], path=path)
    s.enrich_job_reservations("page", "job", [item], path=path)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="binding_changed"):
        s.enrich_job_reservations("page", "job", [dict(item, originStartMs=601000)], path=path)
    s.release_job_reservations("page", "job", reason="failed", path=path)
    with pytest.raises(s.ScramblerHistoryCorrupt, match="not_reserved"):
        s.enrich_job_reservations("page", "job", [item], path=path)


def test_render_replay_never_moves_original_render_day(tmp_path):
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "job", 1, path=path, now="2026-10-28T12:00:00Z")
    s.mark_cut_rendered("page", "job", 0, asset_sha256="a" * 64,
        rendered_at="2026-10-28T12:01:00Z", path=path)
    before = path.read_bytes()
    s.mark_cut_rendered("page", "job", 0, asset_sha256="a" * 64,
        rendered_at="2026-10-29T12:01:00Z", path=path)
    assert path.read_bytes() == before
    assert s.scrambler_history_rows("page", path=path)[0]["renderedAt"] == "2026-10-28T12:01:00Z"


def test_existing_jobs_prevent_unknown_bootstrap_when_both_files_lost(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "old", 1, path=path)
    path.unlink()
    path.with_name(path.name + ".created").unlink()
    with pytest.raises(s.ScramblerHistoryLost, match="existing_jobs_missing"):
        s.assert_existing_jobs(["old"], path=path)
    assert not path.exists(), "unknown history must never be bootstrapped even before refusal"
    assert not path.with_name(path.name + ".created").exists()
    # An existing but unrelated ledger also cannot impersonate known history.
    s.reserve_job_cuts("page", "unrelated", 1, path=path)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerHistoryLost, match="existing_jobs_missing"):
        s.reserve_job_cuts("page", "new", 1, path=path, existing_job_ids=["old"])
    assert path.read_bytes() == before
    s.reserve_job_cuts("page", "unrelated", 1, path=path, existing_job_ids=["unrelated"])


def test_private_canonical_identity_is_paired_and_immutable(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    draw = s.reserve_job_cuts("page", "job", 1, path=path)[0]
    item = {"cutIndex":0,"masterSha256":"a"*64,"masterSourceId":"slug",
            "libraryStartMs":0,"originStartMs":600000,"originEndMs":600000+draw.window_ms(),
            "originKind":"drive","originId":"CaseSensitiveID"}
    with pytest.raises(s.ScramblerHistoryCorrupt, match="identity_invalid"):
        s.enrich_job_reservations("page", "job", [{k:v for k,v in item.items() if k != "originId"}], path=path)
    s.enrich_job_reservations("page", "job", [item], path=path)
    assert s.live_reservation_rows(path=path)[0]["originId"] == "CaseSensitiveID"
    with pytest.raises(s.ScramblerHistoryCorrupt, match="binding_changed"):
        s.enrich_job_reservations("page", "job", [dict(item, originId="casesensitiveid")], path=path)

    import json
    data = json.loads(path.read_text())
    original_end = data["pages"]["page"][0]["originEndMs"]
    for wrong_end in (600000, original_end+1):
        data["pages"]["page"][0]["originEndMs"] = wrong_end
        path.write_text(json.dumps(data))
        with pytest.raises(s.ScramblerHistoryCorrupt, match="geometry_invalid"):
            s.scrambler_history_rows("page", path=path)
    data["pages"]["page"][0]["originEndMs"] = original_end
    del data["pages"]["page"][0]["originId"]
    path.write_text(json.dumps(data))
    with pytest.raises(s.ScramblerHistoryCorrupt, match="identity_invalid"):
        s.scrambler_history_rows("page", path=path)


@pytest.mark.parametrize(("axis", "last"), [
    ("length", s.ScramblerDraw("split_swap", 0.65, 8000, 0, True)),
    ("treatment", s.ScramblerDraw("none", 0.7, 8500, 0, True)),
])
def test_release_cannot_create_actual_render_consecutive_repeat(tmp_path, monkeypatch, axis, last):
    import pytest
    path = tmp_path / "history.json"
    forced = [s.ScramblerDraw("none", 0.8, 8000, 0, False)]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    s.reserve_job_cuts("page", "a", 1, path=path)
    s.mark_cut_rendered("page", "a", 0, asset_sha256="a"*64, path=path)
    forced[0] = s.ScramblerDraw("camera", 0.75, 7500, 0, False)
    s.reserve_job_cuts("page", "pending", 1, path=path)
    forced[0] = last
    s.reserve_job_cuts("page", "c", 1, path=path)
    s.release_job_reservations("page", "pending", reason="failed", path=path)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerExhausted, match="render_consecutive_"+axis):
        s.mark_cut_rendered("page", "c", 0, asset_sha256="c"*64, path=path)
    assert path.read_bytes() == before


def test_actual_render_order_is_commit_order_even_with_equal_timestamps(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "history.json"
    forced = [s.ScramblerDraw("none", 0.8, 8000, 0, False)]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    for job, draw in (("a", forced[0]), ("b", s.ScramblerDraw("camera", 0.75, 7500, 0, False)),
                      ("c", s.ScramblerDraw("none", 0.7, 8500, 0, True))):
        forced[0] = draw
        s.reserve_job_cuts("page", job, 1, path=path)
    moment = "2026-10-29T12:00:00Z"
    s.mark_cut_rendered("page", "b", 0, asset_sha256="b"*64, rendered_at=moment, path=path)
    s.mark_cut_rendered("page", "a", 0, asset_sha256="a"*64, rendered_at=moment, path=path)
    with pytest.raises(s.ScramblerExhausted, match="render_consecutive_treatment"):
        s.mark_cut_rendered("page", "c", 0, asset_sha256="c"*64, rendered_at=moment, path=path)


def test_posting_observation_preserves_valid_raw_producer_page_labels(tmp_path):
    import pytest
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "job", 1, path=path)
    s.mark_cut_rendered("page", "job", 0, asset_sha256="a"*64, path=path)
    for label in ("", "x"*257, "acct:bad\nlabel", "acct:bad\x7flabel", "\ud800"):
        with pytest.raises(s.ScramblerHistoryCorrupt, match="posting_page_invalid"):
            s.mark_posting_observed("page", "job", 0, asset_sha256="a"*64,
                posted_at="2026-10-29T12:00:00Z", page_handle=label, path=path)
    label = "acct:CaseSensitiveProducerPage"
    s.mark_posting_observed("page", "job", 0, asset_sha256="a"*64,
        posted_at="2026-10-29T12:00:00Z", page_handle=label, path=path)
    assert s.live_reservation_rows(path=path)[0]["postingObservation"]["page"] == label


def test_cumulative_treatment_weights_preserve_small_weight_mapping(monkeypatch):
    choices = ("none", "camera", "grade")
    for weights in ({"none":2,"camera":0,"grade":3},
                    {"none":0,"camera":0,"grade":0}):
        expanded = [name for name in choices for _ in range(weights[name])]
        if not expanded:
            expanded = [name for name in choices for _ in range(s.DEFAULT_TREATMENT_WEIGHTS[name])]
        for unit in (0.0, 0.1, 0.24999, 0.25, 0.5, 0.999999):
            monkeypatch.setattr(s, "_unit", lambda *args, value=unit: value)
            assert s._weighted_treatment("seed", choices, weights) == expanded[int(unit*len(expanded))]


def test_huge_treatment_weights_use_intervals_without_list_expansion(monkeypatch):
    class GuardedIntegerWeight:
        # Implements the integer arithmetic used by cumulative intervals.
        # Old list multiplication would invoke __index__ and fail BEFORE any
        # dangerous proportional allocation, keeping this regression safe.
        def __init__(self, value): self.value = value
        def __gt__(self, other): return self.value > other
        def __radd__(self, other): return other + self.value
        def __index__(self): raise AssertionError("proportional list allocation forbidden")
    huge = GuardedIntegerWeight(10**1000)
    monkeypatch.setattr(s, "_unit", lambda *args: 0.75)
    assert s._weighted_treatment("seed", ("none", "camera"),
        {"none": huge, "camera": huge}) == "camera"


def test_preflight_refuses_repeated_actual_treatment_without_mutation(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    forced = [s.ScramblerDraw("none", 0.8, 8000, 0, False)]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    for job, draw in (("a", forced[0]), ("b", s.ScramblerDraw("camera", 0.75, 7500, 0, False)),
                      ("c", s.ScramblerDraw("none", 0.7, 8500, 0, True))):
        forced[0] = draw
        s.reserve_job_cuts("page", job, 1, path=path)
    s.mark_cut_rendered("page", "a", 0, asset_sha256="a"*64, path=path)
    before = path.read_bytes()
    with pytest.raises(s.ScramblerExhausted, match="render_consecutive_treatment"):
        s.assert_cut_renderable("page", "c", 0, path=path)
    s.assert_cut_renderable("page", "b", 0, path=path)
    assert path.read_bytes() == before
    s.mark_cut_rendered("page", "b", 0, asset_sha256="b"*64, path=path)
    s.assert_cut_renderable("page", "c", 0, path=path)
    s.mark_cut_rendered("page", "c", 0, asset_sha256="c"*64, path=path)


def test_daily_report_uses_actual_render_order_not_reservation_order(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    forced = [s.ScramblerDraw("none", 0.8, 8000, 0, False)]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    for job, draw in (("a", forced[0]), ("b", s.ScramblerDraw("camera", 0.75, 7500, 0, False)),
                      ("c", s.ScramblerDraw("none", 0.7, 8500, 0, True)),
                      ("d", s.ScramblerDraw("camera", 0.6, 9000, 0, True))):
        forced[0] = draw
        s.reserve_job_cuts("page", job, 1, path=path)
    # Each forced draw must be the stored draw (a refused draw would let the
    # exhaustive backstop store another one, and the test would check that).
    stored = {r["jobId"]: (r["treatment"], r["speed"], r["lengthMs"], r["mirror"])
              for r in s.scrambler_history_rows("page", path=path)}
    assert stored == {"a": ("none", 0.8, 8000, False), "b": ("camera", 0.75, 7500, False),
                      "c": ("none", 0.7, 8500, True), "d": ("camera", 0.6, 9000, True)}
    # Reservation order is A,B,C,D; B is still pending. Actual output order
    # A,D,C has no repetition. A,C,D would create a false none->none alert.
    for job in ("a", "d", "c"):
        s.mark_cut_rendered("page", job, 0, asset_sha256=job*64,
            rendered_at="2026-10-29T12:00:00Z", path=path)
    report = s.scrambler_daily_report(s.scrambler_history_rows("page", path=path),
        now="2026-10-29T12:01:00Z")
    assert report["cuts"] == 3
    assert report["consecutiveTreatmentRepeats"] == 0
    assert report["consecutiveLengthRepeats"] == 0
