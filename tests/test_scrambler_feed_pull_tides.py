"""Tides red-proof (final3 c0540fa): the hourly pull must not turn a stale
producer feed into a 'fresh' ledger for the Scrambler's 26 h freshness gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import scrambler_originality as so
from services.scrambler_feed_pull import pull_once, sha256_hex
from tests.test_scrambler_feed_pull import _make_fetch, _manifest, _origins, _usage_rows


def test_three_day_old_producer_feed_is_still_refused_after_a_pull(tmp_path):
    usage, origins = _usage_rows(), _origins()
    stale_at = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    objects = {"manifest.json": _manifest(usage, origins, generated_at=stale_at),
               "usage.jsonl": usage, "origins.json": origins}
    dest = tmp_path / "originality"
    result = pull_once(_make_fetch(objects), dest)
    assert "manifest_stale" in result.warnings          # the pull knows the data is 3 days old
    # The loader's freshness gate exists so that a feed nobody refreshed refuses ON.
    # The data is 72 h old (> USAGE_STALE_AFTER_S = 26 h), so ON must refuse.
    with pytest.raises(so.ScramblerOriginalityUnavailable, match="usage_ledger_stale"):
        so.load_originality_index(str(dest / "usage.jsonl"),
                                  origins_path=str(dest / "origins.json"))


def test_hourly_loop_makes_no_pull_when_off(monkeypatch):
    """The scheduler thread starts on every boot; with SCRAMBLER_ENABLED unset
    or 0 its loop must never call the pull (no R2 call, no write)."""
    import services.scrambler_feed_pull as sfp
    calls = []
    monkeypatch.setattr(sfp, "run_feed_pull_once", lambda: calls.append(1))
    for value in (None, "0"):
        if value is None:
            monkeypatch.delenv("SCRAMBLER_ENABLED", raising=False)
        else:
            monkeypatch.setenv("SCRAMBLER_ENABLED", value)
        sfp._FEED_PULL_STOP.set()          # one loop pass, then exit
        try:
            sfp._feed_pull_loop()
        finally:
            sfp._FEED_PULL_STOP.clear()
    assert calls == []
    monkeypatch.setenv("SCRAMBLER_ENABLED", "1")
    sfp._FEED_PULL_STOP.set()
    try:
        sfp._feed_pull_loop()
    finally:
        sfp._FEED_PULL_STOP.clear()
    assert calls == [1]                     # ON really pulls (the test is not vacuous)
