"""C6: behaviour otherwise unchanged.

The actual behavioural risk this PR introduces is narrow: capabilities()
now hands the job-reservation helpers a per-page/per-sourceKind SLICE of the
store (built from the snapshot's index) instead of the full store. Every one
of those helper functions is otherwise untouched — none of their bodies
changed — and each already re-checks pageId/sourceKind/status itself, so a
narrower input can only ever change the result if the slicing itself is
wrong. This file proves it is not wrong: for >= 200 random synthetic
page/store states, the full-store scan and the index-narrowed view produce
byte-identical results at every site capabilities() narrows.

This is a direct, in-repo replacement for a literal base-vs-head HTTP diff:
capabilities()'s own business logic (recipe resolution, quantity planning,
schema shape) is unchanged and is instead covered by the existing
tests/test_control_plane_*.py capability suite continuing to pass unchanged
on this branch (part of the full seeno run). What's new here is specifically
"does going through the index change what a helper sees" — and it does not.
"""

from __future__ import annotations

import random
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
