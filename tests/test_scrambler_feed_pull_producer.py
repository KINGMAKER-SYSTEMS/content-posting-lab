"""Scrambler feed pull against the producer's key layout, plus the one-path rules.

The producer is control-plane tools/launchd/bin/originality-feed.sh. It puts
three objects at ``rt-content-buckets/originality/feed/``. The manifest contract
is the puller's ``parse_manifest`` and is NOT relaxed here (lead note 10-30):

    {"generated_at": "<ISO 8601 UTC>", "rows": <int>,
     "sha256s": {"usage.jsonl": "<hex64>", "origins.json": "<hex64>"}}

Extra fields (the producer's ``dropped_unknown_origin``) are ignored. The r3
producer's flat-only manifest (``usage_sha256`` / ``origins_sha256`` with no
``sha256s`` map) is refused by name; producer r4 writes the map. The pilot env is
SCRAMBLER_FEED_R2_BUCKET=rt-content-buckets, SCRAMBLER_FEED_R2_PREFIX=originality/feed/
(docs/SCRAMBLER.md "Pilot environment (exact)").
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services import scrambler_feed_pull as sfp
from services import scrambler_preflight as pf
from services.scrambler_feed_pull import ScramblerFeedPullError, pull_once, sha256_hex
from tools import scrambler_feed_sync as feed_sync

R3_FLAT_MANIFEST_FMT = ('{"generated_at":"%s","rows":%s,"usage_sha256":"%s",'
                        '"origins_sha256":"%s","dropped_unknown_origin":%s}\n')

MASTER = "b" * 64


def _usage(n: int = 3) -> bytes:
    rows = [json.dumps({
        "page": f"@page{i}", "posted_at": "2026-10-28T12:00:00-0400",
        "video_id": str(7_000_000_000_000_000_000 + i), "final_sha": "f" * 64,
        "asset_sha": "a" * 64, "origin_kind": "yt", "origin_id": f"vid{i:02d}",
        "origin_start_s": 0.0, "origin_end_s": 9.0, "master_sha": MASTER,
        "recipe": {"speed": None, "reverse": None, "loop_split": None, "crop": None, "hflip": None},
        "fp": f"fp/finals/{'f' * 64}.fp.json", "prov": "d1", "gaps": [],
    }) for i in range(n)]
    return ("\n".join(rows) + "\n").encode()


def _origins() -> bytes:
    return json.dumps({MASTER: {"page": "page0", "origin_kind": "yt", "origin_id": "vid00",
                                "origin_start_s": 0.0, "origin_end_s": 9.0,
                                "evidence": "manifest", "confidence": "high"}}).encode()


def _producer_manifest(usage: bytes, origins: bytes, *, usage_sha: str | None = None,
                       dropped: int = 0) -> bytes:
    """The contract shape, with the producer's extra field and UTC Z timestamp."""
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = sum(1 for line in usage.split(b"\n") if line.strip())
    return (json.dumps({"generated_at": gen, "rows": rows,
                        "sha256s": {"usage.jsonl": usage_sha or sha256_hex(usage),
                                    "origins.json": sha256_hex(origins)},
                        "dropped_unknown_origin": dropped}, separators=(",", ":")) + "\n").encode()


def _seed(dest: Path, files: dict[str, bytes]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (dest / name).write_bytes(raw)
        os.utime(dest / name, (1_600_000_000, 1_600_000_000))


# --- the env prefix IS the feed directory (pilot values) ----------------------

def test_pilot_env_fetches_the_exact_producer_keys(tmp_path, monkeypatch):
    """With the documented pilot env the pull asks R2 for the producer's keys."""
    monkeypatch.setenv("SCRAMBLER_FEED_R2_BUCKET", "rt-content-buckets")
    monkeypatch.setenv("SCRAMBLER_FEED_R2_PREFIX", "originality/feed/")
    usage_path = tmp_path / "projects" / "originality" / "usage.jsonl"
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", str(usage_path))
    usage, origins = _usage(), _origins()
    manifest = _producer_manifest(usage, origins)
    bucket = {  # what `wrangler r2 object put rt-content-buckets/originality/feed/<f>` stores
        "originality/feed/manifest.json": manifest,
        "originality/feed/usage.jsonl": usage,
        "originality/feed/origins.json": origins,
    }
    calls = []

    def fake_r2_fetch(b, key):
        calls.append((b, key))
        if key not in bucket:
            raise KeyError(f"NoSuchKey {key}")
        return bucket[key]

    monkeypatch.setattr(sfp, "r2_fetch", fake_r2_fetch)
    result = sfp.run_feed_pull_once()
    assert calls == [("rt-content-buckets", "originality/feed/manifest.json"),
                     ("rt-content-buckets", "originality/feed/usage.jsonl"),
                     ("rt-content-buckets", "originality/feed/origins.json")]
    assert result.wrote == ("manifest.json", "origins.json", "usage.jsonl")
    assert usage_path.read_bytes() == usage
    assert (usage_path.parent / "origins.json").read_bytes() == origins


# --- the manifest contract (sha256s map required) -----------------------------

def test_producer_manifest_shape_is_verified_and_written(tmp_path):
    dest = tmp_path / "o"
    _seed(dest, {"usage.jsonl": b"OLD-U\n", "origins.json": b"{}", "manifest.json": b"{}"})
    usage, origins = _usage(4), _origins()
    manifest = _producer_manifest(usage, origins)
    result = pull_once(lambda n: {"manifest.json": manifest, "usage.jsonl": usage,
                                  "origins.json": origins}[n], dest)
    assert result.warnings == ()
    assert (dest / "usage.jsonl").read_bytes() == usage
    assert (dest / "origins.json").read_bytes() == origins
    assert (dest / "manifest.json").read_bytes() == manifest
    assert time.time() - (dest / "usage.jsonl").stat().st_mtime < 60
    parsed = sfp.parse_manifest(manifest)
    assert parsed.rows == 4
    assert parsed.sha256s == {"usage.jsonl": sha256_hex(usage), "origins.json": sha256_hex(origins)}


def test_producer_manifest_sha_mismatch_keeps_old_files(tmp_path):
    dest = tmp_path / "o"
    old = {"usage.jsonl": b"OLD-U\n", "origins.json": b"{}", "manifest.json": b"{}"}
    _seed(dest, old)
    usage, origins = _usage(), _origins()
    manifest = _producer_manifest(usage, origins, usage_sha="c" * 64)
    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(lambda n: {"manifest.json": manifest, "usage.jsonl": usage,
                             "origins.json": origins}[n], dest)
    assert exc.value.reason == "sha256_mismatch:usage.jsonl"
    for name, raw in old.items():
        assert (dest / name).read_bytes() == raw


def test_manifest_without_the_sha256s_map_is_refused_by_name(tmp_path):
    """The sha256s map is required: a flat-only (r3-shaped) manifest keeps the old files."""
    dest = tmp_path / "o"
    old = {"usage.jsonl": b"OLD-U\n", "origins.json": b"{}", "manifest.json": b"{}"}
    _seed(dest, old)
    usage, origins = _usage(), _origins()
    flat = (R3_FLAT_MANIFEST_FMT % ("2026-10-30T05:00:00Z", 3, sha256_hex(usage),
                                    sha256_hex(origins), 0)).encode()
    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(lambda n: {"manifest.json": flat, "usage.jsonl": usage,
                             "origins.json": origins}[n], dest)
    assert exc.value.reason == "manifest_invalid: sha256s missing or not an object"
    for name, raw in old.items():
        assert (dest / name).read_bytes() == raw


# --- rollback restores usage.jsonl before origins.json (review MED-1) --------

def test_rollback_never_pairs_old_origins_with_new_usage(tmp_path, monkeypatch):
    """A failed manifest replace rolls back usage.jsonl FIRST, then origins.json.

    At every rollback rename the on-disk pair must be generation-consistent:
    OLD origins beside NEW usage (a usage row whose master is absent from the
    map) is the fail-open window the write order exists to prevent.
    """
    dest = tmp_path / "o"
    old_usage, old_origins = b"OLD-U\n", b'{"old": {}}'
    _seed(dest, {"usage.jsonl": old_usage, "origins.json": old_origins, "manifest.json": b"{}"})
    usage, origins = _usage(), _origins()
    manifest = _producer_manifest(usage, origins)
    real_replace = os.replace
    restored: list[str] = []
    bad_pairs: list[tuple[bytes, bytes]] = []

    def spy_replace(src, dst):
        if str(src).endswith(".tmp") and Path(dst).name == "usage.jsonl":
            raise OSError("injected usage replace failure")
        real_replace(src, dst)
        if str(src).endswith(".bak"):
            restored.append(Path(dst).name)
            pair = ((dest / "origins.json").read_bytes(), (dest / "usage.jsonl").read_bytes())
            if pair == (old_origins, usage):
                bad_pairs.append(pair)

    monkeypatch.setattr(sfp.os, "replace", spy_replace)
    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(lambda n: {"manifest.json": manifest, "usage.jsonl": usage,
                             "origins.json": origins}[n], dest)
    assert exc.value.reason.startswith("write_failed")
    assert restored == ["origins.json", "manifest.json"]
    assert bad_pairs == []
    assert (dest / "usage.jsonl").read_bytes() == old_usage
    assert (dest / "origins.json").read_bytes() == old_origins


# --- one feed path: the manual sync tool writes in the same order -------------

def test_sync_tool_replaces_origins_before_usage(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "usage.jsonl").write_bytes(_usage())
    (src / "origins.json").write_bytes(_origins())
    order: list[str] = []
    real_replace = os.replace

    def spy_replace(a, b):
        order.append(Path(b).name)
        return real_replace(a, b)

    monkeypatch.setattr(feed_sync.os, "replace", spy_replace)
    feed_sync.sync_feed(src, tmp_path / "dest", now_epoch=time.time())
    assert order == ["manifest.json", "origins.json", "usage.jsonl"]
    assert sfp.WRITE_ORDER == ("manifest.json", "origins.json", "usage.jsonl")


# --- preflight: the pull writes where the loader reads ------------------------

def _pilot_env(tmp_path: Path) -> dict:
    volume = tmp_path / "projects"
    feed = volume / "originality"
    feed.mkdir(parents=True)
    (feed / "usage.jsonl").write_text("")
    (feed / "origins.json").write_text("{}")
    return {
        "SCRAMBLER_ENABLED": "1",
        "SCRAMBLER_PAGES": "acct:trailheadtravis,acct:backpack-bohemian,acct:freddy-2am",
        "SCRAMBLER_FAST_SHARE": "0",
        "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT": "1",
        "CONTENT_LAB_ORIGINALITY_USAGE_PATH": str(feed / "usage.jsonl"),
        "SCRAMBLER_FEED_R2_BUCKET": "rt-content-buckets",
        "SCRAMBLER_FEED_R2_PREFIX": "originality/feed/",
        "RAILWAY_VOLUME_MOUNT_PATH": str(volume),
        "SCRAMBLER_HISTORY_PATH": str(volume / "scrambler-history.json"),
    }


def test_preflight_names_a_loader_path_the_pull_never_writes(tmp_path):
    env = _pilot_env(tmp_path)
    assert pf.preflight_problems(env) == []
    feed = Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent
    # The explicit beside-usage origins path is fine.
    ok = dict(env, CONTENT_LAB_ORIGINALITY_ORIGINS_PATH=str(feed / "origins.json"))
    assert pf.preflight_problems(ok) == []
    # An origins override elsewhere is never refreshed by the pull.
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "origins.json").write_text("{}")
    moved = dict(env, CONTENT_LAB_ORIGINALITY_ORIGINS_PATH=str(other / "origins.json"))
    problems = pf.preflight_problems(moved)
    assert any("CONTENT_LAB_ORIGINALITY_ORIGINS_PATH must be unset" in p for p in problems), problems
    # A ledger file name other than usage.jsonl is never refreshed either.
    (feed / "ledger.jsonl").write_text("")
    renamed = dict(env, CONTENT_LAB_ORIGINALITY_USAGE_PATH=str(feed / "ledger.jsonl"))
    problems = pf.preflight_problems(renamed)
    assert any("must end in /usage.jsonl" in p for p in problems), problems
