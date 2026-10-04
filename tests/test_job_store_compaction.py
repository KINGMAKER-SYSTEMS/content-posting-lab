"""Job-store compaction: archives old terminal jobs and keeps every live path intact.

These tests exercise the pure transform in services/job_store_compaction and
the reader merges + IO glue in routers/control_plane against a monkeypatched
``_jobs_path``. They are the K7 red suite: each reader's answer is proven
unchanged after compaction, crash-mid-compaction recovery is proven, the
archive is idempotent, the tmp sweep never touches the live write, compaction
is concurrent-safe with writes, no live job is dropped, and the no-repeat
index is mutation-proven.
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routers import control_plane as cp
from services import job_store_compaction as c
from services.control_plane_sources import canonical_source_identity
from services.source_treatment import source_treatment_receipt
from services.json_store import atomic_save, atomic_load

SHA = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64
LIB_HASH = "L" * 64


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
    return path


def _now():
    return datetime.now(timezone.utc)


def _old():
    return (_now() - timedelta(days=40)).isoformat()


def _recent():
    return (_now() - timedelta(days=2)).isoformat()


def _empty():
    return {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}


def _write(path, store):
    atomic_save(path, store)
    cp._jobs_snapshot = None


# ── K2: what gets archived ────────────────────────────────────────────────

def test_compact_archives_only_terminal_old_jobs():
    store = _empty()
    old = _old()
    recent = _recent()
    store["jobs"]["old-completed"] = {"jobId": "old-completed", "status": "completed", "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    store["jobs"]["old-failed"] = {"jobId": "old-failed", "status": "failed", "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    store["jobs"]["old-cancelled"] = {"jobId": "old-cancelled", "status": "cancelled", "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    store["jobs"]["recent-completed"] = {"jobId": "recent-completed", "status": "completed", "createdAt": recent, "completedAt": recent, "pageId": "acct:p"}
    store["jobs"]["active"] = {"jobId": "active", "status": "queued", "createdAt": old, "pageId": "acct:p"}
    store["byIdempotency"]["k-old"] = "old-completed"
    store["byIdempotency"]["k-recent"] = "recent-completed"
    store["byIdempotency"]["k-active"] = "active"

    new_store, archived = c.compact_job_store(store, _now())
    archived_ids = {j["jobId"] for j in archived}
    assert archived_ids == {"old-completed", "old-failed", "old-cancelled"}
    assert set(new_store["jobs"]) == {"recent-completed", "active"}
    assert "old-completed" not in new_store["byIdempotency"]
    assert new_store["byIdempotency"]["k-old"] == "old-completed"
    assert new_store["byIdempotency"]["k-recent"] == "recent-completed"
    assert new_store["byIdempotency"]["k-active"] == "active"


def test_compaction_is_incremental_and_bounded():
    store = _empty()
    old = _old()
    for i in range(10):
        store["jobs"][f"j{i}"] = {"jobId": f"j{i}", "status": "completed", "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    new_store, archived = c.compact_job_store(store, _now(), batch_size=4)
    assert len(archived) == 4
    assert len(new_store["jobs"]) == 6  # 4 moved, 6 remain for the next batch


# ── K1 reader: sourceIdentities ───────────────────────────────────────────

def test_capabilities_source_identities_unchanged_after_compaction(job_path, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities",
                        lambda _pid: ({"contentNiche": "niche", "contentEngine": "sourced_video"}, "h"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [])
    profile = SimpleNamespace(format_slug="x:master", content_niche="niche",
                              content_engine="sourced_video", execution_status="commissioned",
                              format_contract_version="sha256:" + "a" * 64, max_quantity=10)
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({"p": profile}, "rh"))

    store = _empty()
    old = _old()
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    identity = canonical_source_identity(url)
    store["jobs"]["imp-old"] = {"jobId": "imp-old", "pageId": "acct:p", "sourceKind": "page_source_import",
                                "status": "completed", "sourceUrl": url, "createdAt": old, "completedAt": old}
    _write(job_path, store)
    before = cp.capabilities(x_rt_page_id="acct:p", x_page_id=None)
    assert before["capabilities"][0]["sourceIdentities"] == [identity]

    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["imp-old"]
    _write(job_path, new_store)
    after = cp.capabilities(x_rt_page_id="acct:p", x_page_id=None)
    assert after == before


# ── K1 reader: sourced-window reservations ────────────────────────────────

def test_source_dna_unavailable_slots_unchanged_after_compaction():
    store = _empty()
    old, recent = _old(), _recent()
    store["jobs"]["src-old"] = {"jobId": "src-old", "pageId": "acct:p", "sourceKind": "dossier_source_dna",
                                "status": "completed", "sourceLibraryId": "lib", "sourceLibraryHash": LIB_HASH,
                                "recipeVersion": "rv1", "createdAt": old, "completedAt": old,
                                "sourceCuts": [{"slotId": "s1", "masterSha256": SHA, "startMs": 0, "durationMs": 5000}]}
    store["jobs"]["src-recent"] = {"jobId": "src-recent", "pageId": "acct:p", "sourceKind": "dossier_source_dna",
                                   "status": "completed", "sourceLibraryId": "lib", "sourceLibraryHash": LIB_HASH,
                                   "recipeVersion": "rv1", "createdAt": recent, "completedAt": recent,
                                   "sourceCuts": [{"slotId": "s2", "masterSha256": SHA2, "startMs": 5000, "durationMs": 5000}]}
    recipe = SimpleNamespace(masters=[SimpleNamespace(sha256=SHA), SimpleNamespace(sha256=SHA2)],
                             source_library_id="lib", source_library_hash=LIB_HASH)
    before = cp._source_dna_unavailable_slots(store, recipe, "rv1")
    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["src-old"]
    after = cp._source_dna_unavailable_slots(new_store, recipe, "rv1")
    assert before == after
    # the archived cut's exact time frame is still reserved
    assert f"{SHA}:0:5000" in after
    assert "s1" in after


# ── K1 reader: slideshow reservations ─────────────────────────────────────

def test_slideshow_unavailable_signatures_unchanged_after_compaction():
    store = _empty()
    old, recent = _old(), _recent()
    sig = "f" * 64
    sig2 = "e" * 64
    store["jobs"]["slide-old"] = {"jobId": "slide-old", "pageId": "acct:p", "sourceKind": "syzygy_slideshow",
                                  "status": "completed", "sourceLibraryId": "lib", "createdAt": old, "completedAt": old,
                                  "slideshowPlan": [{"signature": sig}]}
    store["jobs"]["slide-recent"] = {"jobId": "slide-recent", "pageId": "acct:p", "sourceKind": "syzygy_slideshow",
                                     "status": "completed", "sourceLibraryId": "lib", "createdAt": recent, "completedAt": recent,
                                     "slideshowPlan": [{"signature": sig2}]}
    recipe = SimpleNamespace(library_id="lib")
    before = cp._slideshow_unavailable_signatures(store, recipe, "acct:p")
    new_store, archived = c.compact_job_store(store, _now())
    after = cp._slideshow_unavailable_signatures(new_store, recipe, "acct:p")
    assert before == after == {sig, sig2}


# ── K1 reader: truck re-crop candidates (full path) ───────────────────────

def _truck_job(job_id, sha, page_id, root, *, created):
    treatment = {"filters": {"brightness": 1.0}, "clipSpeed": 1.0,
                 "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}
    recipe_spec_hash = "sha256:" + "d" * 64
    job = {
        "jobId": job_id, "pageId": page_id, "sourceKind": "generated", "status": "completed",
        "engine": "hailuo", "recipeId": "truck-scenic:master",
        "engineRegistryHash": "e" * 64, "formatContractVersion": "sha256:" + "a" * 64,
        "engineProfileHash": "sha256:" + "b" * 64,
        "executorVersion": "v1", "promptCatalogHash": "p" * 64, "providerModel": "minimax/hailuo-2.3",
        "family": "truck", "materialSource": "generated_video", "assetType": "video/mp4",
        "artifactRoot": str(root), "createdAt": created, "completedAt": created,
        "recipeSpecHash": recipe_spec_hash,
    }
    receipt = source_treatment_receipt(job, treatment, sha, clip_speed=1.0, clip_crop=treatment["clipCrop"])
    job["clips"] = [{
        "path": f"clip-{sha[:8]}.mp4", "sha256": sha, "bytes": 4,
        "source": {"pageId": page_id, "recipeId": "truck-scenic:master",
                   "contentNiche": "TRUCK", "contentEngine": "hailuo"},
        "sourceTreatment": receipt,
    }]
    return job


def test_truck_master_candidates_unchanged_after_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda p: True)
    root = tmp_path / "gen"
    root.mkdir()
    # The clip path encodes sha[:8]; create the exact files the manifests claim.
    (root / f"clip-{SHA[:8]}.mp4").write_bytes(b"1234")
    (root / f"clip-{SHA2[:8]}.mp4").write_bytes(b"1234")

    store = _empty()
    old, recent = _old(), _recent()
    store["jobs"]["truck-old"] = _truck_job("truck-old", SHA, "acct:p", root, created=old)
    store["jobs"]["truck-recent"] = _truck_job("truck-recent", SHA2, "acct:p", root, created=recent)
    store["jobs"]["recovery-old"] = {"jobId": "recovery-old", "pageId": "acct:p",
                                     "sourceKind": "truck_master_recovery", "status": "completed",
                                     "createdAt": old, "completedAt": old,
                                     "recoveryMasters": [{"sha256": SHA3}]}

    recipe = SimpleNamespace(
        engine="hailuo", recipe_id="truck-scenic:master",
        engine_registry_hash="e" * 64, format_contract_version="sha256:" + "a" * 64,
        engine_profile_hash="sha256:" + "b" * 64,
        executor_version="v1", prompt_catalog_hash="p" * 64,
        family_name="truck", material_source="generated_video", asset_type="video/mp4",
        provider_model="minimax/hailuo-2.3",
        recipe_spec={"renderTreatment": {"filters": {"brightness": 1.0}, "clipSpeed": 1.0,
                                          "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}},
        clips_per_generation=5,
    )

    before = cp._truck_master_candidates(
        store, "acct:p", 10, content_engine="hailuo", recipe_id="truck-scenic:master",
        generation_recipe=recipe,
    )
    new_store, archived = c.compact_job_store(store, _now())
    assert {j["jobId"] for j in archived} == {"truck-old", "recovery-old"}
    archived_truck = next(j for j in new_store[c.ARCHIVE_INDEX_KEY]["truckCandidateJobs"]
                          if j["jobId"] == "truck-old")
    assert archived_truck["engineProfileHash"] == "sha256:" + "b" * 64
    assert archived_truck["materialSource"] == "generated_video"
    assert archived_truck["assetType"] == "video/mp4"
    after = cp._truck_master_candidates(
        new_store, "acct:p", 10, content_engine="hailuo", recipe_id="truck-scenic:master",
        generation_recipe=recipe,
    )
    assert [x["sha256"] for x in before] == [x["sha256"] for x in after]
    assert {x["sha256"] for x in after} == {SHA, SHA2}
    # the archived recovery's master remains reserved (SHA3 not produced here,
    # but it must be in the reserved set -> no candidate with that sha)


# ── K1 reader: same-asset dedupe / no-repeat ──────────────────────────────

def test_generated_dedupe_unchanged_after_compaction(job_path):
    store = _empty()
    old = _old()
    store["jobs"]["old-gen"] = {"jobId": "old-gen", "pageId": "acct:p", "sourceKind": "generated",
                                "status": "completed", "createdAt": old, "completedAt": old,
                                "clips": [{"sha256": SHA}]}
    store["jobs"]["active"] = {"jobId": "active", "pageId": "acct:p", "sourceKind": "generated",
                               "status": "queued", "createdAt": _recent()}
    new_store, _ = c.compact_job_store(store, _now())
    _write(job_path, new_store)

    manifest = {"sha256": SHA, "promptHash": SHA2, "generationIndex": 0}
    with pytest.raises(RuntimeError, match="duplicate_generated_artifact"):
        cp._claim_unique_generated_clip("active", manifest)


def test_failed_clip_reader_changes_after_compaction(job_path):
    store = _empty()
    store["jobs"]["old-failed"] = {
        "jobId": "old-failed", "pageId": "acct:p", "sourceKind": "generated",
        "status": "failed", "createdAt": _old(), "completedAt": _old(),
        "clips": [{"sha256": SHA}],
    }
    store["jobs"]["active"] = {
        "jobId": "active", "pageId": "acct:p", "sourceKind": "generated",
        "status": "queued", "createdAt": _recent(), "clips": [],
    }
    manifest = {"sha256": SHA, "promptHash": SHA2, "generationIndex": 0}

    # A failed job releases its bytes while it remains in the live store.
    _write(job_path, store)
    cp._claim_unique_generated_clip("active", dict(manifest))

    # Archiving that failed job must not widen the live reader's predicate.
    new_store, archived = c.compact_job_store(store, _now())
    assert [job["jobId"] for job in archived] == ["old-failed"]
    _write(job_path, new_store)
    cp._claim_unique_generated_clip("active", dict(manifest))


def test_no_repeat_index_mutation_red(job_path):
    # The index is what prevents replaying an archived clip. Drop it and the
    # exact same bytes are admitted again (the mutation red).
    store = _empty()
    old = _old()
    store["jobs"]["old-gen"] = {"jobId": "old-gen", "pageId": "acct:p", "sourceKind": "generated",
                                "status": "completed", "createdAt": old, "completedAt": old,
                                "clips": [{"sha256": SHA}]}
    store["jobs"]["active"] = {"jobId": "active", "pageId": "acct:p", "sourceKind": "generated",
                               "status": "queued", "createdAt": _recent()}
    new_store, _ = c.compact_job_store(store, _now())
    assert SHA in (new_store["archiveIndex"]["usedClipSha256"].get("acct:p") or [])
    # Mutation: drop the usedClipSha256 fact.
    new_store["archiveIndex"]["usedClipSha256"] = {}
    _write(job_path, new_store)
    manifest = {"sha256": SHA, "promptHash": SHA2, "generationIndex": 0}
    cp._claim_unique_generated_clip("active", manifest)  # no raise -> repeat allowed


def test_slideshow_dedupe_unchanged_after_compaction(job_path):
    store = _empty()
    old = _old()
    sig = "f" * 64
    store["jobs"]["old-slide"] = {"jobId": "old-slide", "pageId": "acct:p", "sourceKind": "syzygy_slideshow",
                                  "status": "completed", "createdAt": old, "completedAt": old,
                                  "slideshowPlan": [{"signature": sig}],
                                  "clips": [{"sha256": SHA, "source": {"planSignature": sig}}]}
    store["jobs"]["active"] = {"jobId": "active", "pageId": "acct:p", "sourceKind": "syzygy_slideshow",
                               "status": "queued", "createdAt": _recent()}
    new_store, _ = c.compact_job_store(store, _now())
    _write(job_path, new_store)

    manifest = {"sha256": SHA, "promptHash": SHA2, "generationIndex": 0}
    with pytest.raises(RuntimeError, match="duplicate_slideshow_artifact"):
        cp._claim_unique_slideshow_clip("active", manifest, sig)
    with pytest.raises(RuntimeError, match="duplicate_slideshow_plan"):
        cp._claim_unique_slideshow_clip("active", {"sha256": SHA3, "promptHash": SHA2, "generationIndex": 0}, sig)


# ── K3: provider self-report carry-forward ────────────────────────────────

def _attempt(call_key, outcome, klass, at):
    return {"attempt": 0, "at": at, "outcome": outcome, "class": klass,
            "promptHash": SHA, "promptVariant": None}


def test_provider_health_carried_forward_matches_from_scratch():
    store = _empty()
    old = _old()
    recent = _recent()
    t1 = (_now() - timedelta(days=45)).isoformat()
    t2 = (_now() - timedelta(days=44)).isoformat()
    t3 = (_now() - timedelta(days=1)).isoformat()
    old_job = {"jobId": "old-j", "pageId": "acct:p", "sourceKind": "generated",
               "engine": "hailuo", "status": "completed", "createdAt": old, "completedAt": old,
               "generationAttempts": {"0": [_attempt("0", "refused", "insufficient_credit", t1),
                                            _attempt("0", "succeeded", None, t2)]}}
    recent_job = {"jobId": "recent-j", "pageId": "acct:p", "sourceKind": "generated",
                  "engine": "hailuo", "status": "completed", "createdAt": recent, "completedAt": recent,
                  "generationAttempts": {"0": [_attempt("0", "refused", "insufficient_credit", t3)]}}
    store["jobs"]["old-j"] = old_job
    store["jobs"]["recent-j"] = recent_job

    from_scratch = c.provider_health_from_records(c.provider_records_from_jobs([old_job, recent_job]))

    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["old-j"]
    carried = new_store["archiveIndex"]["providerHealth"]
    merged = c.provider_health_merge(carried, c.provider_records_from_jobs(new_store["jobs"].values()))
    assert merged == from_scratch
    # The old credit failure still shortens nothing: streak counts the fresh 402.
    assert merged["replicate"]["consecutive_credit_failures"] == 1
    assert merged["replicate"]["last_success_at"] == t2


# ── K4: atomic / concurrent-safe / crash-safe ─────────────────────────────

def _old_completed(job_id):
    return {
        "jobId": job_id,
        "status": "completed",
        "createdAt": _old(),
        "completedAt": _old(),
        "pageId": "acct:p",
    }


def test_version_mismatch_silently_erases_prior_archive_facts(
    job_path, caplog,
):
    store = _empty()
    store["archiveIndex"] = {
        **c.empty_index(),
        "version": 999,
        "usedClipSha256": {"acct:prior": [SHA]},
    }
    store["jobs"]["new-old"] = _old_completed("new-old")
    _write(job_path, store)
    before = job_path.read_bytes()

    with caplog.at_level(logging.ERROR, logger="control_plane"):
        with pytest.raises(RuntimeError, match="index_unreadable"):
            cp.run_compaction_once(job_path, _now())

    assert job_path.read_bytes() == before
    assert not c.archive_path_for(job_path, _now()).exists()
    assert "ALERT-LABJOBSTORE index_unreadable" in caplog.text
    assert "version=999" in caplog.text


def test_non_dict_archive_index_refuses_compaction(job_path, caplog):
    store = _empty()
    store["archiveIndex"] = ["corrupt"]
    store["jobs"]["new-old"] = _old_completed("new-old")
    _write(job_path, store)
    before = job_path.read_bytes()

    with caplog.at_level(logging.ERROR, logger="control_plane"):
        with pytest.raises(RuntimeError, match="index_unreadable"):
            cp.run_compaction_once(job_path, _now())

    assert job_path.read_bytes() == before
    assert not c.archive_path_for(job_path, _now()).exists()
    assert "ALERT-LABJOBSTORE index_unreadable" in caplog.text
    assert "not_a_dict" in caplog.text


def test_absent_archive_index_with_archive_files_refuses(job_path, caplog):
    store = _empty()
    store["jobs"]["new-old"] = _old_completed("new-old")
    _write(job_path, store)
    archive = c.archive_path_for(job_path, _now())
    archive.write_text(json.dumps(_old_completed("prior-old")) + "\n", encoding="utf-8")
    store_before = job_path.read_bytes()
    archive_before = archive.read_bytes()

    with caplog.at_level(logging.ERROR, logger="control_plane"):
        with pytest.raises(RuntimeError, match="index_unreadable"):
            cp.run_compaction_once(job_path, _now())

    assert job_path.read_bytes() == store_before
    assert archive.read_bytes() == archive_before
    assert "ALERT-LABJOBSTORE index_unreadable" in caplog.text
    assert "missing_with_archives" in caplog.text


def test_absent_archive_index_without_archive_files_proceeds(job_path):
    store = _empty()
    store["jobs"]["first-old"] = _old_completed("first-old")
    _write(job_path, store)

    result = cp.run_compaction_once(job_path, _now())

    assert result["archived"] == 1
    assert cp._load_jobs()["archiveIndex"] == c.empty_index()
    assert c.archive_path_for(job_path, _now()).exists()


def test_partial_archive_tail_then_retry_loses_full_archive_record(job_path):
    store = _empty()
    store["archiveIndex"] = c.empty_index()
    record = {**_old_completed("retry-old"), "auditPayload": "must-survive"}
    store["jobs"][record["jobId"]] = record
    _write(job_path, store)
    archive = c.archive_path_for(job_path, _now())
    archive.write_bytes(
        (json.dumps(_old_completed("prior-old")) + "\n").encode()
        + b'{"jobId":"retry-old","auditPayload":"torn'
    )

    cp.run_compaction_once(job_path, _now())

    parsed = [json.loads(line) for line in archive.read_text().splitlines() if line]
    by_id = {row["jobId"]: row for row in parsed}
    assert set(by_id) == {"prior-old", "retry-old"}
    assert by_id["retry-old"]["auditPayload"] == "must-survive"


def test_crash_mid_compaction_archive_idempotent_and_store_valid(job_path):
    store = _empty()
    old = _old()
    for i in range(3):
        store["jobs"][f"j{i}"] = {"jobId": f"j{i}", "status": "completed",
                                  "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    _write(job_path, store)
    path = cp._jobs_path()
    now = _now()

    # Simulate a crash AFTER the archive append but BEFORE the store commit:
    # append the records to the archive, then leave the store as-is.
    new_store, archived = c.compact_job_store(store, now)
    written = c.append_archive_records(path, archived, now)
    assert written == 3
    # store NOT committed yet -> still the old store, still valid
    assert set(cp._load_jobs()["jobs"]) == {"j0", "j1", "j2"}

    # Retry the whole compaction: it must be idempotent and now commit.
    retry_store, retry_archived = c.compact_job_store(cp._load_jobs(), now)
    written_again = c.append_archive_records(path, retry_archived, now)
    assert written_again == 0  # nothing new to append
    atomic_save(path, retry_store)
    assert cp._load_jobs()["jobs"] == {}
    # Archive has exactly 3 lines, no duplicates.
    lines = [json.loads(line) for line in c.archive_path_for(path, now).read_text().splitlines() if line.strip()]
    ids = [rec["jobId"] for rec in lines]
    assert ids == ["j0", "j1", "j2"]


def test_archive_idempotent_no_duplicates(tmp_path):
    path = tmp_path / "jobs.json"
    recs = [{"jobId": f"j{i}", "status": "completed"} for i in range(3)]
    assert c.append_archive_records(path, recs, _now()) == 3
    assert c.append_archive_records(path, recs, _now()) == 0
    assert c.append_archive_records(path, recs[:1], _now()) == 0
    assert len([line for line in c.archive_path_for(path, _now()).read_text().splitlines() if line.strip()]) == 3


def test_tmp_sweep_never_touches_live_tmp(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{}", encoding="utf-8")
    live_tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    live_tmp.write_text("live", encoding="utf-8")
    stale_tmp = path.with_suffix(f"{path.suffix}.99999.12345.tmp")
    stale_tmp.write_text("stale", encoding="utf-8")
    old = time.time() - 7200
    os.utime(live_tmp, (old, old))
    os.utime(stale_tmp, (old, old))

    removed = cp._sweep_stale_compaction_tmps(path)
    assert removed == 1
    assert live_tmp.exists()          # live writer's own tmp never touched
    assert not stale_tmp.exists()     # stale tmp swept


def test_compaction_concurrent_with_writes(job_path):
    store = _empty()
    old = _old()
    for i in range(200):
        store["jobs"][f"old{i}"] = {"jobId": f"old{i}", "status": "completed",
                                    "createdAt": old, "completedAt": old, "pageId": "acct:p"}
    store["jobs"]["active"] = {"jobId": "active", "pageId": "acct:p", "sourceKind": "generated",
                               "status": "queued", "createdAt": _recent(), "clips": []}
    _write(job_path, store)
    path = cp._jobs_path()

    errors = []

    def writer():
        try:
            for n in range(50):
                cp._update_job("active", progress=n % 100)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    result = cp.run_compaction_once(path, _now())
    thread.join()
    assert not errors
    assert result["archived"] == 200
    final = cp._load_jobs()
    assert "active" in final["jobs"]
    assert "old0" not in final["jobs"]


def test_no_live_path_needed_job_is_dropped(job_path):
    store = _empty()
    old = _old()
    store["jobs"]["active"] = {"jobId": "active", "pageId": "acct:p", "sourceKind": "generated",
                               "status": "running", "createdAt": old,
                               "generationCheckpointVersion": 1, "providerCheckpoints": {"0": {"x": 1}},
                               "clips": []}
    store["jobs"]["recent-completed"] = {"jobId": "recent-completed", "pageId": "acct:p",
                                         "sourceKind": "generated", "status": "completed",
                                         "createdAt": _recent(), "completedAt": _recent(), "clips": []}
    store["byIdempotency"]["k-active"] = "active"
    _write(job_path, store)
    path = cp._jobs_path()

    cp.run_compaction_once(path, _now())
    final = cp._load_jobs()
    # active job (restart recovery reads it) and recent terminal job (worker
    # status/artifacts reads it) both survive.
    assert set(final["jobs"]) == {"active", "recent-completed"}
    assert final["byIdempotency"]["k-active"] == "active"


def _archive_job_with_key(job_path, job, key):
    store = _empty()
    store["jobs"][job["jobId"]] = job
    store["byIdempotency"][key] = job["jobId"]
    compacted, archived = c.compact_job_store(store, _now())
    assert [row["jobId"] for row in archived] == [job["jobId"]]
    _write(job_path, compacted)


def test_archived_idempotency_key_rejected_by_create_job(
    job_path, tmp_path, monkeypatch,
):
    key = "archived-job-key"
    archived_id = "archived-generated"
    _archive_job_with_key(job_path, {
        **_old_completed(archived_id),
        "sourceKind": "generated",
        "idempotencyKey": key,
    }, key)

    master_pages = {"pageId": "acct:p", "contentEngine": "test_engine"}
    body = {
        "pageId": "acct:p",
        "lane": cp.CONTROL_PLANE_LANE,
        "engine": "test_engine",
        "lockedRecipeId": "test:master",
        "recipeVersion": "v1",
        "quantity": 1,
        "constraints": {},
        "sourceIsolation": None,
        "policyHash": "sha256:" + "1" * 64,
        "masterPages": master_pages,
        "masterPagesHash": "sha256:" + "2" * 64,
    }
    recipe = SimpleNamespace(
        engine="test_engine",
        recipe_id="test:master",
        engine_registry_hash="e" * 64,
        format_contract_version="sha256:" + "f" * 64,
        material_source="generated",
        asset_type="video/mp4",
        executor_version="v1",
        prompt_catalog_hash="p" * 64,
        family_name="test",
        provider_model="test-model",
        planned_provider_calls=lambda quantity: quantity,
    )
    publication = {
        "engine": "ai_video",
        "dossierRevision": 1,
        "recipeSpecHash": "sha256:" + "3" * 64,
    }
    monkeypatch.setattr(cp, "require_control_plane_bearer", lambda _auth: None)
    monkeypatch.setattr(cp, "exact_intent", lambda intent, _hash, **_kwargs: intent)
    monkeypatch.setattr(
        cp, "_current_master_pages_intent",
        lambda _page, intent: (intent, body["masterPagesHash"]),
    )
    monkeypatch.setattr(
        cp, "load_registered_recipe_binding",
        lambda *_args: ("acct:p", publication),
    )
    monkeypatch.setattr(cp, "publication_matches_master_pages", lambda *_args: True)
    monkeypatch.setattr(cp, "resolve_generation_recipe", lambda _publication: recipe)
    monkeypatch.setattr(cp, "_truck_master_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        cp, "plan_prompt_combinations",
        lambda *_args, **_kwargs: [{"promptHash": SHA}],
    )
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generation")
    monkeypatch.setattr(cp, "_start_dossier_generation", lambda _job_id: None)
    monkeypatch.setitem(cp.PROVIDERS, "test_engine", {"key_id": "test"})

    with pytest.raises(HTTPException) as raised:
        asyncio.run(cp.create_job(
            None,
            x_rt_page_id="acct:p",
            x_rt_lane=cp.CONTROL_PLANE_LANE,
            idempotency_key=key,
            authorization="Bearer test",
            body=body,
        ))

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "idempotency_key_archived",
        "jobId": archived_id,
    }
    final = cp._load_jobs()
    assert final["jobs"] == {}
    assert final["byIdempotency"][key] == archived_id


def test_archived_idempotency_key_rejected_by_source_import(
    job_path, tmp_path, monkeypatch,
):
    key = "archived-import-key"
    archived_id = "archived-import"
    _archive_job_with_key(job_path, {
        **_old_completed(archived_id),
        "sourceKind": "page_source_import",
        "idempotencyKey": key,
    }, key)

    master_pages = {
        "pageId": "acct:p",
        "contentEngine": "sourced_video",
        "contentNiche": "niche",
    }
    body = {
        "schema": cp.SOURCE_IMPORT_SCHEMA,
        "pageId": "acct:p",
        "format": "test-format",
        "sourceUrl": "https://example.com/source.mp4",
        "masterPages": master_pages,
        "masterPagesHash": "sha256:" + "2" * 64,
    }
    contract = SimpleNamespace(
        definition_status="complete",
        content_niche="niche",
        content_engine="sourced_video",
        material_source="source_library",
        asset_type="video/mp4",
        contract_hash="f" * 64,
    )
    profile = SimpleNamespace(
        execution_status="commissioned",
        content_niche="niche",
        content_engine="sourced_video",
        material_source="source_library",
        asset_type="video/mp4",
        format_contract_version="sha256:" + "f" * 64,
    )

    async def validated_source_url(value):
        return value

    monkeypatch.setattr(cp, "require_control_plane_bearer", lambda _auth: None)
    monkeypatch.setattr(cp, "_validated_source_url", validated_source_url)
    monkeypatch.setattr(cp, "exact_intent", lambda intent, _hash, **_kwargs: intent)
    monkeypatch.setattr(
        cp, "_current_master_pages_intent",
        lambda _page, intent: (intent, body["masterPagesHash"]),
    )
    monkeypatch.setattr(cp, "load_format_contracts", lambda: ({"test-format": contract}, "h"))
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({"test-format": profile}, "h"))
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generation")
    monkeypatch.setattr(cp, "_start_page_source_import", lambda _job_id: None)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(cp.create_source_import(
            x_rt_page_id="acct:p",
            x_rt_lane=cp.CONTROL_PLANE_LANE,
            idempotency_key=key,
            authorization="Bearer test",
            body=body,
        ))

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "idempotency_key_archived",
        "jobId": archived_id,
    }
    final = cp._load_jobs()
    assert final["jobs"] == {}
    assert final["byIdempotency"][key] == archived_id


# ── K6: performance at 20x growth with compaction on ──────────────────────

def test_compaction_bounds_store_at_20x_growth():
    store = _empty()
    old = _old()
    recent = _recent()
    for i in range(2000):
        store["jobs"][f"old{i}"] = {"jobId": f"old{i}", "status": "completed",
                                    "createdAt": old, "completedAt": old, "pageId": "acct:p",
                                    "clips": [{"sha256": f"{i:064x}", "bytes": 100}]}
    for i in range(20):
        store["jobs"][f"recent{i}"] = {"jobId": f"recent{i}", "status": "completed",
                                       "createdAt": recent, "completedAt": recent, "pageId": "acct:p"}

    full_bytes = len(json.dumps(store, separators=(",", ":")))
    new_store, archived = c.compact_job_store(store, _now())
    assert len(archived) == 2000
    compact_bytes = len(json.dumps(new_store, separators=(",", ":")))
    # The compacted store is bounded by the retained working set, not history.
    assert len(new_store["jobs"]) == 20
    assert compact_bytes < full_bytes // 2
    # The archive preserves every archived record for audit/recovery.
    assert c.archive_lines_for(archived).count("\n") == 2000


# ── #185 views: capabilities() hands the helpers narrowed views ───────────
# After #185, capabilities() no longer passes the whole store to the
# reservation helpers: it passes {"jobs": …} slices built from the snapshot.
# Those helpers read the archived facts from the dict they are given, so each
# view must carry the archiveIndex. These tests build the views with the SAME
# functions capabilities() calls, from a snapshot of a fully-archived store.

def _views(job_path, store):
    _write(job_path, store)
    snapshot = cp._read_jobs_snapshot_object()
    page_view, dna_view = cp._capabilities_job_views(snapshot, "acct:p")
    return page_view, dna_view, cp._generation_related_jobs_view(snapshot, page_view, "acct:p")


def test_source_dna_view_keeps_archived_cuts(job_path):
    store = _empty()
    old = _old()
    store["jobs"]["src-old"] = {"jobId": "src-old", "pageId": "acct:p", "sourceKind": "dossier_source_dna",
                                "status": "completed", "sourceLibraryId": "lib", "sourceLibraryHash": LIB_HASH,
                                "recipeVersion": "rv1", "createdAt": old, "completedAt": old,
                                "sourceCuts": [{"slotId": "s1", "masterSha256": SHA, "startMs": 0, "durationMs": 5000}]}
    recipe = SimpleNamespace(masters=[SimpleNamespace(sha256=SHA)],
                             source_library_id="lib", source_library_hash=LIB_HASH)
    before = cp._source_dna_unavailable_slots(store, recipe, "rv1")
    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["src-old"] and not new_store["jobs"]
    _, dna_view, _ = _views(job_path, new_store)
    assert cp._source_dna_unavailable_slots(dna_view, recipe, "rv1") == before
    assert f"{SHA}:0:5000" in before


def test_page_view_keeps_archived_slideshow_signatures(job_path):
    store = _empty()
    old = _old()
    sig = "f" * 64
    store["jobs"]["slide-old"] = {"jobId": "slide-old", "pageId": "acct:p", "sourceKind": "syzygy_slideshow",
                                  "status": "completed", "sourceLibraryId": "lib", "createdAt": old, "completedAt": old,
                                  "slideshowPlan": [{"signature": sig}]}
    recipe = SimpleNamespace(library_id="lib")
    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["slide-old"] and not new_store["jobs"]
    page_view, _, _ = _views(job_path, new_store)
    assert cp._slideshow_unavailable_signatures(page_view, recipe, "acct:p") == {sig}


def test_generation_view_keeps_archived_truck_jobs(job_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda p: True)
    root = tmp_path / "gen"
    root.mkdir()
    (root / f"clip-{SHA[:8]}.mp4").write_bytes(b"1234")
    store = _empty()
    store["jobs"]["truck-old"] = _truck_job("truck-old", SHA, "acct:p", root, created=_old())
    recipe = SimpleNamespace(
        engine="hailuo", recipe_id="truck-scenic:master",
        engine_registry_hash="e" * 64, format_contract_version="sha256:" + "a" * 64,
        engine_profile_hash="sha256:" + "b" * 64,
        executor_version="v1", prompt_catalog_hash="p" * 64,
        family_name="truck", material_source="generated_video", asset_type="video/mp4",
        provider_model="minimax/hailuo-2.3",
        recipe_spec={"renderTreatment": {"filters": {"brightness": 1.0}, "clipSpeed": 1.0,
                                          "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}},
        clips_per_generation=5,
    )
    kwargs = dict(content_engine="hailuo", recipe_id="truck-scenic:master",
                  generation_recipe=recipe)
    before = cp._truck_master_candidates(store, "acct:p", 10, **kwargs)
    assert [x["sha256"] for x in before] == [SHA]
    new_store, archived = c.compact_job_store(store, _now())
    assert [j["jobId"] for j in archived] == ["truck-old"] and not new_store["jobs"]
    _, _, generation_view = _views(job_path, new_store)
    after = cp._truck_master_candidates(generation_view, "acct:p", 10, **kwargs)
    assert [x["sha256"] for x in after] == [SHA]
