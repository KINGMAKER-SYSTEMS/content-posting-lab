"""C6, defect 3 follow-up (Tides ruling on PR #185, 2026-09-27): a literal
base-vs-head diff. Dynamically loads main 8ad9ab32's routers/control_plane.py
as an independent module (sharing every OTHER, unchanged dependency module
with head's routers.control_plane via sys.modules caching, since only
control_plane.py itself differs between the two commits) and runs its
`capabilities()` as the reference against head's, over >= 200 random job-
store states, asserting byte-identical JSON.

Split of responsibility (per the ruling): this file proves the NON-TRUCK
fields byte-identical — sourceIdentities, sourced-window reservations,
bootstrap entry, ai_video bindings, maxQuantity. The truck-recovery
reservation regression itself (dropping truck_master_recovery from the
narrowed view over-admits) is proven, mutation-red/green, by the real-file,
stubbed-ffprobe test in test_capability_job_snapshot_golden.py
(test_dropping_truck_reservations_over_admits_is_caught) — real recovery
candidates need actual files on disk and a real ffprobe, which this file
does NOT have, so `_is_exact_16x9_video` and `recovery_treatment_matches`
are stubbed IDENTICALLY on both base and head here: the truck-candidate I/O
path is deliberately neutralized on both sides so it can never contribute a
difference either way, exactly as the ruling specifies.
"""

from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

from routers import control_plane as cp
from services.json_store import atomic_save

BASE_SHA = "8ad9ab32f3804cc98e50384571ee648f978437b6"
NUM_STATES = 200
PAGES = [f"page-{i}" for i in range(8)]
SOURCE_KINDS = [
    "page_source_import", "generated", "dossier_source_dna",
    "truck_master_recovery", "syzygy_slideshow", "some_other_kind",
]
STATUSES = ["queued", "running", "completed", "failed"]
MASTER_SHAS = ["sha-a", "sha-b", "sha-c"]
LIBRARY_IDS = ["lib-a", "lib-b"]
LIBRARY_HASHES = ["hash-1", "hash-2"]
RECIPE_VERSIONS = ["v1", "v2"]
CATALOG_HASHES = ["cat-1", "cat-2"]
FAMILIES = ["family-a", "family-b"]
PROVIDER_MODELS = ["model-a", "model-b"]


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
    elif source_kind == "generated":
        job["promptCatalogHash"] = rng.choice(CATALOG_HASHES)
        job["family"] = rng.choice(FAMILIES)
        job["providerModel"] = rng.choice(PROVIDER_MODELS)
        job["promptPlan"] = [{"promptHash": _random_hex(rng)} for _ in range(rng.randint(0, 2))]
        job["clips"] = [
            {"promptHash": _random_hex(rng)} for _ in range(rng.randint(0, 2))
        ]
        job["engine"] = "ai_video"
        job["recipeId"] = "truck-scenic:master"
        job["artifactRoot"] = f"/nonexistent/{page_id}/{index}"
        for key in ("engineRegistryHash", "formatContractVersion", "executorVersion"):
            job[key] = "stub-" + key
    elif source_kind == "dossier_source_dna":
        job["sourceLibraryId"] = rng.choice(LIBRARY_IDS)
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
    elif source_kind == "truck_master_recovery":
        job["recoveryMasters"] = [
            {"sha256": _random_hex(rng)} for _ in range(rng.randint(0, 2))
        ]
    elif source_kind == "syzygy_slideshow":
        job["sourceLibraryId"] = rng.choice(LIBRARY_IDS)
    return job


def _random_store(rng: random.Random, jobs_per_page: int = 6) -> dict:
    jobs = {}
    for page_id in PAGES:
        for index in range(rng.randint(0, jobs_per_page)):
            kind = rng.choice(SOURCE_KINDS)
            job = _random_job(rng, page_id, index, kind)
            jobs[job["jobId"]] = job
    return {"version": 1, "jobs": jobs, "byIdempotency": {}, "served": {}}


def _load_base_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = subprocess.run(
        ["git", "show", f"{BASE_SHA}:routers/control_plane.py"],
        cwd=repo_root, capture_output=True, check=True, text=True,
    ).stdout
    tmp_dir = Path(tempfile.mkdtemp(prefix="lab_capsnap_base_import_"))
    tmp_file = tmp_dir / "control_plane_base_8ad9ab32.py"
    tmp_file.write_text(source)
    spec = importlib.util.spec_from_file_location("control_plane_base_8ad9ab32_snapshot_test", tmp_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cp_base():
    return _load_base_module()


def _stub_generation_recipe():
    return types.SimpleNamespace(
        recipe_id="truck-scenic:master",
        engine="ai_video",
        engine_registry_hash="stub-engineRegistryHash",
        format_contract_version="stub-formatContractVersion",
        executor_version="stub-executorVersion",
        material_source="generated",
        asset_type="video/mp4",
        prompt_catalog_hash=CATALOG_HASHES[0],
        family_name=FAMILIES[0],
        provider_model=PROVIDER_MODELS[0],
        clips_per_generation=1,
        recipe_spec={"renderTreatment": {}},
        planned_provider_calls=lambda n: min(n, 10),
    )


def _stub_source_recipe():
    return types.SimpleNamespace(
        masters=[
            types.SimpleNamespace(
                sha256=sha, provenance={"sourceUrl": f"https://example.test/master/{sha}"},
            )
            for sha in MASTER_SHAS
        ],
        source_library_id=LIBRARY_IDS[0],
        source_library_hash=LIBRARY_HASHES[0],
        max_quantity=5,
        engine="sourced_video",
        material_source="source_library",
        asset_type="video/mp4",
    )


def _wire(module, monkeypatch, *, job_path, master_pages, bindings_present):
    monkeypatch.setattr(module, "_jobs_path", lambda: job_path)
    if hasattr(module, "_jobs_snapshot"):
        module._jobs_snapshot = None
    if hasattr(module, "_jobs_decode_pending"):
        module._jobs_decode_pending.clear()
    monkeypatch.setattr(module, "_current_intent_for_capabilities", lambda _: (master_pages, "hash"))

    generation_publication = {
        "recipeId": "truck-scenic:master", "engine": "ai_video",
        "recipeVersion": RECIPE_VERSIONS[0], "recipeSpecHash": "sha256:" + ("ef" * 32),
    }
    source_publication = {
        "recipeId": "dossier-source:master", "engine": "sourced_video",
        "recipeVersion": RECIPE_VERSIONS[1], "recipeSpecHash": "sha256:" + ("cd" * 32),
    }
    bindings = (
        [("gen-key", generation_publication), ("src-key", source_publication)]
        if bindings_present else []
    )
    monkeypatch.setattr(module, "list_registered_recipe_bindings", lambda *a: bindings)

    bootstrap_profile = types.SimpleNamespace(
        content_niche="TRUCK", content_engine="ai_video", execution_status="commissioned",
        format_slug="truck-scenic", format_contract_version="stub-bootstrap-version",
        max_quantity=7,
    )
    monkeypatch.setattr(module, "load_engine_registry", lambda: ({"truck-scenic": bootstrap_profile}, "hash"))

    monkeypatch.setattr(module, "resolve_generation_recipe", lambda publication: _stub_generation_recipe())
    monkeypatch.setattr(module, "_dossier_source_recipe", lambda publication: _stub_source_recipe())
    # sourced_slideshow/lyrics_slideshows are not in the field list this
    # file is scoped to (per the ruling); force that branch off identically
    # on both sides rather than wiring its own deep material-profile chain.
    monkeypatch.setattr(module, "resolve_slideshow_recipe", lambda publication: None)

    # Sensitive to reservations (Tides review of PR #185, round 2, defect
    # 1): a stub that always returns [] makes fresh_capacity 0 regardless
    # of what _generated_unavailable_prompts computed, which made a mutant
    # dropping every generated reservation pass all 200 states. Its length
    # must depend on `hashes`/`slots`, the same way plan_source_cuts'
    # stub below already does.
    monkeypatch.setattr(
        module, "plan_prompt_combinations",
        lambda recipe, run_id, count, hashes, slots=None: list(
            range(max(0, count - len(hashes) - len(slots or ())))
        ),
    )
    monkeypatch.setattr(
        module, "plan_source_cuts",
        lambda recipe, quantity, served_slots, *a, **k: list(range(max(0, quantity - len(served_slots)))),
    )
    # Truck candidate I/O: stubbed IDENTICALLY on both sides (this file's
    # scope excludes the truck field; see module docstring). Neutralizing
    # ffprobe and treatment-matching means _truck_master_candidates' data
    # selection still runs for real and identically on both sides, it just
    # can never produce a candidate — consistent with "truck can be stubbed
    # identically since it's covered elsewhere," not "truck is skipped."
    monkeypatch.setattr(module, "_is_exact_16x9_video", lambda path: False)
    monkeypatch.setattr(module, "recovery_treatment_matches", lambda *a, **k: False)


@pytest.mark.parametrize("seed", range(NUM_STATES))
def test_head_matches_base_capabilities_over_random_states(seed, tmp_path, monkeypatch, cp_base):
    rng = random.Random(seed)
    store = _random_store(rng)
    job_path = tmp_path / f"jobs-{seed}.json"
    atomic_save(job_path, store)

    page_id = rng.choice(PAGES)
    master_pages = {"contentNiche": "TRUCK", "contentEngine": "ai_video"}
    bindings_present = rng.random() > 0.3  # sometimes exercise the bootstrap-only path

    _wire(cp, monkeypatch, job_path=job_path, master_pages=master_pages, bindings_present=bindings_present)
    head_result = cp.capabilities(x_rt_page_id=page_id, x_page_id=None)

    _wire(cp_base, monkeypatch, job_path=job_path, master_pages=master_pages, bindings_present=bindings_present)
    base_result = cp_base.capabilities(x_rt_page_id=page_id, x_page_id=None)

    assert json.dumps(head_result, sort_keys=True) == json.dumps(base_result, sort_keys=True), (
        seed, page_id, head_result, base_result,
    )
