"""Offline tests for services.scrambler_feed_pull (R2 stubbed via injected fetch).

The puller is byte-agnostic: it sha256-verifies, counts rows, and copies verbatim.
Test payloads mirror the real producer shapes (usage.jsonl row shape and
origins.json shape from tools/originality/*/fixtures); the manifest contract is
{generated_at, rows, sha256s} (tests/test_scrambler_feed_pull_producer.py pins it).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import scrambler_feed_pull as sfp
from services.scrambler_feed_pull import (
    PullResult,
    ScramblerFeedPullError,
    pull_once,
    sha256_hex,
)


def _usage_rows(n: int = 3) -> bytes:
    """usage.jsonl in the real producer row shape (one JSON line per confirmed post)."""
    rows = []
    for i in range(n):
        rows.append(json.dumps({
            "page": f"@page{i}",
            "posted_at": "2026-10-28T12:00:00-0400",
            "video_id": str(7_000_000_000_000_000_000 + i),
            "final_sha": "f" * 64,
            "asset_sha": "a" * 64,
            "origin_kind": "yt",
            "origin_id": f"vid{i:02d}",
            "origin_start_s": 0.0,
            "origin_end_s": 9.0,
            "master_sha": "b" * 64,
            "recipe": {"speed": None, "reverse": None, "loop_split": None,
                       "crop": None, "hflip": None},
            "fp": f"fp/finals/{'f' * 64}.fp.json",
            "prov": "d1",
            "gaps": [],
        }))
    return ("\n".join(rows) + "\n").encode()


def _origins() -> bytes:
    """origins.json in the real producer map shape (master sha -> canonical origin)."""
    return json.dumps({
        "b" * 64: {
            "page": "page0", "origin_kind": "yt", "origin_id": "vid00",
            "origin_start_s": 0.0, "origin_end_s": 9.0,
            "evidence": "manifest", "confidence": "high",
        },
    }).encode()


def _manifest(usage: bytes, origins: bytes, *, generated_at: str | None = None,
              rows: int | None = None) -> bytes:
    return json.dumps({
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "rows": rows if rows is not None else sum(1 for l in usage.split(b"\n") if l.strip()),
        "sha256s": {
            "usage.jsonl": sha256_hex(usage),
            "origins.json": sha256_hex(origins),
        },
    }).encode()


def _make_fetch(objects: dict[str, bytes]):
    def fetch(name: str) -> bytes:
        if name not in objects:
            raise KeyError(name)
        return objects[name]
    return fetch


def _feed_generation(tags: list[str], rows: int = 3) -> dict[str, bytes]:
    """A self-consistent feed generation.

    Usage rows reference the latest tag's master_sha; origins.json is cumulative
    over every tag (as in production, where the origins map only grows), so an
    OLDER usage file is always a subset of a NEWER origins map.
    """
    current = tags[-1]
    master = (current * 64)[:64]
    usage_rows = []
    for i in range(rows):
        usage_rows.append(json.dumps({
            "page": f"@page{current}{i}",
            "posted_at": "2026-10-28T12:00:00-0400",
            "video_id": str(7_000_000_000_000_000_000 + i),
            "final_sha": "f" * 64,
            "asset_sha": "a" * 64,
            "origin_kind": "yt",
            "origin_id": f"vid{current}{i:02d}",
            "origin_start_s": 0.0,
            "origin_end_s": 9.0,
            "master_sha": master,
            "recipe": {"speed": None, "reverse": None, "loop_split": None,
                       "crop": None, "hflip": None},
            "fp": f"fp/finals/{'f' * 64}.fp.json",
            "prov": "d1",
            "gaps": [],
        }))
    usage = ("\n".join(usage_rows) + "\n").encode()
    origins = {
        (tag * 64)[:64]: {
            "page": f"page{tag}", "origin_kind": "yt", "origin_id": f"vid{tag}00",
            "origin_start_s": 0.0, "origin_end_s": 9.0,
            "evidence": "manifest", "confidence": "high",
        }
        for tag in tags
    }
    origins_bytes = json.dumps(origins).encode()
    return {
        "manifest.json": _manifest(usage, origins_bytes),
        "usage.jsonl": usage,
        "origins.json": origins_bytes,
    }


def _seed_old_files(dest, contents: dict[str, bytes]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        path = dest / name
        path.write_bytes(content)
        os.utime(path, (1_600_000_000, 1_600_000_000))


def test_good_manifest_writes_files_with_fresh_mtime(tmp_path):
    dest = tmp_path / "originality"
    _seed_old_files(dest, {"manifest.json": b"OLD\n", "usage.jsonl": b"OLD\n",
                          "origins.json": b"OLD\n"})
    usage = _usage_rows(3)
    origins = _origins()
    manifest = _manifest(usage, origins)
    objects = {"manifest.json": manifest, "usage.jsonl": usage, "origins.json": origins}

    t0 = time.time()
    result = pull_once(_make_fetch(objects), dest)

    assert result.wrote == ("manifest.json", "origins.json", "usage.jsonl")
    assert result.warnings == ()
    for name, content in (("usage.jsonl", usage), ("origins.json", origins),
                          ("manifest.json", manifest)):
        path = dest / name
        assert path.read_bytes() == content
        mtime = path.stat().st_mtime
        # generated_at is now here, so the data files' mtime (the manifest time,
        # Tides MED-F) and the manifest's mtime (the pull time) are both fresh.
        assert mtime >= t0 - 1 and mtime < t0 + 60, f"{name} mtime {mtime} not fresh"
        assert mtime > 1_600_000_000


def test_sha_mismatch_keeps_old_files(tmp_path):
    dest = tmp_path / "o"
    old = {"manifest.json": b"OLD-M\n", "usage.jsonl": b"OLD-U\n", "origins.json": b"OLD-O\n"}
    _seed_old_files(dest, old)

    usage = _usage_rows(2)
    origins = _origins()
    manifest = json.loads(_manifest(usage, origins))
    manifest["sha256s"]["usage.jsonl"] = "0" * 64  # wrong digest
    manifest = json.dumps(manifest).encode()

    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                               "origins.json": origins}), dest)
    assert exc.value.reason.startswith("sha256_mismatch:usage.jsonl")
    for name, content in old.items():
        assert (dest / name).read_bytes() == content
        assert (dest / name).stat().st_mtime == 1_600_000_000  # untouched


def test_missing_file_keeps_old_files(tmp_path):
    dest = tmp_path / "o"
    old = {"manifest.json": b"OLD-M\n", "usage.jsonl": b"OLD-U\n", "origins.json": b"OLD-O\n"}
    _seed_old_files(dest, old)

    usage = _usage_rows(2)
    origins = _origins()
    manifest = _manifest(usage, origins)

    # origins.json absent from the bucket
    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage}), dest)
    assert exc.value.reason.startswith("fetch_failed:origins.json")
    for name, content in old.items():
        assert (dest / name).read_bytes() == content
        assert (dest / name).stat().st_mtime == 1_600_000_000


def test_sha256s_missing_entry_keeps_old_files(tmp_path):
    dest = tmp_path / "o"
    old = {"usage.jsonl": b"OLD-U\n", "origins.json": b"OLD-O\n", "manifest.json": b"OLD-M\n"}
    _seed_old_files(dest, old)

    usage = _usage_rows(2)
    origins = _origins()
    manifest = json.loads(_manifest(usage, origins))
    del manifest["sha256s"]["origins.json"]  # manifest fails to name origins.json
    manifest = json.dumps(manifest).encode()

    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                               "origins.json": origins}), dest)
    assert exc.value.reason == "sha256_missing:origins.json"
    for name, content in old.items():
        assert (dest / name).read_bytes() == content


def test_rows_mismatch_keeps_old_files(tmp_path):
    dest = tmp_path / "o"
    old = {"usage.jsonl": b"OLD-U\n", "origins.json": b"OLD-O\n", "manifest.json": b"OLD-M\n"}
    _seed_old_files(dest, old)

    usage = _usage_rows(3)
    origins = _origins()
    manifest = _manifest(usage, origins, rows=2)  # wrong row count

    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                               "origins.json": origins}), dest)
    assert exc.value.reason.startswith("rows_mismatch")
    assert (dest / "usage.jsonl").read_bytes() == b"OLD-U\n"


def test_stale_manifest_warns_but_writes(tmp_path, caplog):
    dest = tmp_path / "o"
    dest.mkdir()
    usage = _usage_rows(2)
    origins = _origins()
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    manifest = _manifest(usage, origins, generated_at=stale)

    with caplog.at_level(logging.WARNING):
        result = pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                                        "origins.json": origins}), dest)

    assert result.warnings == ("manifest_stale",)
    assert "manifest_stale" in caplog.text
    assert (dest / "usage.jsonl").read_bytes() == usage  # still written fresh


def test_fresh_manifest_does_not_warn(tmp_path, caplog):
    dest = tmp_path / "o"
    dest.mkdir()
    usage = _usage_rows(2)
    origins = _origins()
    manifest = _manifest(usage, origins)  # generated_at = now

    with caplog.at_level(logging.WARNING):
        result = pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                                        "origins.json": origins}), dest)
    assert result.warnings == ()
    assert "manifest_stale" not in caplog.text


def test_off_path_does_not_fetch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sfp, "run_feed_pull_once",
                        lambda: calls.append(1) or PullResult(wrote=(), warnings=()))
    monkeypatch.setenv("SCRAMBLER_ENABLED", "0")
    assert sfp.maybe_pull_once() is None
    assert calls == []  # OFF: no fetch, no write

    monkeypatch.setenv("SCRAMBLER_ENABLED", "1")
    assert sfp.maybe_pull_once() is not None
    assert calls == [1]


def test_enabled_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("SCRAMBLER_ENABLED", raising=False)
    assert sfp.scrambler_enabled_now() is False
    monkeypatch.setenv("SCRAMBLER_ENABLED", "1")
    assert sfp.scrambler_enabled_now() is True


def test_concurrent_read_never_sees_partial_file(tmp_path):
    dest = tmp_path / "o"
    dest.mkdir()
    old = b"OLD-CONTENT\n" * 2000
    (dest / "usage.jsonl").write_bytes(old)
    valid = {sha256_hex(old)}

    payload_a = b"AAAA-CONTENT\n" * 2000
    payload_b = b"BBBB-CONTENT\n" * 2000
    valid |= {sha256_hex(payload_a), sha256_hex(payload_b)}
    origins = _origins()

    stop = threading.Event()
    seen: list[str] = []

    def reader():
        path = dest / "usage.jsonl"
        while not stop.is_set():
            try:
                seen.append(sha256_hex(path.read_bytes()))
            except FileNotFoundError:
                continue

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(40):
            content = payload_a if i % 2 == 0 else payload_b
            manifest = _manifest(content, origins, rows=2000)
            pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": content,
                                   "origins.json": origins}), dest)
    finally:
        stop.set()
        thread.join()

    bad = sorted({h for h in seen if h not in valid})
    assert not bad, f"reader observed partial/unknown content: {bad[:3]}"
    assert (valid - {sha256_hex(old)}) & set(seen), "reader never observed a new full file"


def test_write_order_never_exposes_mixed_generation(tmp_path, monkeypatch):
    """At every data-file replace, the ON-DISK origins/usage pair is consistent.

    ``load_originality_index`` reads origins.json THEN usage.jsonl. The writer
    replaces origins.json before usage.jsonl, so the files on disk never form
    NEW usage.jsonl beside OLD origins.json. This checks that disk invariant
    deterministically at each replace boundary. It does NOT mean a reader can
    never pair generations: a reader that read OLD origins.json before both
    renames and reads usage.jsonl after them still does (see WRITE_ORDER).
    """
    dest = tmp_path / "o"
    gen_a = _feed_generation(["A"])
    gen_b = _feed_generation(["A", "B"])
    dest.mkdir()
    for name, content in gen_a.items():
        (dest / name).write_bytes(content)

    mixed: list[str] = []
    real_replace = os.replace

    def check_pair():
        try:
            origins = set(json.loads((dest / "origins.json").read_bytes()).keys())
            usage = (dest / "usage.jsonl").read_bytes()
        except (FileNotFoundError, ValueError):
            return
        for line in usage.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            if json.loads(line)["master_sha"] not in origins:
                mixed.append(line.decode())

    def spy_replace(src, dst):
        if Path(dst).name in ("origins.json", "usage.jsonl"):
            check_pair()
        return real_replace(src, dst)

    monkeypatch.setattr(sfp.os, "replace", spy_replace)

    pull_once(_make_fetch(gen_b), dest)

    assert mixed == [], f"reader saw a mixed generation: {mixed[:3]}"
    assert (dest / "origins.json").read_bytes() == gen_b["origins.json"]
    assert (dest / "usage.jsonl").read_bytes() == gen_b["usage.jsonl"]


def test_concurrent_read_during_replace_never_sees_bad_pair(tmp_path, monkeypatch):
    """A racing reader (origins.json then usage.jsonl) does not see NEW usage over
    OLD origins while the writer holds the intermediate state (origins NEW,
    usage OLD). A reader that stalls across BOTH renames can still pair them;
    that case is re-checked below and is not what this test pins.

    The writer holds the SAFE intermediate state open (origins already NEW,
    usage still OLD) and a reader hammers the pair. A transient stall of the
    reader across a full pull is re-checked (read origins again) so it cannot
    false-positive; a real ordering bug leaves the bad pair persistent for the
    whole hold and is caught.
    """
    dest = tmp_path / "o"
    gen_a = _feed_generation(["A"])
    gen_b = _feed_generation(["A", "B"])
    dest.mkdir()
    for name, content in gen_a.items():
        (dest / name).write_bytes(content)

    usage_replace = threading.Event()
    release = threading.Event()
    real_replace = os.replace

    def blocking_replace(src, dst):
        if str(src).endswith(".tmp") and Path(dst).name == "usage.jsonl":
            # origins.json was already replaced; hold disk at (NEW, OLD).
            usage_replace.set()
            release.wait(5)
        return real_replace(src, dst)

    monkeypatch.setattr(sfp.os, "replace", blocking_replace)

    stop = threading.Event()
    bad: list[str] = []
    saw_new_usage = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                origins = set(json.loads((dest / "origins.json").read_bytes()).keys())
                usage = (dest / "usage.jsonl").read_bytes()
            except (FileNotFoundError, ValueError):
                continue
            if usage == gen_b["usage.jsonl"]:
                saw_new_usage.set()
            missing = []
            for line in usage.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                if json.loads(line)["master_sha"] not in origins:
                    missing.append(line.decode())
            if missing:
                # Transient reader stall across a full pull resolves on re-read;
                # re-read origins once so only a persistent writer bug is bad.
                try:
                    origins = set(json.loads((dest / "origins.json").read_bytes()).keys())
                except (FileNotFoundError, ValueError):
                    origins = set()
                still_missing = [
                    line for line in missing
                    if json.loads(line)["master_sha"] not in origins
                ]
                if still_missing:
                    bad.extend(still_missing)

    thread = threading.Thread(target=reader)
    thread.start()
    pull_thread = None
    try:
        pull_thread = threading.Thread(target=lambda: pull_once(_make_fetch(gen_b), dest))
        pull_thread.start()
        assert usage_replace.wait(5), "writer never reached the usage replace"
        time.sleep(0.2)  # reader hammers the held (NEW, OLD) window
        release.set()
        pull_thread.join()
        assert saw_new_usage.is_set(), "reader never observed the new full usage file"
    finally:
        release.set()
        stop.set()
        thread.join()
        if pull_thread is not None:
            pull_thread.join()

    assert not bad, f"reader saw usage rows missing from origins: {bad[:3]}"
    assert (dest / "origins.json").read_bytes() == gen_b["origins.json"]
    assert (dest / "usage.jsonl").read_bytes() == gen_b["usage.jsonl"]


def test_replace_failure_rolls_back_old_files(tmp_path, monkeypatch):
    """A mid-replace OSError restores the old files and raises a named reason."""
    dest = tmp_path / "o"
    old = {"manifest.json": b"OLD-M\n", "usage.jsonl": b"OLD-U\n", "origins.json": b"OLD-O\n"}
    _seed_old_files(dest, old)

    usage = _usage_rows(3)
    origins = _origins()
    manifest = _manifest(usage, origins)

    real_replace = os.replace

    def flaky_replace(src, dst):
        # forward writes arrive as *.tmp; rollback restores arrive as *.bak
        if str(src).endswith(".tmp") and Path(dst).name == "manifest.json":
            raise OSError("injected manifest replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(sfp.os, "replace", flaky_replace)

    with pytest.raises(ScramblerFeedPullError) as exc:
        pull_once(_make_fetch({"manifest.json": manifest, "usage.jsonl": usage,
                               "origins.json": origins}), dest)

    assert exc.value.reason.startswith("write_failed")
    for name, content in old.items():
        assert (dest / name).read_bytes() == content, name
        assert (dest / name).stat().st_mtime == 1_600_000_000, name
    leftovers = [p.name for p in dest.iterdir() if ".tmp" in p.name or ".bak" in p.name]
    assert leftovers == [], f"temp/backup files leaked: {leftovers}"


def test_run_feed_pull_once_wires_r2_and_env(tmp_path, monkeypatch):
    """The production call site composes feed_config -> feed_key -> r2_fetch -> dest_dir."""
    monkeypatch.delenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", raising=False)
    monkeypatch.setenv("SCRAMBLER_FEED_R2_BUCKET", "bucket-x")
    monkeypatch.setenv("SCRAMBLER_FEED_R2_PREFIX", "prod/feed")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "vol"))

    usage = _usage_rows(3)
    origins = _origins()
    manifest = _manifest(usage, origins)
    objects = {
        "prod/feed/manifest.json": manifest,
        "prod/feed/usage.jsonl": usage,
        "prod/feed/origins.json": origins,
    }
    calls: list[tuple[str, str]] = []

    def fake_r2_fetch(bucket: str, key: str) -> bytes:
        calls.append((bucket, key))
        return objects[key]

    monkeypatch.setattr(sfp, "r2_fetch", fake_r2_fetch)

    result = sfp.run_feed_pull_once()

    assert calls == [
        ("bucket-x", "prod/feed/manifest.json"),
        ("bucket-x", "prod/feed/usage.jsonl"),
        ("bucket-x", "prod/feed/origins.json"),
    ]
    assert result.wrote == ("manifest.json", "origins.json", "usage.jsonl")
    dest = tmp_path / "vol" / "originality"
    for name, content in (("usage.jsonl", usage), ("origins.json", origins),
                          ("manifest.json", manifest)):
        assert (dest / name).read_bytes() == content


# ── env/config contract ──────────────────────────────────────────────────
def test_feed_config_and_key(monkeypatch):
    monkeypatch.delenv("SCRAMBLER_FEED_R2_BUCKET", raising=False)
    monkeypatch.delenv("SCRAMBLER_FEED_R2_PREFIX", raising=False)
    assert sfp.feed_config() is None

    monkeypatch.setenv("SCRAMBLER_FEED_R2_BUCKET", "mybucket")
    monkeypatch.setenv("SCRAMBLER_FEED_R2_PREFIX", "prod")
    assert sfp.feed_config() == ("mybucket", "prod")
    assert sfp.feed_key("prod", "usage.jsonl") == "prod/usage.jsonl"
    assert sfp.feed_key("", "usage.jsonl") == "originality/feed/usage.jsonl"
    assert sfp.feed_key("prod/", "usage.jsonl") == "prod/usage.jsonl"
    assert sfp.feed_key("originality/feed/", "usage.jsonl") == "originality/feed/usage.jsonl"


def test_dest_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    assert sfp.dest_dir() is None

    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", str(tmp_path / "x" / "usage.jsonl"))
    assert sfp.dest_dir() == tmp_path / "x"

    monkeypatch.delenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "vol"))
    assert sfp.dest_dir() == tmp_path / "vol" / "originality"


def test_manifest_invalid_reasons():
    with pytest.raises(ScramblerFeedPullError) as exc:
        sfp.parse_manifest(b"not json")
    assert exc.value.reason.startswith("manifest_invalid")

    with pytest.raises(ScramblerFeedPullError) as exc:
        sfp.parse_manifest(json.dumps({"rows": 1, "sha256s": {}}).encode())
    assert "generated_at" in exc.value.reason

    with pytest.raises(ScramblerFeedPullError) as exc:
        sfp.parse_manifest(json.dumps({"generated_at": "x", "sha256s": {}}).encode())
    assert "rows" in exc.value.reason
