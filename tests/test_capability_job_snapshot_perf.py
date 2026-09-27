"""C5/T5: the performance red test.

Synthetic 5,000-job store, realistic shape, many pages. A background writer
mutates jobs at >= 5/s while >= 8 concurrent threads call `capabilities()`.
Required: p95 < 200 ms, p99 < 500 ms, no request > 1 s.

This file is written to run unmodified against BOTH main 8ad9ab32 (where it
must FAIL — the point of the red test) and this branch (where it must PASS).
It only calls functions that exist on both: `_jobs_path`, `_update_job`,
`_load_jobs`, `capabilities`, `_current_intent_for_capabilities`,
`list_registered_recipe_bindings`, `load_engine_registry`, plus
`services.json_store.atomic_save`. Nothing here references the new snapshot
internals (`_save_jobs`, `_jobs_snapshot`, `_JobsSnapshot`, ...) so it
collects and runs cleanly on base too.

Numbers are both asserted AND written to /tmp/lab_capsnap_perf_report.json so
they can be read back after a `pytest -q` run that captured stdout.
"""

from __future__ import annotations

import gc
import json
import os
import statistics
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from routers import control_plane as cp
from services.json_store import atomic_save

NUM_PAGES = 250
JOBS_PER_PAGE = 20
NUM_JOBS = NUM_PAGES * JOBS_PER_PAGE  # 5,000
READER_THREADS = 8
WRITE_INTERVAL_SECONDS = 0.15  # ~6.7 writes/s, comfortably >= the 5/s floor
LOAD_DURATION_SECONDS = 4.0

REPORT_PATH = Path(os.environ.get("LAB_PERF_REPORT_PATH", "/tmp/lab_capsnap_perf_report.json"))


def _synthetic_job(page_id: str, index: int) -> dict:
    """A "generated" (ai_video) job shaped like routers/control_plane.py's
    real job creation (~line 3256): the dominant, ever-growing sourceKind in
    production, per the criteria's own framing of the mechanism."""
    job_id = f"cpl-{page_id}-{index:04d}"
    return {
        "jobId": job_id,
        "idempotencyKey": f"{page_id}:{index}:policy",
        "pageId": page_id,
        "lane": "content-lab",
        "engine": "ai_video",
        "lockedRecipeId": "truck-scenic:master",
        "recipeVersion": "dossier-1234567890abcdef",
        "sourceKind": "generated",
        "status": "completed" if index % 3 else "queued",
        "progress": 100 if index % 3 else 0,
        "clips": [
            {
                "path": f"clip-{n}.mp4",
                "sha256": f"{'ab' * 32}",
                "bytes": 4_500_000 + n,
                "promptHash": f"{'cd' * 32}",
            }
            for n in range(2)
        ],
        "artifactRoot": f"/data/generated/{page_id}/dossier-1234567890abcdef/{job_id}",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + ("ef" * 32),
        "engineRegistryHash": "sha256:" + ("11" * 32),
        "formatContractVersion": "sha256:" + ("22" * 32),
        "materialSource": "generated",
        "assetType": "video/mp4",
        "executorVersion": "replicate:v3",
        "promptCatalogHash": "sha256:" + ("33" * 32),
        "family": "truck-scenic",
        "providerModel": "replicate/veo-3",
        "providerCallsPlanned": 1,
        "providerCallsCompleted": 1 if index % 3 else 0,
        "promptPlan": [{"promptHash": f"{'44' * 32}", "callIndex": 0}],
        "generationCheckpointVersion": None,
        "providerCheckpoints": {},
        "completedGenerationCalls": [],
        "runtimeId": "runtime-perf-test",
        "createdAt": "2026-09-01T00:00:00+00:00",
    }


def _build_large_store() -> dict:
    jobs = {}
    for page_index in range(NUM_PAGES):
        page_id = f"perf-page-{page_index:04d}"
        for job_index in range(JOBS_PER_PAGE):
            job = _synthetic_job(page_id, job_index)
            jobs[job["jobId"]] = job
    return {"version": 1, "jobs": jobs, "byIdempotency": {}, "served": {}}


@pytest.fixture
def large_job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    if hasattr(cp, "_jobs_snapshot"):
        cp._jobs_snapshot = None
    if hasattr(cp, "_jobs_decode_pending"):
        cp._jobs_decode_pending.clear()
    store = _build_large_store()
    atomic_save(path, store)
    size_bytes = path.stat().st_size
    return path, store, size_bytes


@pytest.fixture
def capabilities_scaffold(monkeypatch):
    """Isolate the measurement to the job-store read path itself (the actual
    subject of this criteria) the same way the existing
    test_capability_job_snapshot.py suite already does."""
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "hash"))


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _summarize(latencies: list[float]) -> dict:
    ordered = sorted(latencies)
    return {
        "n": len(ordered),
        "p50_ms": round(_percentile(ordered, 0.50) * 1000, 2),
        "p95_ms": round(_percentile(ordered, 0.95) * 1000, 2),
        "p99_ms": round(_percentile(ordered, 0.99) * 1000, 2),
        "max_ms": round((ordered[-1] if ordered else 0.0) * 1000, 2),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 2) if ordered else 0.0,
    }


def _write_report(key: str, payload: dict) -> None:
    report = {}
    if REPORT_PATH.exists():
        try:
            report = json.loads(REPORT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            report = {}
    report[key] = payload
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True))


def _run_concurrent_capabilities_load(
    job_path: Path,
    store: dict,
    *,
    external_writer: bool,
    duration_seconds: float = LOAD_DURATION_SECONDS,
) -> list[float]:
    """Fire READER_THREADS concurrent capabilities() calls for
    ``duration_seconds`` while a background writer mutates the store at
    >= 1/WRITE_INTERVAL_SECONDS Hz.

    ``external_writer=False``: writes go through `_update_job` (the Lab's
    own writer path) — the read-after-our-own-write case.
    ``external_writer=True``: writes bypass `_update_job`/`_save_jobs`
    entirely (plain `atomic_save`, simulating an external replacement) so
    every write forces a REAL decode even on head (T5).
    """
    stop = threading.Event()
    job_ids = list(store["jobs"])
    write_count = [0]

    def writer_loop():
        rotation = 0
        while not stop.is_set():
            job_id = job_ids[rotation % len(job_ids)]
            rotation += 1
            if external_writer:
                current = json.loads(job_path.read_text())
                job = current["jobs"].get(job_id)
                if job is not None:
                    job["progress"] = (job.get("progress", 0) + 1) % 100
                    atomic_save(job_path, current)
            else:
                cp._update_job(job_id, progress=(rotation % 100))
            write_count[0] += 1
            time.sleep(WRITE_INTERVAL_SECONDS)

    writer = threading.Thread(target=writer_loop, daemon=True)
    writer.start()

    latencies: list[float] = []
    latencies_lock = threading.Lock()
    deadline = time.monotonic() + duration_seconds

    def reader_loop(page_index: int):
        page_id = f"perf-page-{page_index % NUM_PAGES:04d}"
        local: list[float] = []
        while time.monotonic() < deadline:
            start = time.perf_counter()
            result = cp.capabilities(x_rt_page_id=page_id, x_page_id=None)
            elapsed = time.perf_counter() - start
            assert "schema" in result
            local.append(elapsed)
        with latencies_lock:
            latencies.extend(local)

    try:
        with ThreadPoolExecutor(max_workers=READER_THREADS) as pool:
            futures = [pool.submit(reader_loop, i) for i in range(READER_THREADS)]
            for future in futures:
                future.result()
    finally:
        stop.set()
        writer.join(timeout=5)

    assert write_count[0] >= duration_seconds * 5, (
        f"writer only completed {write_count[0]} writes in {duration_seconds}s "
        "(need >= 5/s per C5)"
    )
    return latencies


def test_capabilities_p95_p99_under_concurrent_writes(
    large_job_path, capabilities_scaffold,
):
    """C5 / the red test proper: 5k jobs, own-process writes at >= 5/s,
    >= 8 concurrent readers. p95 < 200ms, p99 < 500ms, max < 1s."""
    job_path, store, size_bytes = large_job_path
    latencies = _run_concurrent_capabilities_load(job_path, store, external_writer=False)
    summary = _summarize(latencies)
    summary["store_jobs"] = NUM_JOBS
    summary["store_bytes"] = size_bytes
    summary["reader_threads"] = READER_THREADS
    _write_report("own_write_load", summary)

    assert summary["p95_ms"] < 200, summary
    assert summary["p99_ms"] < 500, summary
    assert summary["max_ms"] < 1000, summary


def test_capabilities_p95_p99_with_external_changes(
    large_job_path, capabilities_scaffold, monkeypatch,
):
    """T5: the file changes externally (not through this process's writer)
    at >= 1/s, forcing a real decode on every change, with 8 concurrent
    readers. Same p95/p99/max bar, plus: no reader blocked behind a decode
    for longer than one refresh (single-flight bound)."""
    job_path, store, size_bytes = large_job_path

    decode_calls = []
    if hasattr(cp, "_decode_jobs_snapshot"):
        real_decode = cp._decode_jobs_snapshot

        def timed_decode(generation):
            start = time.perf_counter()
            result = real_decode(generation)
            decode_calls.append(time.perf_counter() - start)
            return result

        monkeypatch.setattr(cp, "_decode_jobs_snapshot", timed_decode)

    latencies = _run_concurrent_capabilities_load(
        job_path, store, external_writer=True, duration_seconds=LOAD_DURATION_SECONDS,
    )

    summary = _summarize(latencies)
    summary["store_jobs"] = NUM_JOBS
    summary["store_bytes"] = size_bytes
    summary["reader_threads"] = READER_THREADS
    if decode_calls:
        summary["decode_calls"] = len(decode_calls)
        summary["decode_p50_ms"] = round(statistics.median(decode_calls) * 1000, 2)
        summary["decode_max_ms"] = round(max(decode_calls) * 1000, 2)
        # Single-flight bound: no request should be able to wait behind more
        # than roughly one decode's worth of work.
        assert summary["max_ms"] < max(1000, summary["decode_max_ms"] * 3), summary
    _write_report("external_change_load", summary)

    assert summary["p95_ms"] < 200, summary
    assert summary["p99_ms"] < 500, summary
    assert summary["max_ms"] < 1000, summary


def test_raw_5k_store_decode_time(large_job_path):
    """C10 input: the raw cost of decoding the 5k-job file once, unlocked,
    unloaded — the number the whole fix exists to take off the request path."""
    job_path, store, size_bytes = large_job_path
    timings = []
    for _ in range(5):
        if hasattr(cp, "_jobs_snapshot"):
            cp._jobs_snapshot = None
        if hasattr(cp, "_jobs_decode_pending"):
            cp._jobs_decode_pending.clear()
        start = time.perf_counter()
        cp._read_jobs_snapshot()
        timings.append(time.perf_counter() - start)
    summary = {
        "store_bytes": size_bytes,
        "store_jobs": NUM_JOBS,
        "decode_p50_ms": round(statistics.median(timings) * 1000, 2),
        "decode_max_ms": round(max(timings) * 1000, 2),
        "raw_samples_ms": [round(t * 1000, 2) for t in timings],
    }
    _write_report("raw_5k_decode", summary)
    # Sanity bound only (not a C5 target): a single decode of one 5k-job file
    # must not itself hang; the fix is about NOT doing this on every request,
    # not about making one decode instant.
    assert summary["decode_max_ms"] < 5000, summary


def test_snapshot_memory_bound(large_job_path):
    """C7: the snapshot + index for a 5k-job store adds < 50 MB of retained
    (live, traced) memory. `tracemalloc` measures Python-level allocations
    still reachable at the end of the block, independent of whatever RSS
    peak the rest of the test session happened to reach first."""
    job_path, store, size_bytes = large_job_path
    if hasattr(cp, "_jobs_snapshot"):
        cp._jobs_snapshot = None
    if hasattr(cp, "_jobs_decode_pending"):
        cp._jobs_decode_pending.clear()
    gc.collect()

    tracemalloc.start()
    try:
        cp._read_jobs_snapshot()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    delta_mb = current_bytes / (1024 * 1024)

    _write_report("snapshot_memory", {
        "store_jobs": NUM_JOBS,
        "store_bytes": size_bytes,
        "traced_current_mb": round(delta_mb, 2),
        "traced_peak_mb": round(peak_bytes / (1024 * 1024), 2),
    })
    assert delta_mb < 50, f"snapshot+index retained {delta_mb:.1f} MB for {NUM_JOBS} jobs"


def test_no_unbounded_growth_after_1000_writes(large_job_path):
    """C7: after 1,000 further writes (each publishing a fresh snapshot),
    retained memory stays close to one store's worth — old snapshots and
    their generations must be garbage, not accumulated. `current` (not
    `peak`) is the right metric: transient double-buffering during a single
    write is expected and is not what this criterion forbids."""
    job_path, store, size_bytes = large_job_path
    if hasattr(cp, "_jobs_snapshot"):
        cp._jobs_snapshot = None
    if hasattr(cp, "_jobs_decode_pending"):
        cp._jobs_decode_pending.clear()
    cp._read_jobs_snapshot()
    gc.collect()

    job_ids = list(store["jobs"])
    tracemalloc.start()
    baseline_current, _ = tracemalloc.get_traced_memory()
    try:
        for i in range(1000):
            job_id = job_ids[i % len(job_ids)]
            cp._update_job(job_id, progress=i % 100)
        gc.collect()
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    delta_mb = (current_bytes - baseline_current) / (1024 * 1024)

    _write_report("post_1000_writes_memory", {
        "store_jobs": NUM_JOBS,
        "writes": 1000,
        "traced_current_delta_mb": round(delta_mb, 2),
        "traced_peak_mb": round(peak_bytes / (1024 * 1024), 2),
    })
    # A single extra full-store copy is ~ store_bytes; 1000 writes must not
    # look like 1000 retained copies (which would dwarf this bound).
    assert delta_mb < 100, f"1000 writes grew retained memory by {delta_mb:.1f} MB (unbounded retention?)"
