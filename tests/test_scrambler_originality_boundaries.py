"""Canonical-origin conversion and bounded-reader regression tests.

Covers the three surfaces this fix added, beyond the 13 independent reader
contracts: ``canonical_origin_window`` (upstream offset + extent validation,
private purity), the future-own-page age clamp (M3), and the byte/entry
bounds around every feed read (KG-C incl. the stat/read growth race).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import scrambler_originality as originality  # noqa: E402

NOW = datetime.now(timezone.utc)
SHA = "a" * 64
OTHER_SHA = "b" * 64


def build(tmp_path, rows=(), *, mapping=None, retired=()):
    usage = tmp_path / "usage.jsonl"
    usage.write_text("".join(__import__("json").dumps(r) + "\n" for r in rows))
    entry = mapping or {"page": "@pilot", "origin_kind": "drive",
                        "origin_id": "CaseSensitiveID",
                        "origin_start_s": 1200, "origin_end_s": 1800}
    (tmp_path / "origins.json").write_text(__import__("json").dumps(
        {SHA: entry, OTHER_SHA: entry}))
    retired_path = tmp_path / "retired.json"
    retired_path.write_text(__import__("json").dumps(list(retired)))
    return originality.load_originality_index(
        usage, retired_path=retired_path, now_epoch=NOW.timestamp())


def show(*, page="@other", days=0, start=1200, end=1203,
         origin_id="CaseSensitiveID", sha=SHA):
    return {"page": page, "posted_at": (NOW - timedelta(days=days)).isoformat(),
            "master_sha": sha, "origin_kind": "drive", "origin_id": origin_id,
            "origin_start_s": start, "origin_end_s": end}


# ---------------------------------------------------------------- canonical_origin_window

def test_canonical_origin_window_applies_upstream_offset(tmp_path):
    index = build(tmp_path)
    assert index.canonical_origin_window(SHA, 5000, 3000) == (
        "drive", "CaseSensitiveID", 1205000, 1208000)


def test_canonical_origin_window_unknown_master_raises(tmp_path):
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="origin_mapping_unknown"):
        build(tmp_path).canonical_origin_window("f" * 64, 0, 3000)


def test_canonical_origin_window_lost_window_raises(tmp_path):
    mapping = {"page": "@pilot", "origin_kind": "drive",
               "origin_id": "CaseSensitiveID",
               "origin_start_s": None, "origin_end_s": None}
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="origin_window_unknown"):
        build(tmp_path, mapping=mapping).canonical_origin_window(SHA, 0, 3000)


@pytest.mark.parametrize(("start", "duration", "why"), [
    (-1, 3000, "negative local start"),
    (5000, 0, "zero duration"),
    (598000, 5000, "leaves the mapped extent"),
    (500.5, 3000, "non-integer milliseconds"),
    ("5000", 3000, "non-numeric input"),
])
def test_canonical_origin_window_rejects_invalid_local_windows(
        tmp_path, start, duration, why):
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="candidate_window_invalid|usage"):
        build(tmp_path).canonical_origin_window(SHA, start, duration)


def test_canonical_origin_window_extent_bound_is_exact(tmp_path):
    index = build(tmp_path)
    # The whole mapped extent is 600000 ms: filling it exactly is fine...
    assert index.canonical_origin_window(SHA, 0, 600000)[2:] == (1200000, 1800000)
    # ...one millisecond past it is refused.
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="candidate_window_invalid"):
        index.canonical_origin_window(SHA, 0, 600001)


def test_canonical_origin_window_is_pure_accounting(tmp_path):
    index = build(tmp_path)
    before = (dict(index.shows), dict(index.wildcard_shows), dict(index.origins))
    index.canonical_origin_window(SHA, 5000, 3000)
    after = (dict(index.shows), dict(index.wildcard_shows), dict(index.origins))
    assert before == after


def test_window_allowed_accepts_the_canonical_pair_verbatim(tmp_path):
    index = build(tmp_path)
    kind, oid, start_ms, end_ms = index.canonical_origin_window(SHA, 5000, 3000)
    allowed, reason = index.window_allowed(
        "acct:pilot", SHA, start_ms, end_ms,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert allowed, reason


# ---------------------------------------------------------------- future own-page (M3)

def test_future_own_page_show_blocks_via_gap_not_skip(tmp_path):
    index = build(tmp_path, [show(page="@pilot", days=-0.5)])  # 12 h ahead
    allowed, reason = index.window_allowed(
        "acct:pilot", OTHER_SHA, 1200000, 1203000,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert not allowed and reason.startswith("page_second_gap_7d"), reason


def test_future_own_page_wildcard_blocks_too(tmp_path):
    index = build(tmp_path, [show(page="@pilot", days=-0.5,
                                  start=None, end=None)])
    allowed, reason = index.window_allowed(
        "acct:pilot", OTHER_SHA, 1200000, 1203000,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert not allowed and reason.startswith("page_second_gap_7d"), reason


def test_future_cross_page_show_blocks(tmp_path):
    index = build(tmp_path, [show(page="@other", days=-0.5)])
    allowed, reason = index.window_allowed(
        "acct:pilot", OTHER_SHA, 1200000, 1203000,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert not allowed and reason.startswith("cross_page_90d"), reason


def test_posted_beyond_24h_skew_refuses_the_feed(tmp_path):
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="posted_at in the future"):
        build(tmp_path, [show(page="@pilot", days=-2)])


# ---------------------------------------------------------------- bytes/entry bounds (KG-C)

def test_usage_read_is_bounded_even_when_stat_lies(tmp_path, monkeypatch):
    # File-growth race: stat reports a tiny fresh file, the read is still
    # capped at USAGE_MAX_BYTES and refuses instead of slurping.
    import json as _json
    index_path = tmp_path / "usage.jsonl"
    index_path.write_text("x" * 500)
    entry = {"page": "@pilot", "origin_kind": "drive",
             "origin_id": "CaseSensitiveID",
             "origin_start_s": 1200, "origin_end_s": 1800}
    (tmp_path / "origins.json").write_text(_json.dumps({SHA: entry}))
    (tmp_path / "retired.json").write_text("[]")
    monkeypatch.setattr(originality, "USAGE_MAX_BYTES", 100)
    real_stat = Path.stat

    def lying_stat(self, **kwargs):
        if self == index_path:
            return SimpleNamespace(st_mtime=NOW.timestamp(), st_size=1,
                                   st_mode=0o100644)
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", lying_stat)
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="usage_ledger_too_large"):
        originality.load_originality_index(
            index_path, origins_path=tmp_path / "origins.json",
            retired_path=Path(tmp_path / "retired.json"),
            now_epoch=NOW.timestamp())


def test_origins_and_retired_reads_are_byte_bounded(tmp_path, monkeypatch):
    real_origins_cap = originality.ORIGINS_MAX_BYTES
    monkeypatch.setattr(originality, "ORIGINS_MAX_BYTES", 16)
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="origins_map_invalid"):
        build(tmp_path)
    monkeypatch.setattr(originality, "ORIGINS_MAX_BYTES", real_origins_cap)
    monkeypatch.setattr(originality, "RETIRED_MAX_BYTES", 2)
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="retired_list_invalid"):
        build(tmp_path, retired=[{"master_sha256s": [SHA]}])


def test_entry_budget_counts_duplicate_entries_on_the_same_second(
        tmp_path, monkeypatch):
    monkeypatch.setattr(originality, "USAGE_MAX_INDEX_ENTRIES", 1)
    with pytest.raises(originality.ScramblerOriginalityUnavailable,
                       match="usage_ledger_too_large"):
        # Two one-second shows on the SAME second: 2 stored entries > 1.
        build(tmp_path, [show(days=8, page="@pilot", start=1200, end=1201),
                         show(days=9, page="@pilot", start=1200, end=1201)])


def test_rows_older_than_ninety_days_are_not_indexed(tmp_path):
    index = build(tmp_path, [show(page="@other", days=120)])
    assert index.seconds_indexed() == 0
    assert not index.wildcard_shows
    allowed, reason = index.window_allowed(
        "acct:pilot", OTHER_SHA, 1200000, 1203000,
        page_handles=frozenset({"@pilot"}), now_epoch=NOW.timestamp())
    assert allowed, reason


def test_enormous_integer_window_refuses_without_overflow(tmp_path):
    with pytest.raises(originality.ScramblerOriginalityUnavailable, match="candidate_window_invalid"):
        build(tmp_path).canonical_origin_window(SHA, 10 ** 500, 3000)


def test_usage_partial_millisecond_in_next_origin_second_still_counts(tmp_path):
    index = build(tmp_path, [show(start=1200.0, end=1201.0005)])
    allowed, reason = index.window_allowed('acct:pilot', SHA, 1201000, 1202000,
        page_handles=frozenset({'@pilot'}), now_epoch=NOW.timestamp())
    assert not allowed, 'every covered origin second counts, even a fractional millisecond'
    assert reason.startswith('cross_page_90d')
