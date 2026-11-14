"""Posted-asset release evidence (H4 reader API) contracts.

``OriginalityIndex.posted_asset_observation`` is the PRIVATE query that
releases a planner asset hold ONLY on an observed-posted usage row for
that exact asset. These tests pin: exact asset + case-sensitive identity
+ full whole-second coverage returning the producer's VERBATIM timestamp
and page; conservative refusals (wrong asset, wrong-case identity,
partial range, invalid bounds); known-origin wildcard coverage with
unknown-identity refusal; latest-of-several postings without a later
partial posting shadowing an earlier covering one; metadata counting in
the shared aggregate entry budget; the documented >90-day proof
unavailability; and that ``window_allowed`` still governs show limits
for an observed asset.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from services import scrambler_originality as originality  # noqa: E402

NOW = datetime.now(timezone.utc)
SHA = "a" * 64
ASSET = "c" * 64
POSTED_AT = (NOW - timedelta(days=2)).isoformat()
POSTED_EPOCH = (NOW - timedelta(days=2)).timestamp()


def build(tmp_path, rows=(), *, mapping=None, retired=()):
    usage = tmp_path / "usage.jsonl"
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows))
    entry = mapping or {"page": "@pilot", "origin_kind": "drive",
                        "origin_id": "CaseSensitiveID",
                        "origin_start_s": 1200, "origin_end_s": 1800}
    (tmp_path / "origins.json").write_text(json.dumps({SHA: entry}))
    retired_path = tmp_path / "retired.json"
    retired_path.write_text(json.dumps(list(retired)))
    return originality.load_originality_index(
        usage, retired_path=retired_path, now_epoch=NOW.timestamp())


def show(*, page="@pilot", days=2, start=1200, end=1204,
         origin_id="CaseSensitiveID", asset=ASSET, sha=SHA):
    row = {"page": page, "posted_at": (NOW - timedelta(days=days)).isoformat(),
           "master_sha": sha, "origin_kind": "drive", "origin_id": origin_id,
           "origin_start_s": start, "origin_end_s": end}
    if asset is not None:
        row["asset_sha"] = asset
    return row


def observe(index, asset=ASSET, kind="drive", origin_id="CaseSensitiveID",
            start=1200000, end=1204000):
    return index.posted_asset_observation(asset, kind, origin_id, start, end)


def test_full_coverage_returns_verbatim_producer_posted_at_and_page(tmp_path):
    index = build(tmp_path, [show()])
    assert observe(index) == {"page": "@pilot", "posted_at": POSTED_AT,
                              "posted_epoch": POSTED_EPOCH}


def test_wrong_or_unknown_asset_never_releases(tmp_path):
    index = build(tmp_path, [show()])
    assert observe(index, asset="d" * 64) is None
    assert observe(build(tmp_path)) is None  # empty ledger: no evidence at all


def test_origin_identity_matches_case_sensitively(tmp_path):
    index = build(tmp_path, [show()])
    assert observe(index, origin_id="casesensitiveid") is None
    assert observe(index, origin_id="CaseSensitiveID") is not None


@pytest.mark.parametrize(("start", "end"), [
    (1199000, 1204000),  # second 1199 not covered by the posting
    (1200000, 1205000),  # second 1204 not covered: posting ends at 1203
    (1500000, 1504000),  # far outside the posting entirely
])
def test_partial_coverage_refuses(tmp_path, start, end):
    index = build(tmp_path, [show()])
    assert observe(index, start=start, end=end) is None


def test_fractional_ms_row_uses_the_same_second_accounting_as_limits(tmp_path):
    # A posting 1200.0..1204.0005 covers second 1204 too (ceil - 1), exactly
    # like window_allowed counts that fractional millisecond as used.
    index = build(tmp_path, [show(start=1200.0, end=1204.0005)])
    assert observe(index, start=1204000, end=1205000) is not None
    assert observe(index, start=1205000, end=1206000) is None


def test_wildcard_known_origin_covers_unknown_identity_refuses(tmp_path):
    index = build(tmp_path, [show(start=None, end=None)])
    assert observe(index, start=1500000, end=1504000) is not None
    assert observe(index, origin_id="OtherID",
                   start=1500000, end=1504000) is None


def test_latest_of_multiple_covering_postings_wins(tmp_path):
    index = build(tmp_path, [show(page="@earlier", days=5),
                             show(page="@later", days=1)])
    assert observe(index) == {"page": "@later",
                              "posted_at": (NOW - timedelta(days=1)).isoformat(),
                              "posted_epoch": (NOW - timedelta(days=1)).timestamp()}


def test_later_partial_posting_does_not_shadow_earlier_covering_one(tmp_path):
    # The recent row covers only 1202..1203: preferring it would release on a
    # partial range, so the earlier FULL posting is the evidence returned.
    index = build(tmp_path, [show(page="@full", days=5, start=1200, end=1204),
                             show(page="@partial", days=1, start=1202, end=1204)])
    assert observe(index)["page"] == "@full"


@pytest.mark.parametrize(("start", "end"), [
    (-1000, 2000), (1200000, 1200000), (1204000, 1200000),
    (1200000.5, 1204000), ("1200000", 1204000),
])
def test_invalid_bounds_return_none(tmp_path, start, end):
    assert observe(build(tmp_path, [show()]), start=start, end=end) is None


def test_non_string_asset_or_kind_return_none(tmp_path):
    index = build(tmp_path, [show()])
    assert observe(index, asset=None) is None
    assert observe(index, asset=123) is None
    assert observe(index, kind=None) is None
    assert observe(index, kind="") is None


def test_sha_prefix_and_case_are_normalized_but_ids_are_not(tmp_path):
    index = build(tmp_path, [show()])
    assert observe(index, asset="SHA256:" + ASSET.upper()) is not None


def test_result_dict_is_fresh_and_query_is_pure(tmp_path):
    index = build(tmp_path, [show()])
    first = observe(index)
    first["page"] = "@mutated"
    first["posted_at"] = "2026-01-01T00:00:00+00:00"  # never invents one later
    before = (dict(index.shows), dict(index.wildcard_shows), dict(index.posted_assets))
    assert observe(index) == {"page": "@pilot", "posted_at": POSTED_AT,
                              "posted_epoch": POSTED_EPOCH}
    after = (dict(index.shows), dict(index.wildcard_shows), dict(index.posted_assets))
    assert before == after


def test_metadata_records_count_in_the_shared_entry_budget(tmp_path, monkeypatch):
    rows = [show(days=1, start=1200, end=1201), show(days=2, start=1200, end=1201)]
    # 2 one-second show entries + 2 metadata records = 4 stored entries.
    monkeypatch.setattr(originality, "USAGE_MAX_INDEX_ENTRIES", 3, raising=False)
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="usage_ledger_too_large"):
        build(tmp_path, rows)
    # Without asset_sha the same rows store only 2 entries: metadata is what
    # tipped the aggregate budget, and rows without it load unchanged.
    monkeypatch.setattr(originality, "USAGE_MAX_INDEX_ENTRIES", 3, raising=False)
    index = build(tmp_path, [show(days=1, start=1200, end=1201, asset=None),
                             show(days=2, start=1200, end=1201, asset=None)])
    assert not index.posted_assets
    assert observe(index) is None


def test_rows_without_asset_sha_produce_no_posting_evidence(tmp_path):
    index = build(tmp_path, [show(asset=None)])
    assert not index.posted_assets
    assert observe(index) is None


def test_old_rows_leave_no_posting_proof(tmp_path):
    # Documented unavailability: rows older than the 90-day lookback are
    # skipped before capture - main persists a copied actual observation
    # when it sees one; the reader never invents an old timestamp.
    index = build(tmp_path, [show(days=120)])
    assert not index.posted_assets
    assert observe(index) is None


def test_observed_asset_still_faces_window_allowed_show_limits(tmp_path):
    # Release evidence and show permission are separate: an observed posting
    # of the exact asset exists, yet a recent OTHER-page show of a covered
    # second still refuses the window - no double counting, no bypass.
    index = build(tmp_path, [show(page="@other", days=1)])
    assert observe(index) is not None
    allowed, reason = index.window_allowed(
        "acct:pilot", SHA, 1200000, 1204000,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert not allowed and reason.startswith("cross_page_90d"), reason
