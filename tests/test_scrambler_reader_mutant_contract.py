"""Stable behavioral quadrants for current unused-only reader mutation proofs.

Exercise a synthetic feed matching the reader contract and canonical reader; recipes, page names
and history age never authorize reuse. These tests intentionally use no media,
network, generation endpoint or production originality files.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest
from services import scrambler_originality as reader

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _paths(tmp_path, rows=(), *, include_alias=True, lost_window=False):
    usage = tmp_path / "usage.jsonl"
    usage.write_text("".join(json.dumps(row) + "\n" for row in rows))
    entry = {"page": "@pilot", "origin_kind": "drive", "origin_id": "CaseSensitiveID",
             "origin_start_s": 1200, "origin_end_s": 1800}
    origins = {SHA_A: entry}
    if include_alias:
        origins[SHA_B] = {**entry, **({"origin_start_s": None, "origin_end_s": None}
                                    if lost_window else {})}
    (tmp_path / "origins.json").write_text(json.dumps(origins))
    retired = tmp_path / "retired.json"
    retired.write_text("[]")
    return usage, retired


def _index(tmp_path, rows=(), **kwargs):
    usage, retired = _paths(tmp_path, rows, **kwargs)
    return reader.load_originality_index(usage, retired_path=retired, now_epoch=NOW.timestamp())


def _show(days, page, *, wildcard=False):
    return {"page": page, "posted_at": (NOW - timedelta(days=days)).isoformat(),
            "master_sha": SHA_A, "origin_kind": "drive", "origin_id": "CaseSensitiveID",
            "origin_start_s": None if wildcard else 1200,
            "origin_end_s": None if wildcard else 1203}


def _allowed(index, start=1200000, end=1203000, *, clock=None):
    return index.window_allowed("acct:pilot", SHA_B, start, end,
        page_handles=frozenset({"@pilot"}),
        now_epoch=NOW.timestamp() if clock is None else clock)[0]


@pytest.mark.parametrize("page", ["@pilot", "@elsewhere"])
@pytest.mark.parametrize("days", [0, 8, 31, 91, 3650])
def test_reader_contract_prior_show_never_expires_or_changes_page(tmp_path, page, days):
    assert not _allowed(_index(tmp_path, [_show(days, page)]))


def test_reader_contract_unused_adjacent_moment_remains_eligible(tmp_path):
    assert _allowed(_index(tmp_path, [_show(3650, "@pilot")]), 1203000, 1206000)


@pytest.mark.parametrize("page", ["@pilot", "@elsewhere"])
@pytest.mark.parametrize("days", [8, 3650])
def test_reader_contract_wildcard_blocks_unplaced_prior_usage(tmp_path, page, days):
    index = _index(tmp_path, [_show(days, page, wildcard=True)])
    assert not _allowed(index)
    assert not _allowed(index, 1300000, 1303000)


def test_reader_contract_unknown_origin_cannot_certify_unused(tmp_path):
    assert not _allowed(_index(tmp_path, include_alias=False))


def test_reader_contract_unknown_window_cannot_certify_unused(tmp_path):
    assert not _allowed(_index(tmp_path, lost_window=True))


@pytest.mark.parametrize("start,end", [(1199000, 1202000), (1799000, 1801000)])
def test_reader_contract_mapped_bounds_refuse_positive_outside_extent(tmp_path, start, end):
    assert not _allowed(_index(tmp_path), start, end)


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
def test_reader_contract_invalid_query_clock_refuses_even_unused_history(tmp_path, clock):
    assert not _allowed(_index(tmp_path), clock=clock)


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), float("-inf")])
def test_reader_contract_invalid_loader_clock_refuses_empty_feed(tmp_path, clock):
    usage, retired = _paths(tmp_path)
    with pytest.raises(reader.ScramblerOriginalityUnavailable, match="usage_clock_invalid"):
        reader.load_originality_index(usage, retired_path=retired, now_epoch=clock)


def test_reader_contract_wildcard_entry_budget_is_enforced_without_asset_metadata(
        tmp_path, monkeypatch):
    rows = [_show(8, "@pilot", wildcard=True),
            _show(9, "@elsewhere", wildcard=True)]
    assert all("asset_sha" not in row for row in rows)
    # Only wildcard appends consume entries: metadata/per-second guards cannot
    # mask a missing wildcard guard in this two-row, one-entry budget.
    monkeypatch.setattr(reader, "USAGE_MAX_INDEX_ENTRIES", 1)
    with pytest.raises(reader.ScramblerOriginalityUnavailable,
                       match="usage_ledger_too_large"):
        _index(tmp_path, rows)


def test_reader_contract_posted_metadata_entry_budget_is_checked_before_allocation(
        tmp_path, monkeypatch):
    prefix = _show(8, "@pilot", wildcard=True)
    asset_row = {**_show(9, "@pilot"), "asset_sha": "c" * 64}
    assert "asset_sha" not in prefix
    # The first wildcard fills the budget. The next metadata entry must refuse
    # BEFORE allocation, not rely on its later per-second guard to raise.
    monkeypatch.setattr(reader, "USAGE_MAX_INDEX_ENTRIES", 1)
    original_observation = reader._PostedObservation
    allocations = []

    def record_observation(*args, **kwargs):
        allocations.append((args, kwargs))
        return original_observation(*args, **kwargs)

    monkeypatch.setattr(reader, "_PostedObservation", record_observation)
    with pytest.raises(reader.ScramblerOriginalityUnavailable,
                       match="usage_ledger_too_large"):
        _index(tmp_path, [prefix, asset_row])
    assert allocations == [], "over-budget metadata must not be allocated"
