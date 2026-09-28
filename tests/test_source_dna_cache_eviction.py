"""Closed contracts for the persistent source-master cache byte budget (lab #5)."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import routers.control_plane as cp
from services.source_dna_registry import source_dna_master_hashes


def _write(path: Path, size: int, mtime_ns: int) -> None:
    path.write_bytes(b"\0" * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_source_dna_cache_evicts_oldest_unreferenced_above_budget(tmp_path, monkeypatch):
    cache = tmp_path / "_source_dna"
    cache.mkdir()
    monkeypatch.setenv("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", "200")
    old = cache / f"{'a' * 64}.mp4"
    mid = cache / f"{'b' * 64}.mp4"
    new = cache / f"{'c' * 64}.mp4"
    _write(old, 100, mtime_ns=1000)
    _write(mid, 100, mtime_ns=2000)
    _write(new, 100, mtime_ns=3000)

    cp._evict_source_dna_cache(cache, pinned=set())

    assert not old.exists()          # oldest, unreferenced -> evicted
    assert mid.exists() and new.exists()  # brought total back under budget


def test_source_dna_cache_preserves_pinned_files_even_when_over_budget(tmp_path, monkeypatch):
    cache = tmp_path / "_source_dna"
    cache.mkdir()
    monkeypatch.setenv("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", "100")
    old = cache / f"{'a' * 64}.mp4"
    pinned = cache / f"{'b' * 64}.mp4"
    _write(old, 100, mtime_ns=1000)
    _write(pinned, 100, mtime_ns=2000)

    cp._evict_source_dna_cache(cache, pinned={pinned.name[:-4]})

    assert not old.exists()
    assert pinned.exists()


def test_cache_failed_unlink_does_not_reduce_accounted_bytes(tmp_path, monkeypatch):
    cache = tmp_path / "_source_dna"
    cache.mkdir()
    monkeypatch.setenv("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", "100")
    old = cache / f"{'a' * 64}.mp4"
    mid = cache / f"{'b' * 64}.mp4"
    new = cache / f"{'c' * 64}.mp4"
    _write(old, 100, mtime_ns=1000)
    _write(mid, 100, mtime_ns=2000)
    _write(new, 100, mtime_ns=3000)
    real_unlink = Path.unlink

    def fail_oldest(path, *args, **kwargs):
        if path == old:
            raise OSError("simulated busy cache file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_oldest)
    cp._evict_source_dna_cache(cache, pinned=set())

    assert old.exists()
    assert sum(path.stat().st_size for path in cache.glob("*.mp4")) <= 100


@pytest.mark.asyncio
async def test_cache_evicts_obsolete_bytes_before_opening_new_partial(tmp_path, monkeypatch):
    content = b"new master bytes"
    digest = hashlib.sha256(content).hexdigest()
    cache = tmp_path / "_source_dna"
    cache.mkdir()
    obsolete = cache / f"{'a' * 64}.mp4"
    _write(obsolete, len(content), mtime_ns=1000)
    monkeypatch.setenv("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", str(len(content)))
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path)
    monkeypatch.setattr(cp, "_source_media_origin", lambda: "https://source.example")
    monkeypatch.setattr(cp, "_source_dna_active_master_hashes", lambda: set())
    monkeypatch.setattr(cp, "source_dna_master_hashes", lambda: set())

    real_open = Path.open

    def fail_if_volume_still_full(path, *args, **kwargs):
        if path.suffix == ".part" and obsolete.exists():
            raise OSError("simulated full volume")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_if_volume_still_full)
    original_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-length": str(len(content))},
                content=content,
            )
        )
        return original_client(**kwargs)

    monkeypatch.setattr(cp.httpx, "AsyncClient", client)
    master = SimpleNamespace(sha256=digest, bytes=len(content))

    target = await cp._cached_source_master("page-a", master, "job-a")

    assert target.read_bytes() == content
    assert not obsolete.exists()


@pytest.mark.asyncio
async def test_cache_hit_sweeps_obsolete_bytes_before_return(tmp_path, monkeypatch):
    content = b"resident master"
    digest = hashlib.sha256(content).hexdigest()
    cache = tmp_path / "_source_dna"
    cache.mkdir()
    obsolete = cache / f"{'a' * 64}.mp4"
    target = cache / f"{digest}.mp4"
    _write(obsolete, len(content), mtime_ns=1000)
    target.write_bytes(content)
    monkeypatch.setenv("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", str(len(content)))
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path)
    monkeypatch.setattr(cp, "_source_dna_active_master_hashes", lambda: set())
    monkeypatch.setattr(cp, "source_dna_master_hashes", lambda: set())
    master = SimpleNamespace(sha256=digest, bytes=len(content))

    resolved = await cp._cached_source_master("page-a", master, "job-a")

    assert resolved == target
    assert target.exists()
    assert not obsolete.exists()


def test_source_dna_active_master_hashes_pins_queued_and_running_sources(monkeypatch):
    monkeypatch.setattr(cp, "_load_jobs", lambda: {
        "jobs": {
            "active": {
                "sourceKind": "dossier_source_dna",
                "status": "running",
                "sourceCuts": [{"masterSha256": "a" * 64}, {"masterSha256": "b" * 64}],
            },
            "completed": {
                "sourceKind": "dossier_source_dna",
                "status": "completed",
                "sourceCuts": [{"masterSha256": "c" * 64}],
            },
            "generated": {
                "sourceKind": "generated",
                "status": "running",
                "sourceCuts": [{"masterSha256": "d" * 64}],
            },
        },
    })
    assert cp._source_dna_active_master_hashes() == {"a" * 64, "b" * 64}


def test_source_dna_master_hashes_returns_registered_manifest_masters():
    hashes = source_dna_master_hashes()
    assert hashes
    assert all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in hashes)
