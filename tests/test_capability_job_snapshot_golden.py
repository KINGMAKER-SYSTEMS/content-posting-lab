"""C6: behaviour otherwise unchanged.

The actual behavioural risk this PR introduces is narrow: capabilities()
now hands the job-reservation helpers a per-page/per-sourceKind SLICE of the
store (built from the snapshot's index) instead of the full store. Every one
of those helper functions is otherwise untouched — none of their bodies
changed — and each already re-checks pageId/sourceKind/status itself, so a
narrower input can only ever change the result if the slicing itself is
wrong. `test_index_narrowed_views_match_full_store_scan` proves this for the
three page-/sourceKind-scoped sites (page-source-import identities,
slideshow reservations, source-dna reservations) across >= 200 random
states, at the helper level.

Revision note (Tides review of PR #185, defect 3): this file originally also
claimed generated/truck-recovery coverage and described itself as "a direct
in-repo replacement for a literal base-vs-head HTTP diff" — neither was
accurate. The randomized comparison never passed the store-wide
`generation_related_jobs_view` capabilities() actually builds for the
ai_video/generated site (:391-401 in-file), and no test here ever ran
base's code or called `capabilities()` at all. `resolve_generation_recipe`
is real, deeply-wired machinery (prompt catalogs, format contracts, engine
registry files), and `_truck_master_candidates`' candidate loop additionally
stats and ffprobes real files on disk — so a same-code helper comparison for
that site is honest coverage only once real files are involved; without
them, a candidate is excluded by the missing-file check regardless of
whether the truck-recovery reservation was dropped, which would make a
"200 random states, no real files" version of this test pass against the
exact regression it was supposed to catch. `test_dropping_truck_reservations_over_admits_is_caught`
below is a smaller, deterministic, real-file test built specifically to be
sensitive to that one question — proved mutation-red against the dropped
narrowing described in the review, proved green against the shipped
`{by_page[page_id], by_source_kind["truck_master_recovery"]}` view.
"""

from __future__ import annotations

import random
import types
from types import SimpleNamespace

import pytest

from routers import control_plane as cp

NUM_STATES = 200
SOURCE_KINDS = [
    "page_source_import", "syzygy_slideshow", "generated",
    "dossier_source_dna", "truck_master_recovery", "some_other_kind",
]
STATUSES = ["queued", "running", "completed", "failed"]
PAGES = [f"page-{i}" for i in range(8)]
LIBRARIES = ["lib-a", "lib-b", "lib-c"]
CATALOG_HASHES = ["cat-1", "cat-2"]
FAMILIES = ["family-a", "family-b"]
PROVIDER_MODELS = ["model-a", "model-b"]
MASTER_SHAS = ["sha-a", "sha-b", "sha-c"]
LIBRARY_HASHES = ["hash-1", "hash-2"]
RECIPE_VERSIONS = ["v1", "v2"]


def _random_hex(rng: random.Random) -> str:
    return format(rng.getrandbits(256), "064x")


def _random_job(rng: random.Random, page_id: str, index: int, source_kind: str) -> dict:
    job: dict = {
        "jobId": f"{page_id}-{index}",
        "pageId": page_id,
        "sourceKind": source_kind,
        "status": rng.choice(STATUSES),
    }
    if source_kind == "page_source_import":
        job["sourceUrl"] = (
            f"https://example.test/{page_id}/{index}" if rng.random() > 0.1 else None
        )
    elif source_kind == "syzygy_slideshow":
        job["sourceLibraryId"] = rng.choice(LIBRARIES)
        job["slideshowPlan"] = [
            {"signature": _random_hex(rng)} for _ in range(rng.randint(0, 3))
        ]
    elif source_kind == "generated":
        # promptCatalogHash/family/providerModel are read off the JOB, not
        # the clip (_generated_unavailable_prompts gates promptSlots
        # reservation on all three matching the recipe at the job level).
        job["promptCatalogHash"] = rng.choice(CATALOG_HASHES)
        job["family"] = rng.choice(FAMILIES)
        job["providerModel"] = rng.choice(PROVIDER_MODELS)
        job["promptPlan"] = [
            {"promptHash": _random_hex(rng)} for _ in range(rng.randint(0, 2))
        ]
        job["clips"] = [
            {
                "promptHash": _random_hex(rng),
                **({"promptSlots": {"style": rng.choice(["a", "b"])}} if rng.random() > 0.5 else {}),
            }
            for _ in range(rng.randint(0, 2))
        ]
    elif source_kind == "dossier_source_dna":
        job["sourceLibraryId"] = rng.choice(LIBRARIES)
        job["sourceLibraryHash"] = rng.choice(LIBRARY_HASHES)
        job["recipeVersion"] = rng.choice(RECIPE_VERSIONS)
        job["sourceCuts"] = [
            {
                "slotId": f"slot-{rng.randint(0, 5)}",
                "masterSha256": rng.choice(MASTER_SHAS),
                "startMs": rng.randint(0, 100_000),
                "durationMs": rng.randint(1_000, 5_000),
            }
            for _ in range(rng.randint(0, 3))
        ]
    return job


def _random_store(rng: random.Random, pages=PAGES, jobs_per_page=6) -> dict:
    jobs = {}
    for page_id in pages:
        for index in range(rng.randint(0, jobs_per_page)):
            kind = rng.choice(SOURCE_KINDS)
            job = _random_job(rng, page_id, index, kind)
            jobs[job["jobId"]] = job
    return {"version": 1, "jobs": jobs, "byIdempotency": {}, "served": {}}


def _full_store_import_identities(store: dict, page_id: str) -> list:
    return sorted({
        identity
        for job in store.get("jobs", {}).values()
        if isinstance(job, dict)
        and job.get("pageId") == page_id
        and job.get("sourceKind") == "page_source_import"
        and job.get("status") == "completed"
        for identity in [cp.canonical_source_identity(job.get("sourceUrl"))]
        if identity is not None
    })


def _narrowed_import_identities(by_page: dict, page_id: str) -> list:
    return sorted({
        identity
        for job in by_page.get(page_id, {}).values()
        if isinstance(job, dict)
        and job.get("sourceKind") == "page_source_import"
        and job.get("status") == "completed"
        for identity in [cp.canonical_source_identity(job.get("sourceUrl"))]
        if identity is not None
    })


@pytest.mark.parametrize("seed", range(NUM_STATES))
def test_index_narrowed_views_match_full_store_scan(seed):
    rng = random.Random(seed)
    store = _random_store(rng)
    by_page, by_source_kind = cp._build_jobs_indices(store)
    page_id = rng.choice(PAGES)

    # 1) completed page-source-import identities (inlined in capabilities()).
    assert _full_store_import_identities(store, page_id) == _narrowed_import_identities(
        by_page, page_id,
    )

    # 2) syzygy_slideshow reservations.
    slideshow_recipe = SimpleNamespace(library_id=rng.choice(LIBRARIES))
    page_view = {"jobs": by_page.get(page_id, {})}
    assert cp._slideshow_unavailable_signatures(
        store, slideshow_recipe, page_id,
    ) == cp._slideshow_unavailable_signatures(page_view, slideshow_recipe, page_id)

    # 3) dossier_source_dna reservations (NOT page-scoped by design — scoped
    # by sourceKind only, since a source library can span pages).
    source_recipe = SimpleNamespace(
        masters=[
            SimpleNamespace(sha256=sha)
            for sha in rng.sample(MASTER_SHAS, k=rng.randint(1, len(MASTER_SHAS)))
        ],
        source_library_id=rng.choice(LIBRARIES),
        source_library_hash=rng.choice(LIBRARY_HASHES),
    )
    recipe_version = rng.choice(RECIPE_VERSIONS)
    dossier_view = {"jobs": by_source_kind.get("dossier_source_dna", {})}
    assert cp._source_dna_unavailable_slots(
        store, source_recipe, recipe_version,
    ) == cp._source_dna_unavailable_slots(dossier_view, source_recipe, recipe_version)

    # 4) generated-prompt reservations.
    generation_recipe = SimpleNamespace(
        prompt_catalog_hash=rng.choice(CATALOG_HASHES),
        family_name=rng.choice(FAMILIES),
        provider_model=rng.choice(PROVIDER_MODELS),
    )
    full_hashes, full_slots = cp._generated_unavailable_prompts(
        store, generation_recipe, page_id,
    )
    narrowed_hashes, narrowed_slots = cp._generated_unavailable_prompts(
        page_view, generation_recipe, page_id,
    )
    assert full_hashes == narrowed_hashes
    assert full_slots == narrowed_slots


def test_index_partition_is_lossless_and_disjoint_by_construction():
    """Sanity check on the index itself, over one larger random store: every
    job appears in its page bucket (if it has one) and its sourceKind bucket
    (if it has one), and nothing appears that shouldn't."""
    rng = random.Random("partition-check")
    store = _random_store(rng, jobs_per_page=40)
    by_page, by_source_kind = cp._build_jobs_indices(store)

    for job_id, job in store["jobs"].items():
        page_id = job["pageId"]
        assert by_page[page_id][job_id] is job
        assert by_source_kind[job["sourceKind"]][job_id] is job

    assert sum(len(v) for v in by_page.values()) == len(store["jobs"])
    assert sum(len(v) for v in by_source_kind.values()) == len(store["jobs"])


def test_dropping_truck_reservations_over_admits_is_caught(tmp_path, monkeypatch):
    """Defect 3 fix (Tides review of PR #185): a deterministic, real-file
    mutation proof for the ai_video/generated site's narrowed view.

    `_truck_master_candidates` excludes a candidate at the FIRST field that
    fails (`sha256 in seen`, from the truck_master_recovery reservation) OR
    at the file-existence check, whichever comes first in its filter chain —
    the reservation check runs BEFORE the file check. So the reservation
    genuinely changes the outcome (this test), independent of whether real
    files exist for candidates that were never reserved in the first place;
    for the excluded-by-reservation path, a real file must exist so the
    "excluded by missing file" and "excluded by reservation" cases are
    distinguishable at all. `_is_exact_16x9_video` (ffprobe) and
    `recovery_treatment_matches` are stubbed: this test is about which JOBS
    get scanned, not about video-probing or treatment-matching correctness,
    both already covered elsewhere and untouched by this PR.
    """
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda path: True)
    monkeypatch.setattr(cp, "recovery_treatment_matches", lambda *args, **kwargs: True)

    page_id = "truck-page"
    recipe_id = cp.TRUCK_RECIPE_ID
    recipe_spec_hash = "sha256:" + ("ef" * 32)
    sha256 = "a" * 64

    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    clip_bytes = 128
    (artifact_root / "clip.mp4").write_bytes(b"0" * clip_bytes)

    generation_recipe = types.SimpleNamespace(
        recipe_id=recipe_id,
        engine="ai_video",
        engine_registry_hash="h-engine",
        format_contract_version="h-format",
        executor_version="h-executor",
        prompt_catalog_hash="h-catalog",
        provider_model="h-model",
        recipe_spec={"renderTreatment": {}},
    )
    generated_job = {
        "jobId": "gen-1",
        "pageId": page_id,
        "sourceKind": "generated",
        "status": "completed",
        "engine": "ai_video",
        "recipeId": recipe_id,
        "engineRegistryHash": "h-engine",
        "formatContractVersion": "h-format",
        "executorVersion": "h-executor",
        "promptCatalogHash": "h-catalog",
        "providerModel": "h-model",
        "artifactRoot": str(artifact_root),
        "createdAt": "2026-01-01T00:00:00+00:00",
        "clips": [{
            "path": "clip.mp4",
            "sha256": sha256,
            "bytes": clip_bytes,
            "source": {
                "pageId": page_id,
                "recipeId": recipe_id,
                "contentNiche": "TRUCK",
                "contentEngine": "ai_video",
            },
            "sourceTreatment": {"recipeSpecHash": recipe_spec_hash},
        }],
    }
    # A completed recovery job reserving the exact same master, filed under
    # a DIFFERENT page — recovery reservations are cross-page by design
    # (routers/control_plane.py's own _truck_master_candidates comment).
    recovery_job = {
        "jobId": "recovery-1",
        "pageId": "some-other-page",
        "sourceKind": "truck_master_recovery",
        "status": "completed",
        "recoveryMasters": [{"sha256": sha256}],
    }
    store = {
        "jobs": {generated_job["jobId"]: generated_job, recovery_job["jobId"]: recovery_job},
        "version": 1, "byIdempotency": {}, "served": {},
    }
    by_page, by_source_kind = cp._build_jobs_indices(store)

    # The SHIPPED view (routers/control_plane.py :401-406, after the C3 fix):
    # this page's own jobs, unioned with every truck_master_recovery job.
    shipped_view = {
        "jobs": {
            **by_page.get(page_id, {}),
            **by_source_kind.get("truck_master_recovery", {}),
        },
    }
    # The REGRESSION the review's mutant B reproduced: truck_master_recovery
    # dropped from the union, so a completed recovery no longer reserves
    # anything.
    mutant_view = {"jobs": {**by_page.get(page_id, {})}}

    shipped_candidates = cp._truck_master_candidates(
        shipped_view, page_id, 10, content_engine="ai_video", recipe_id=recipe_id,
        generation_recipe=generation_recipe, current_recipe_spec_hash=recipe_spec_hash,
    )
    mutant_candidates = cp._truck_master_candidates(
        mutant_view, page_id, 10, content_engine="ai_video", recipe_id=recipe_id,
        generation_recipe=generation_recipe, current_recipe_spec_hash=recipe_spec_hash,
    )

    # Shipped view: the completed recovery reserves the sha256, so the
    # identical master is correctly NOT offered again as a fresh candidate.
    assert shipped_candidates == []
    # Mutant view: the reservation disappeared, so the exact same master
    # over-admits as an available candidate — the regression this test is
    # built to catch.
    assert len(mutant_candidates) == 1
    assert mutant_candidates[0]["sha256"] == sha256
