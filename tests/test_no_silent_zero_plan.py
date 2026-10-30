"""CI guard: no format family may reach zero plans silently.

Covers every planner dispatch family — image-video photo holds, clean-video
(curated and generic), the stride/start permutation slideshows, source-DNA
recuts, and ai_video generation. A zero-plan answer must always name a reason
code, and fresh media must produce new plans wherever the format allows it
(the terminal-by-design families are the curated pinned montage and the
source-DNA master windows).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import types
from dataclasses import replace

import pytest

from services import control_plane_slideshows as ss
from services import control_plane_image_video as iv
from services import control_plane_plan_reasons as reasons
from services.control_plane_generation import (
    GenerationRecipe,
    plan_prompt_combinations,
    prompt_combination_space,
)
from services.control_plane_sources import (
    SOURCE_WINDOWS_EXHAUSTED,
    explain_empty_source_plan,
    plan_source_cuts,
)
from services.content_engine_registry import load_engine_registry
from services.json_store import atomic_save
from routers import control_plane as cp
from tests.test_control_plane_ugc_slideshow import (
    _culture_library,
    _culture_recipe,
    _ugc_library,
    _ugc_recipe,
)
from tests.test_control_plane_lyric_video import recipe_and_library as burnt_recipe_and_library
from tests.test_control_plane_image_video import PENPAL as PENPAL_PAGE, TWIN as TWIN_PAGE
from tests.test_image_video_variants import (
    PENPAL_CANONICAL,
    TWIN_CANONICAL,
    _real_library,
    _recipe as _image_video_recipe,
)
from tests.test_curated_variants import BURNT_CANONICAL, CULTURE_CANONICAL
from tests.source_dna_fixture import isolate_source_dna_registry


@pytest.fixture(autouse=True)
def _advertise_zero_reasons(monkeypatch):
    """These tests check the zero-plan reason fields, which the Lab sends
    only when CONTENT_LAB_ADVERTISE_ZERO_REASONS is on."""
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ZERO_REASONS", "1")


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    """Retired chase/BTL libraries are deleted, so source-dna tests run against
    an isolated copy of the registry plus the non-retired dirt-bike fixture."""
    isolate_source_dna_registry(monkeypatch, tmp_path)


# ── Sweep table: every format slug the planners dispatch on, its family,
#    whether a zero is terminal by design (fresh media cannot refill it), and
#    the reason code the explain path must return. A new format slug with no
#    row here fails the coverage test.
SWEEP = {
    # image-video photo holds (#266 variants draw from the page's own photos).
    "image-video": ("slideshow", False, reasons.PHOTO_VARIANTS_EXHAUSTED),
    "single-image-video": ("slideshow", False, reasons.PHOTO_VARIANTS_EXHAUSTED),
    # clean-video: ugc is generic OR curated by page; lyric-video is always
    # curated (slideshow_library_address refuses it without a curated plan).
    "ugc-slideshow": ("slideshow", False, reasons.GENERIC_MEDIA_EXHAUSTED),
    "lyric-video": ("slideshow", True, reasons.CURATED_VARIANTS_EXHAUSTED),
    # stride/start permutation slideshows.
    "lyric-edits": ("slideshow", False, reasons.SLIDESHOW_PERMUTATIONS_EXHAUSTED),
    "meme-slideshow": ("slideshow", False, reasons.SLIDESHOW_PERMUTATIONS_EXHAUSTED),
    # source-DNA recuts: windows per master are terminal; a new master refills.
    "pov-dirt-bike": ("source-dna", True, reasons.SOURCE_WINDOWS_EXHAUSTED),
    "pov-club": ("source-dna", True, reasons.SOURCE_WINDOWS_EXHAUSTED),
    "pov-night-core": ("source-dna", True, reasons.SOURCE_WINDOWS_EXHAUSTED),
    "pov-scenic": ("source-dna", True, reasons.SOURCE_WINDOWS_EXHAUSTED),
    "meme": ("source-dna", True, reasons.SOURCE_WINDOWS_EXHAUSTED),
    # ai_video generation: prompts reserve only while jobs are in flight, so a
    # zero clears when those finish (not terminal by design).
    "boat-lake": ("generated", False, reasons.PROMPT_INVENTORY_EXHAUSTED),
    "coffee-tok": ("generated", False, reasons.PROMPT_INVENTORY_EXHAUSTED),
    "silhouette-truck": ("generated", False, reasons.PROMPT_INVENTORY_EXHAUSTED),
    "truck-scenic": ("generated", False, reasons.PROMPT_INVENTORY_EXHAUSTED),
}


def _commissioned_profiles():
    profiles, _ = load_engine_registry()
    return {slug: p for slug, p in profiles.items() if p.execution_status == "commissioned"}


def _dispatched_slugs():
    profiles = _commissioned_profiles()
    slideshow = {slug for slug, p in profiles.items() if p.executor_kind == "slideshow_renderer"}
    source_dna = {slug for slug, p in profiles.items() if p.executor_id == "source-dna-recut"}
    ai_video = {slug for slug, p in profiles.items() if p.executor_kind == "prompt_family"}
    dispatched = iv.IMAGE_VIDEO_FORMATS | ss.CLEAN_VIDEO_FORMATS | slideshow | source_dna | ai_video
    return dispatched


# ── 6a: coverage — a new dispatched format with no sweep row fails here ─────
def test_every_dispatched_format_has_a_sweep_row_and_a_valid_reason():
    dispatched = _dispatched_slugs()
    assert dispatched, "the dispatch enumeration came back empty"
    assert set(SWEEP) == dispatched, (
        "sweep table drifted from the planner dispatch families",
        sorted(set(SWEEP) - dispatched),
        sorted(dispatched - set(SWEEP)),
    )
    for family, terminal, code in SWEEP.values():
        table_family, table_terminal, remedy = reasons.reason_for(code)
        assert table_family == family, (code, table_family, family)
        assert table_terminal is terminal, (code, table_terminal, terminal)
        assert isinstance(remedy, str) and remedy.strip()


# ── Back-compat: first-plan signatures are byte-identical to before ────────
def _generic_old_signature(template, objects):
    identity = {
        "templateSlug": template.slug,
        "mediaKeys": [item.key for item in objects],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_generic_chunk_zero_signature_is_unchanged():
    recipe = _ugc_recipe()
    library = _ugc_library()
    template = recipe.templates[0]
    chunk = ss._generic_clean_video_chunk(template, list(library.objects), 0)
    assert chunk is not None
    assert chunk["signature"] == _generic_old_signature(template, list(library.objects))
    # And the plan_slideshows path returns that same chunk 0 plan.
    plan = ss.plan_slideshows(recipe, library, 1, set(), "run")
    assert plan[0]["signature"] == chunk["signature"]
    assert plan[0]["mediaKeys"] == [item.key for item in library.objects]


@pytest.mark.parametrize(
    "page,lane,subject,library_id,format_slug,canonical,count",
    [
        (PENPAL_PAGE, "animals", "pen-pal3", "syzygy:animals:pen-pal3",
         "image-video", PENPAL_CANONICAL, 3),
        (TWIN_PAGE, "interiors", "twin47996", "syzygy:interiors:twin47996",
         "single-image-video", TWIN_CANONICAL, 1),
    ],
    ids=["pen-pal3", "twin47996"],
)
def test_image_video_variant_zero_signature_is_unchanged(
    page, lane, subject, library_id, format_slug, canonical, count,
):
    assert iv.image_video_plan(page)["signature"] == canonical


def test_curated_variant_zero_signature_is_unchanged():
    recipe, library = burnt_recipe_and_library()
    assert ss.plan_slideshows(recipe, library, 1, set(), "run")[0]["signature"] == BURNT_CANONICAL
    recipe, library = _culture_recipe(), _culture_library()
    assert ss.plan_slideshows(recipe, library, 1, set(), "run")[0]["signature"] == CULTURE_CANONICAL


# ── 6b: per family, exhaustion names a code; fresh media refills (or stays
#        terminal by design) ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "page,lane,subject,library_id,format_slug",
    [
        (PENPAL_PAGE, "animals", "pen-pal3", "syzygy:animals:pen-pal3", "image-video"),
        (TWIN_PAGE, "interiors", "twin47996", "syzygy:interiors:twin47996", "single-image-video"),
    ],
    ids=["pen-pal3", "twin47996"],
)
def test_image_video_exhaustion_names_code_and_fresh_photo_refills(
    page, lane, subject, library_id, format_slug,
):
    library = _real_library(lane, subject, library_id)
    recipe = _image_video_recipe(page, format_slug, library)
    everything = {plan["signature"] for plan in iv.image_video_variant_plans(page, library)}
    assert ss.plan_slideshows(recipe, library, 1, everything, "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, library, 1, everything) \
        == reasons.PHOTO_VARIANTS_EXHAUSTED
    # Fresh photo (real-shaped key with an embedded SHA) creates new variants.
    fresh = ss.SyzygyObject(
        f"{lane}/{subject}/photos/pexels_fresh_{'e' * 64}.jpg",
        424242,
        f"https://r2.example/{lane}/{subject}/photos/pexels_fresh_{'e' * 64}.jpg",
    )
    grown = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, fresh), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, grown, 1, everything, "run"), (
        "fresh photo must produce a new photo-hold variant"
    )


def test_image_video_pinned_photos_missing_names_its_own_code():
    page, lane, subject, library_id, format_slug = (
        PENPAL_PAGE, "animals", "pen-pal3", "syzygy:animals:pen-pal3", "image-video",
    )
    library = _real_library(lane, subject, library_id)
    recipe = _image_video_recipe(page, format_slug, library)
    photo = iv.image_video_plan(page)
    pinned_keys = {item["key"] for item in photo["media"]}
    damaged = tuple(
        ss.SyzygyObject(o.key, o.bytes + 1 if o.key in pinned_keys else o.bytes, o.url)
        for o in library.objects
    )
    damaged_library = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject, damaged, library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, damaged_library, 1, set(), "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, damaged_library, 1, set()) \
        == reasons.PINNED_PHOTOS_MISSING


def test_generic_clean_video_exhaustion_names_code_and_fresh_photos_refill():
    recipe = _ugc_recipe()
    library = _ugc_library()
    chunk0 = ss._generic_clean_video_chunk(recipe.templates[0], list(library.objects), 0)
    assert ss.plan_slideshows(recipe, library, 1, {chunk0["signature"]}, "run") == []
    assert reasons.explain_empty_slideshow_plan(
        recipe, library, 1, {chunk0["signature"]},
    ) == reasons.GENERIC_MEDIA_EXHAUSTED
    # A full fresh chunk (media_count keys) makes the next unreserved chunk live.
    fresh = tuple(
        ss.SyzygyObject(
            f"ugc/french-core/photos/img-fresh-{index:02d}.jpg",
            9_000 + index,
            f"https://r2.example/ugc/french-core/photos/img-fresh-{index:02d}.jpg",
        )
        for index in range(recipe.templates[0].media_count)
    )
    grown = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, *fresh), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, grown, 1, {chunk0["signature"]}, "run"), (
        "a fresh unreserved chunk must produce a new generic clean-video plan"
    )


def test_generic_clean_video_insertion_never_reuses_reserved_media():
    recipe = _ugc_recipe()
    library = _ugc_library()
    first = ss.plan_slideshows(recipe, library, 1, set(), "run")[0]
    early = ss.SyzygyObject(
        "ugc/french-core/photos/img-00-early.jpg", 1000,
        "https://r2.example/ugc/french-core/photos/img-00-early.jpg",
    )
    grown = replace(library, objects=(early, *library.objects))
    assert ss.plan_slideshows(
        recipe, grown, 1, {first["signature"]}, "run",
        reserved_media_keys=set(first["mediaKeys"]),
    ) == []  # one fresh key is insufficient for the next unreserved three-photo plan
    more = tuple(ss.SyzygyObject(
        f"ugc/french-core/photos/img-00-{index}.jpg", 1000,
        f"https://r2.example/ugc/french-core/photos/img-00-{index}.jpg",
    ) for index in range(2))
    grown = replace(grown, objects=tuple(sorted((*grown.objects, *more), key=lambda x: x.key)))
    next_plan = ss.plan_slideshows(
        recipe, grown, 1, {first["signature"]}, "run",
        reserved_media_keys=set(first["mediaKeys"]),
    )[0]
    # Disjointness holds here only because the fresh keys sort before the
    # reserved chunk, so the next unreserved chunk skips the reserved media.
    assert set(next_plan["mediaKeys"]).isdisjoint(first["mediaKeys"])
    assert ss.plan_slideshows(
        recipe, grown, 1, {first["signature"]}, "run",
        opaque_reserved_media=True,
    ) == []


def _permutation_recipe_and_library(
    format_slug: str = "meme-slideshow",
    engine: str = "sourced_slideshow",
):
    lane = format_slug.split("-")[0]
    subject = "holy-fumble"
    recipe = ss.SlideshowRecipe(
        recipe_id=f"{format_slug}:master",
        format_slug=format_slug,
        engine=engine,
        engine_registry_hash="e" * 64,
        format_contract_version="sha256:" + "f" * 64,
        material_source="image_library",
        asset_type="video/mp4",
        executor_version="sha256:" + "d" * 64,
        library_id=f"syzygy:{lane}:{subject}",
        lane=lane,
        subject=subject,
        minimum_library_items=5,
        templates=tuple(ss.SyzygyTemplate(slug, 5) for slug in (
            "digital-postcard-hook", "clean-slideshow", "mirror-split",
        )),
        max_quantity=10,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={},
    )
    objects = tuple(
        ss.SyzygyObject(
            f"{lane}/{subject}/photos/img-{index:02d}.jpg",
            1_000 + index,
            f"https://r2.example/{lane}/{subject}/photos/img-{index:02d}.jpg",
        )
        for index in range(5)
    )
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    library = ss.SyzygyLibrary(
        recipe.library_id, lane, subject, objects,
        hashlib.sha256(canonical.encode()).hexdigest(),
    )
    return recipe, library


def test_explain_never_returns_empty_for_library_mismatch():
    recipe, library = _permutation_recipe_and_library()
    # A library whose address does not match the recipe is a named mismatch.
    foreign = ss.SyzygyLibrary(
        "syzygy:meme:someone-else", library.lane, library.subject,
        library.objects, library.snapshot_hash,
    )
    assert reasons.explain_empty_slideshow_plan(recipe, foreign, 1, set()) \
        == reasons.LIBRARY_MISMATCH


def test_permutation_exhaustion_names_code_and_fresh_key_refills():
    recipe, library = _permutation_recipe_and_library()
    everything = {
        plan["signature"]
        for plan in ss.plan_slideshows(recipe, library, 10_000, set(), "run")
    }
    assert everything, "a 5-object permutation library must offer candidates"
    assert ss.plan_slideshows(recipe, library, 1, everything, "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, library, 1, everything) \
        == reasons.SLIDESHOW_PERMUTATIONS_EXHAUSTED
    fresh = ss.SyzygyObject(
        "meme/holy-fumble/photos/img-99.jpg", 9_999,
        "https://r2.example/meme/holy-fumble/photos/img-99.jpg",
    )
    grown = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, fresh), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, grown, 1, everything, "run"), (
        "a fresh key must produce a new permutation plan"
    )


def test_permutation_exhaustion_explain_stays_under_generous_perf_bound():
    """LOW-R2-6: explain_empty_slideshow_plan deliberately re-runs the stride/
    start candidate scan that plan_slideshows just ran (no shared cache — a
    process-wide cache would hand every request the same plan dicts, which
    create_job then mutates in place). This bounds the double scan on a
    realistic library with every plan reserved, so a future accidental
    blow-up of the empty path is caught."""
    import time

    recipe, library = _permutation_recipe_and_library()
    everything = {
        plan["signature"]
        for plan in ss.plan_slideshows(recipe, library, 10_000, set(), "run")
    }
    assert everything, "a 5-object permutation library must offer candidates"
    assert ss.plan_slideshows(recipe, library, 1, everything, "run") == []
    started = time.monotonic()
    assert reasons.explain_empty_slideshow_plan(recipe, library, 1, everything) \
        == reasons.SLIDESHOW_PERMUTATIONS_EXHAUSTED
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (
        f"explain_empty_slideshow_plan took {elapsed:.3f}s on a reserved-out "
        "realistic permutation library; the empty path must stay cheap"
    )


@pytest.mark.parametrize(
    "page", ["culture", "burnt"], ids=["culture", "burnt"],
)
def test_curated_exhaustion_is_terminal_by_design(page):
    if page == "culture":
        recipe, library = _culture_recipe(), _culture_library()
    else:
        recipe, library = burnt_recipe_and_library()
    everything = {
        plan["signature"]
        for plan in ss.curated_plans_for_recipe(recipe, library)
    }
    assert ss.plan_slideshows(recipe, library, 1, everything, "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, library, 1, everything) \
        == reasons.CURATED_VARIANTS_EXHAUSTED
    # A fresh library clip never refills a curated montage: its sources are
    # pinned, extras are ignored, so the same terminal code must return.
    fresh = ss.SyzygyObject(
        f"{recipe.lane}/{recipe.subject}/clips/ytal-v-fresh.mp4",
        1_000_000,
        f"https://r2.example/{recipe.lane}/{recipe.subject}/clips/ytal-v-fresh.mp4",
    )
    grown = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, fresh), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, grown, 1, everything, "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, grown, 1, everything) \
        == reasons.CURATED_VARIANTS_EXHAUSTED


def test_source_dna_and_generated_reason_codes_are_registered_and_terminal():
    assert reasons.reason_for(reasons.SOURCE_WINDOWS_EXHAUSTED) == (
        "source-dna", True,
        "Every window of every master is cut; add a new master.",
    )
    assert reasons.reason_for(reasons.SOURCE_MASTER_TOO_SHORT)[1] is True
    assert reasons.reason_for(reasons.PROMPT_INVENTORY_EXHAUSTED) == (
        "generated", False,
        "Every fresh prompt combination is in flight; clears when those jobs finish.",
    )


# ── 6c: capabilities() carries zeroReason and zeroPlanRecipes ──────────────
def _capability_scaffold(
    monkeypatch, tmp_path, *, recipe, library, load_error=None,
    publication_engine=None, publication_recipe_id=None,
    resolve_slideshow=None, resolve_generation=None, resolve_source=None,
    generation_reason=None, runtime_available=True, real_generation=False,
):
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    cp._capabilities_cache.clear()
    atomic_save(tmp_path / "jobs.json", {"jobs": {}, "version": 1, "byIdempotency": {}, "served": {}})
    monkeypatch.setattr(
        cp, "_current_intent_for_capabilities",
        lambda _: ({"contentNiche": "OTHER", "contentEngine": "sourced_slideshow"}, "fixed-intent-hash"),
    )
    publication = {
        "recipeId": publication_recipe_id or recipe.recipe_id,
        "engine": publication_engine or recipe.engine,
        "recipeVersion": "v1",
        "recipeSpecHash": "sha256:" + ("cd" * 32),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "fixed-registry-hash"))
    monkeypatch.setattr(
        cp, "resolve_slideshow_recipe",
        resolve_slideshow if resolve_slideshow is not None else (lambda publication: recipe),
    )
    if not real_generation:
        monkeypatch.setattr(
            cp, "resolve_generation_recipe_with_reason",
            lambda publication: (
                resolve_generation(publication) if resolve_generation is not None else None,
                generation_reason,
            ),
        )
    monkeypatch.setattr(
        cp, "_dossier_source_recipe",
        resolve_source if resolve_source is not None else (lambda publication, refusal_reasons=None: None),
    )
    monkeypatch.setattr(cp, "syzygy_runtime_available", lambda: runtime_available)
    monkeypatch.setattr(cp, "resolve_material_profile", lambda publication, spec: object())

    def stub_load(profile, master_pages):
        if load_error is not None:
            raise load_error
        return library

    monkeypatch.setattr(cp, "load_syzygy_library", stub_load)


def test_capability_zero_entry_carries_zero_reason(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    # A library below the format minimum: plans are empty, the reason must be
    # named rather than a silent zero.
    small = ss.SyzygyLibrary(
        recipe.library_id, recipe.lane, recipe.subject, library.objects[:2],
        library.snapshot_hash,
    )
    _capability_scaffold(monkeypatch, tmp_path, recipe=recipe, library=small)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    entry = response["capabilities"][0]
    assert entry["maxQuantity"] == 0
    assert entry["zeroReason"] == reasons.LIBRARY_BELOW_MINIMUM


def test_capability_library_load_failure_lands_in_zero_plan_recipes(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        load_error=ss.SyzygyUnavailable("down"),
    )
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert response["capabilities"] == []
    assert response["zeroPlanRecipes"] == [{
        "recipeId": recipe.recipe_id,
        "engine": recipe.engine,
        "recipeVersion": "v1",
        "zeroReason": reasons.LIBRARY_LOAD_FAILED,
    }]


# ── MED-1: the explainer must never name a library mismatch on a non-empty plan
def test_explain_empty_slideshow_plan_never_reports_library_mismatch_for_a_nonempty_plan():
    recipe, library = _permutation_recipe_and_library()
    # The reviewer's count=0 probe: a non-empty plan names the caller's bug,
    # never "re-point the recipe".
    reason = reasons.explain_empty_slideshow_plan(recipe, library, 0, set())
    assert reason == reasons.PLAN_AVAILABLE
    assert reason != reasons.LIBRARY_MISMATCH


# ── MED-2: distinct zeroPlanRecipes codes ──────────────────────────────────
def test_slideshow_runtime_outage_lands_in_zero_plan_recipes(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        resolve_slideshow=lambda publication: None, runtime_available=False,
    )
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert response["capabilities"] == []
    assert response["zeroPlanRecipes"] == [{
        "recipeId": recipe.recipe_id, "engine": recipe.engine,
        "recipeVersion": "v1", "zeroReason": reasons.SLIDESHOW_RUNTIME_UNAVAILABLE,
    }]


def test_slideshow_recipe_unresolved_lands_in_zero_plan_recipes(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        resolve_slideshow=lambda publication: None, runtime_available=True,
    )
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert response["capabilities"] == []
    assert response["zeroPlanRecipes"] == [{
        "recipeId": recipe.recipe_id, "engine": recipe.engine,
        "recipeVersion": "v1", "zeroReason": reasons.RECIPE_UNRESOLVED,
    }]


def test_generation_recipe_unresolved_carries_detail_and_survives_the_cache(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    resolve_calls = {"n": 0}

    def resolve_generation(publication):
        resolve_calls["n"] += 1
        return None

    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        publication_engine="ai_video", publication_recipe_id="boat-lake:master",
        resolve_generation=resolve_generation, generation_reason="executor_version",
    )
    first = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    second = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert resolve_calls["n"] == 1, "the second call must be served from the cache"
    expected = [{
        "recipeId": "boat-lake:master", "engine": "ai_video",
        "recipeVersion": "v1", "zeroReason": reasons.RECIPE_UNRESOLVED,
        "detail": "executor_version",
    }]
    assert first["zeroPlanRecipes"] == expected
    assert second["zeroPlanRecipes"] == expected


def test_failing_ai_video_binding_resolves_once(monkeypatch, tmp_path, caplog):
    """LOW-R2-1: one failing ai_video binding runs the resolver (and logs the
    'registered dossier recipe unavailable' warning) exactly once per uncached
    poll, instead of once for resolve_generation_recipe and again for
    generation_recipe_unavailable_reason."""
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        publication_engine="ai_video", publication_recipe_id="boat-lake:master",
        real_generation=True,
    )
    import logging
    caplog.set_level(logging.WARNING)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    warnings = [
        r.getMessage() for r in caplog.records
        if "registered dossier recipe unavailable" in r.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert response["zeroPlanRecipes"] == [{
        "recipeId": "boat-lake:master", "engine": "ai_video",
        "recipeVersion": "v1", "zeroReason": reasons.RECIPE_UNRESOLVED,
        "detail": "typed_recipe_spec",
    }]


def test_source_identity_missing_lands_in_zero_plan_recipes(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    source_recipe = types.SimpleNamespace(
        max_quantity=0,
        masters=[types.SimpleNamespace(provenance={"sourceUrl": None})],
    )
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        publication_engine="sourced_video", publication_recipe_id="pov-dirt-bike:master",
        resolve_source=lambda publication, refusal_reasons=None: source_recipe,
    )
    monkeypatch.setattr(cp, "_source_dna_cut_ledger", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cp, "plan_source_cuts", lambda *a, **k: [])
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert response["capabilities"] == []
    assert response["zeroPlanRecipes"] == [{
        "recipeId": "pov-dirt-bike:master", "engine": "sourced_video",
        "recipeVersion": "v1", "zeroReason": reasons.SOURCE_IDENTITY_MISSING,
    }]


# ── MED-3a: source-level census — every continue follows a zeroPlanRecipes record
def _capabilities_bindings_loop(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "capabilities"
        ):
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.For)
                    and isinstance(child.target, ast.Tuple)
                    and [getattr(e, "id", None) for e in child.target.elts]
                        == ["_", "publication"]
                    and isinstance(child.iter, ast.Name)
                    and child.iter.id == "registered_bindings"
                ):
                    return child
    return None


def _is_zero_plan_append(stmt):
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == "append"
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "zero_plan_recipes"
    )


def _statement_blocks(stmt):
    for attr in ("body", "orelse", "finalbody"):
        value = getattr(stmt, attr, None)
        if isinstance(value, list):
            yield value
    for handler in getattr(stmt, "handlers", None) or []:
        if isinstance(getattr(handler, "body", None), list):
            yield handler.body
    for case in getattr(stmt, "cases", None) or []:
        if isinstance(getattr(case, "body", None), list):
            yield case.body


def _is_max_capabilities_cap_break(stmt):
    """The one allowed break: ``if len(entries) >= MAX_CAPABILITIES: break``."""
    if not (
        isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.Compare)
        and len(stmt.test.ops) == 1
        and len(stmt.test.comparators) == 1
        and isinstance(stmt.test.ops[0], ast.GtE)
        and isinstance(stmt.test.comparators[0], ast.Name)
        and stmt.test.comparators[0].id == "MAX_CAPABILITIES"
    ):
        return False
    left = stmt.test.left
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "len"
        and [getattr(a, "id", None) for a in left.args] == ["entries"]
    )


def _bindings_loop_violations(loop):
    """Violations inside the capabilities() bindings loop.

    Every ``continue`` must be immediately preceded by a zero_plan_recipes
    append; no ``break`` or ``return`` may appear except the MAX_CAPABILITIES
    cap break, which is allow-listed by the comparison it guards. ``match``
    case bodies are descended into so a case-level continue is caught.
    """
    bad = []

    def check(block):
        for index, stmt in enumerate(block):
            if isinstance(stmt, ast.If) and _is_max_capabilities_cap_break(stmt):
                # Allow the cap break; do not descend into its body.
                continue
            if isinstance(stmt, ast.Continue):
                previous = block[index - 1] if index > 0 else None
                if not _is_zero_plan_append(previous):
                    bad.append(f"continue@{getattr(stmt, 'lineno', '?')}")
            elif isinstance(stmt, (ast.Break, ast.Return)):
                bad.append(f"{type(stmt).__name__}@{getattr(stmt, 'lineno', '?')}")
            for sub in _statement_blocks(stmt):
                check(sub)

    check(list(loop.body))
    return bad


def test_served_entry_never_carries_a_zero_reason(monkeypatch, tmp_path):
    # A page with a resolvable recipe and a fresh library serves plans; the
    # served entries must not leak a zeroReason (a silent-zero inversion).
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(monkeypatch, tmp_path, recipe=recipe, library=library)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert response.get("zeroPlanRecipes", []) == []
    assert response["capabilities"], "a fresh permutation library must serve plans"
    for entry in response["capabilities"]:
        assert entry["maxQuantity"] > 0, entry
        assert "zeroReason" not in entry, entry


def test_every_continue_in_capabilities_loop_is_preceded_by_a_zero_plan_record():
    tree = ast.parse(inspect.getsource(cp))
    loop = _capabilities_bindings_loop(tree)
    assert loop is not None, "capabilities() registered_bindings loop not found"
    # Broadened from the original continue-only census: any break/return inside
    # the bindings loop (except the MAX_CAPABILITIES cap break) is also a
    # silent-zero hazard, and match-case bodies are descended into.
    assert _bindings_loop_violations(loop) == []


# ── MED-3d: executed zero -> refill for permutation, source-dna, generated
def test_lyric_edits_permutation_exhaustion_names_code_and_fresh_key_refills():
    recipe, library = _permutation_recipe_and_library(
        format_slug="lyric-edits", engine="lyrics_slideshows",
    )
    everything = {
        plan["signature"]
        for plan in ss.plan_slideshows(recipe, library, 10_000, set(), "run")
    }
    assert everything, "a 5-object lyric-edits library must offer candidates"
    assert ss.plan_slideshows(recipe, library, 1, everything, "run") == []
    assert reasons.explain_empty_slideshow_plan(recipe, library, 1, everything) \
        == reasons.SLIDESHOW_PERMUTATIONS_EXHAUSTED
    fresh = ss.SyzygyObject(
        "lyric/holy-fumble/photos/img-99.jpg", 9_999,
        "https://r2.example/lyric/holy-fumble/photos/img-99.jpg",
    )
    grown = ss.SyzygyLibrary(
        library.library_id, library.lane, library.subject,
        tuple(sorted((*library.objects, fresh), key=lambda o: o.key)),
        library.snapshot_hash,
    )
    assert ss.plan_slideshows(recipe, grown, 1, everything, "run"), (
        "a fresh key must produce a new lyric-edits permutation plan"
    )


def test_source_dna_exhaustion_is_terminal_and_a_fresh_master_refills(monkeypatch):
    import services.shipstream_source_manifest as source_manifest
    from tests import test_source_recut_variety as variety

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable("ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)

    recipe, master = variety._recipe(duration_ms=6_000)
    cuts, served, history = variety._cut_one_at_a_time(recipe, 100)
    assert len(cuts) == 9
    assert plan_source_cuts(recipe, 1, served, history=history) == []
    assert explain_empty_source_plan(recipe, served) == SOURCE_WINDOWS_EXHAUSTED
    # A fresh master opens never-cut windows: the source-DNA zero is terminal
    # per master, not per page.
    fresh_master = replace(master, sha256="a" * 64, duration_ms=6_000)
    grown = replace(recipe, masters=(fresh_master,))
    assert plan_source_cuts(grown, 1, served, history=history), (
        "a fresh master must refill the source-DNA plan space"
    )


def test_generated_prompt_space_zero_refills_when_inflight_jobs_finish():
    recipe = GenerationRecipe(
        recipe_id="boat-lake:master", format_slug="boat-lake", family_name="boat",
        engine="hailuo", provider_model="minimax/hailuo-2.3",
        engine_registry_hash="e" * 64, format_contract_version="sha256:" + "f" * 64,
        material_source="generated_video", asset_type="video/mp4",
        executor_version="sha256:" + "d" * 64, prompt_catalog_hash="c" * 64,
        family={
            "slots": {"scene": ["harbor", "dawn"], "shot": ["wide", "close"]},
            "template": "a {subject} at {scene} {shot}",
            "fixed_subject": "boat",
            "quality_guards": "",
        },
        provider_config={"cost_per_gen_usd": 0.05},
        recipe_spec={"production": {"variationValues": {}}},
    )
    page_id = "acct:guard-page"
    store = {"jobs": {}}
    assert cp._generated_capability_quantity(store, recipe, page_id, {"contentNiche": "OTHER"}) > 0
    space = prompt_combination_space(recipe)
    every = plan_prompt_combinations(recipe, "reserve-all", space, set())
    assert len(every) == space
    store["jobs"]["inflight"] = {
        "jobId": "inflight", "pageId": page_id, "sourceKind": "generated",
        "status": "queued", "promptPlan": every, "clips": [],
    }
    assert cp._generated_capability_quantity(store, recipe, page_id, {"contentNiche": "OTHER"}) == 0
    store["jobs"]["inflight"]["status"] = "completed"
    assert cp._generated_capability_quantity(store, recipe, page_id, {"contentNiche": "OTHER"}) > 0


# ── Wire contract: by default the response fits the deployed Worker ──────────
# The Worker (contentLabClient.js) rejects any capability entry field and any
# top-level field it does not allow-list, and then fails the whole page
# (10-28 outage). The lists live in one shared fixture; the Lab's own
# projection constant must match it.
_CONTRACT_PATH = Path(__file__).parent / "fixtures" / "worker_capabilities_contract.json"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text())
WORKER_CAPABILITY_ENTRY_FIELDS = frozenset(_CONTRACT["capabilityEntryAllowedFields"])
WORKER_CAPABILITIES_TOP_LEVEL_FIELDS = frozenset(_CONTRACT["capabilitiesTopLevelAllowedFields"])


def _assert_fits_worker(response):
    assert set(response) <= WORKER_CAPABILITIES_TOP_LEVEL_FIELDS, sorted(response)
    for entry in response["capabilities"]:
        assert set(entry) <= WORKER_CAPABILITY_ENTRY_FIELDS, entry


def _flag_off(monkeypatch):
    monkeypatch.delenv(cp.ADVERTISE_ZERO_REASONS_ENV, raising=False)
    monkeypatch.delenv(cp.ADVERTISE_CONSTRAINTS_ENV, raising=False)


def test_lab_projection_list_matches_the_pinned_worker_contract():
    assert cp.WORKER_CAPABILITY_ENTRY_FIELDS == WORKER_CAPABILITY_ENTRY_FIELDS


def test_default_capabilities_response_fits_the_worker_allow_lists(monkeypatch):
    _flag_off(monkeypatch)
    entries = [
        {"recipeId": "r-zero", "engine": "slideshow", "recipeVersion": "v1", "maxQuantity": 0,
         "zeroReason": reasons.LIBRARY_BELOW_MINIMUM},
        {"recipeId": "r-ok", "engine": "slideshow", "recipeVersion": "v1", "maxQuantity": 3},
    ]
    zero_rows = [{"engine": "slideshow", "zeroReason": reasons.LIBRARY_LOAD_FAILED}]
    response = cp._capabilities_response(entries, zero_rows)
    _assert_fits_worker(response)
    # the caller's (cached) entries are not mutated
    assert entries[0]["zeroReason"] == reasons.LIBRARY_BELOW_MINIMUM


def test_an_unknown_entry_field_never_reaches_the_worker(monkeypatch):
    # Allow-list, not deny-list: tomorrow's new field is dropped with the flag
    # off AND on, until someone widens the contract on purpose.
    entry = {"recipeId": "r", "engine": "slideshow", "recipeVersion": "v1", "maxQuantity": 0,
             "zeroReason": reasons.LIBRARY_BELOW_MINIMUM, "futureField": 1}
    _flag_off(monkeypatch)
    off = cp._capabilities_response([entry])
    _assert_fits_worker(off)
    monkeypatch.setenv(cp.ADVERTISE_ZERO_REASONS_ENV, "1")
    on = cp._capabilities_response([entry])
    assert "futureField" not in on["capabilities"][0]
    assert on["capabilities"][0]["zeroReason"] == reasons.LIBRARY_BELOW_MINIMUM


def test_zero_reason_fields_go_on_the_wire_only_with_the_flag(monkeypatch):
    monkeypatch.setenv(cp.ADVERTISE_ZERO_REASONS_ENV, "1")
    entries = [{"recipeId": "r-zero", "engine": "slideshow", "recipeVersion": "v1", "maxQuantity": 0,
                "zeroReason": reasons.LIBRARY_BELOW_MINIMUM}]
    zero_rows = [{"engine": "slideshow", "zeroReason": reasons.LIBRARY_LOAD_FAILED}]
    response = cp._capabilities_response(entries, zero_rows)
    assert response["capabilities"][0]["zeroReason"] == reasons.LIBRARY_BELOW_MINIMUM
    assert response["zeroPlanRecipes"] == zero_rows


# ── Endpoint level: the real capabilities() output, flag off, every path ────
def test_capabilities_endpoint_fresh_zero_entry_fits_the_worker(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    small = ss.SyzygyLibrary(
        recipe.library_id, recipe.lane, recipe.subject, library.objects[:2],
        library.snapshot_hash,
    )
    _capability_scaffold(monkeypatch, tmp_path, recipe=recipe, library=small)
    _flag_off(monkeypatch)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    _assert_fits_worker(response)
    assert response["capabilities"][0]["maxQuantity"] == 0


def test_capabilities_endpoint_skipped_recipe_fits_the_worker(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        load_error=ss.SyzygyUnavailable("down"),
    )
    _flag_off(monkeypatch)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    _assert_fits_worker(response)
    assert set(response) == {"schema", "capabilities"}


def test_capabilities_endpoint_cached_path_fits_the_worker_and_keeps_reasons(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    resolve_calls = {"n": 0}

    def resolve_generation(publication):
        resolve_calls["n"] += 1
        return None

    _capability_scaffold(
        monkeypatch, tmp_path, recipe=recipe, library=library,
        publication_engine="ai_video", publication_recipe_id="boat-lake:master",
        resolve_generation=resolve_generation, generation_reason="executor_version",
    )
    _flag_off(monkeypatch)
    first = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    second = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert resolve_calls["n"] == 1, "the second call must be served from the cache"
    _assert_fits_worker(first)
    _assert_fits_worker(second)
    # The cache kept the reasons: turning the flag on serves them from it.
    monkeypatch.setenv(cp.ADVERTISE_ZERO_REASONS_ENV, "1")
    third = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    assert resolve_calls["n"] == 1
    assert third["zeroPlanRecipes"][0]["zeroReason"] == reasons.RECIPE_UNRESOLVED


def test_capabilities_endpoint_no_intent_path_fits_the_worker(monkeypatch, tmp_path):
    recipe, library = _permutation_recipe_and_library()
    _capability_scaffold(monkeypatch, tmp_path, recipe=recipe, library=library)
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: None)
    _flag_off(monkeypatch)
    response = cp.capabilities(x_rt_page_id="acct:guard-page", x_page_id=None)
    _assert_fits_worker(response)
    assert response["capabilities"] == []
