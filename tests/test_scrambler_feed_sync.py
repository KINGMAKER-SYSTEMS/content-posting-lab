"""Provisioning/sync contract for the Scrambler originality feed (MED-1)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tools import scrambler_feed_sync as feed_sync

NOW = time.time()


def write_feed(tmp_path, *, usage=None, origins=None, usage_age_s=None):
    src = tmp_path / "src"
    src.mkdir()
    (src / "usage.jsonl").write_text(
        usage if usage is not None else '{"page":"@pilot"}\n')
    (src / "origins.json").write_text(origins if origins is not None else "{}")
    if usage_age_s is not None:
        stamp = NOW - usage_age_s  # same clock as sync_feed(now_epoch=NOW)
        os.utime(src / "usage.jsonl", (stamp, stamp))
    return src


def test_sync_copies_fresh_feed_atomically(tmp_path):
    src = write_feed(tmp_path)
    dest = tmp_path / "dest"
    result = feed_sync.sync_feed(src, dest, now_epoch=NOW)
    assert (dest / "usage.jsonl").read_text() == '{"page":"@pilot"}\n'
    assert (dest / "origins.json").read_text() == "{}"
    assert result["usageBytes"] > 0 and result["originsBytes"] > 0


def test_sync_accepts_recent_usage_within_staleness_window(tmp_path):
    src = write_feed(tmp_path, usage_age_s=3600)
    feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)


def test_sync_refuses_missing_usage(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "origins.json").write_text("{}")
    with pytest.raises(feed_sync.FeedSyncRefused):
        feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)


def test_sync_refuses_missing_origins(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "usage.jsonl").write_text('{"page":"@pilot"}\n')
    with pytest.raises(feed_sync.FeedSyncRefused):
        feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)


def test_sync_refuses_stale_usage(tmp_path):
    src = write_feed(tmp_path, usage_age_s=feed_sync.originality.USAGE_STALE_AFTER_S + 60)
    with pytest.raises(feed_sync.FeedSyncRefused):
        feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)


def test_sync_refuses_oversized_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(feed_sync.originality, "USAGE_MAX_BYTES", 16, raising=False)
    src = write_feed(tmp_path, usage='{"page":"@pilot"}\n' * 4)  # 68 bytes > 16
    with pytest.raises(feed_sync.FeedSyncRefused):
        feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)


def test_sync_refuses_invalid_origins_json(tmp_path):
    src = write_feed(tmp_path, origins="not-json{")
    with pytest.raises(feed_sync.FeedSyncRefused):
        feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=NOW)
