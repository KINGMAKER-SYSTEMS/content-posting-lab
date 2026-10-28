"""One home for every zero-plan reason code across the Lab planners.

A zero-plan answer is only actionable if the Worker can show WHY. This module
owns the single table that maps each reason code to its planner family, whether
the zero is terminal by design (adding fresh media cannot produce a new plan),
and the plain-words remedy.

Source-DNA reuses its existing codes (imported, never renamed); the ai_video
generated path uses ``prompt_inventory_exhausted``, which is also the 409 detail
at job creation. The slideshow planner's ``explain_empty_slideshow_plan`` is the
one new decision function and is called ONLY after ``plan_slideshows`` returned
an empty list, so the planning hot path and every existing plan/signature stay
byte-identical.
"""

from __future__ import annotations

from typing import Any

from services.control_plane_sources import (
    SOURCE_MASTER_TOO_SHORT,
    SOURCE_WINDOWS_EXHAUSTED,
    SOURCE_WINDOWS_RESERVED_ELSEWHERE,
)

# Slideshow / generated / registry zero-plan reasons. None of these collide
# with the source-DNA codes above; the Worker surfaces the code verbatim.
LIBRARY_MISMATCH = "library_mismatch"
LIBRARY_BELOW_MINIMUM = "library_below_minimum"
TEMPLATE_MEDIA_EXCEEDS_LIBRARY = "template_media_exceeds_library"
SLIDESHOW_PERMUTATIONS_EXHAUSTED = "slideshow_permutations_exhausted"
GENERIC_MEDIA_EXHAUSTED = "generic_media_exhausted"
CURATED_LIBRARY_NOT_READY = "curated_library_not_ready"
CURATED_VARIANTS_EXHAUSTED = "curated_variants_exhausted"
PHOTO_PLAN_UNAVAILABLE = "photo_plan_unavailable"
PHOTO_VARIANTS_EXHAUSTED = "photo_variants_exhausted"
PROMPT_INVENTORY_EXHAUSTED = "prompt_inventory_exhausted"
LIBRARY_LOAD_FAILED = "library_load_failed"
MATERIAL_PROFILE_MISSING = "material_profile_missing"
# A plan came back short but not empty (a reservation race won between the
# capability poll and job creation): retry later or add media, never a
# "re-point the recipe" library diagnosis.
INSUFFICIENT_INVENTORY = "insufficient_inventory"
# The explainer was called on a non-empty plan: its own bug guard, never a
# library diagnosis.
PLAN_AVAILABLE = "plan_available"
# The closed photo-hold page's pinned base photos are gone from the live
# library (bytes changed or missing): restore them, not "re-point the recipe".
PINNED_PHOTOS_MISSING = "pinned_photos_missing"
# The Syzygy slideshow runtime is not configured: an outage, not a registry
# or recipe problem.
SLIDESHOW_RUNTIME_UNAVAILABLE = "slideshow_runtime_unavailable"
# The binding did not resolve to a runnable recipe for any other reason
# (contract drift / generation resolve failures). Carries the Lab's exact
# reason= string as "detail" where one exists.
RECIPE_UNRESOLVED = "recipe_unresolved"
# A source master has no canonical HTTPS source identity.
SOURCE_IDENTITY_MISSING = "source_identity_missing"


# code -> (family, terminal_by_design, remedy in plain words).
#
# terminal_by_design is True exactly when fresh media cannot produce a new
# plan: the curated montage is pinned to its own sources, source windows are
# pinned to a master, a non-photo page stays non-photo, and the generated
# prompt space is Lab capacity rather than a page lever.
REASONS: dict[str, tuple[str, bool, str]] = {
    LIBRARY_MISMATCH: (
        "slideshow", False,
        "Re-point the recipe so its live library matches the page's address.",
    ),
    LIBRARY_BELOW_MINIMUM: (
        "slideshow", False,
        "Add media to the page library to meet the format minimum.",
    ),
    TEMPLATE_MEDIA_EXCEEDS_LIBRARY: (
        "slideshow", False,
        "Add media to the page library to satisfy the template's media count.",
    ),
    SLIDESHOW_PERMUTATIONS_EXHAUSTED: (
        "slideshow", False,
        "Add fresh media; new keys create new stride/start permutations.",
    ),
    GENERIC_MEDIA_EXHAUSTED: (
        "slideshow", False,
        "Add fresh media; the next unreserved chunk will be offered.",
    ),
    CURATED_LIBRARY_NOT_READY: (
        "slideshow", False,
        "Restore the pinned clips (exact bytes) to the curated library.",
    ),
    CURATED_VARIANTS_EXHAUSTED: (
        "slideshow", True,
        "The approved montage and every re-chop variant are reserved; a curated page is terminal by design.",
    ),
    PHOTO_PLAN_UNAVAILABLE: (
        "slideshow", True,
        "The page is not a closed photo-hold page; a non-photo page stays non-photo.",
    ),
    PHOTO_VARIANTS_EXHAUSTED: (
        "slideshow", False,
        "Add fresh photos to the page library; new photo keys create new variants.",
    ),
    PINNED_PHOTOS_MISSING: (
        "slideshow", False,
        "Restore the pinned base photos (exact bytes) to the page library.",
    ),
    PROMPT_INVENTORY_EXHAUSTED: (
        "generated", False,
        "Every fresh prompt combination is in flight; clears when those jobs finish.",
    ),
    LIBRARY_LOAD_FAILED: (
        "library", False,
        "The live library could not be read; retry, then restore the library.",
    ),
    MATERIAL_PROFILE_MISSING: (
        "registry", False,
        "The recipe resolves to no commissioned material profile; fix the registry or recipe.",
    ),
    SLIDESHOW_RUNTIME_UNAVAILABLE: (
        "library", False,
        "The Syzygy slideshow runtime is not configured; restore it.",
    ),
    RECIPE_UNRESOLVED: (
        "registry", False,
        "The binding did not resolve to a runnable recipe; the Lab log line 'registered dossier recipe unavailable … reason=' names the cause.",
    ),
    SOURCE_IDENTITY_MISSING: (
        "source-dna", False,
        "A source master has no canonical HTTPS source identity; restore the master provenance.",
    ),
    INSUFFICIENT_INVENTORY: (
        "slideshow", False,
        "Fewer fresh plans than asked; retry or add media.",
    ),
    PLAN_AVAILABLE: (
        "slideshow", False,
        "A fresh plan is still available; the explainer was called on a non-empty plan.",
    ),
    SOURCE_MASTER_TOO_SHORT: (
        "source-dna", True,
        "No master fits even the shortest allowed cut; add a longer master.",
    ),
    SOURCE_WINDOWS_EXHAUSTED: (
        "source-dna", True,
        "Every window of every master is cut; add a new master.",
    ),
    SOURCE_WINDOWS_RESERVED_ELSEWHERE: (
        "source-dna", False,
        "Never-cut windows remain but other pages reserved them; retry later.",
    ),
}


def reason_for(code: str) -> tuple[str, bool, str]:
    """(family, terminal_by_design, remedy) for a reason code."""
    return REASONS[code]


def explain_empty_slideshow_plan(
    recipe: Any,
    library: Any,
    count: int,
    unavailable_signatures: set[str],
) -> str:
    """Name why plan_slideshows returned nothing for a slideshow recipe.

    Call this ONLY when plan_slideshows(recipe, library, count,
    unavailable_signatures, ...) came back []. It re-derives the same decision
    tree without touching the planning hot path, so every existing plan and
    signature stays byte-identical. The invalid-argument guard of
    plan_slideshows is not re-checked here: a caller that reaches this function
    already used the same validated inputs. A short-but-non-empty plan is the
    caller's job (INSUFFICIENT_INVENTORY), not this function's: every
    "a plan still exists" fall-through returns PLAN_AVAILABLE so a broken call
    is never reported as a library mismatch.
    """
    from services.control_plane_image_video import (
        IMAGE_VIDEO_FORMATS,
        recipe_image_video_plans,
    )
    from services.control_plane_slideshows import (
        CLEAN_VIDEO_FORMATS,
        _generic_clean_video_chunk,
        _permutation_plan_candidates,
        curated_plans_for_recipe,
        requires_curated_video_plan,
    )

    if library.library_id != recipe.library_id:
        return LIBRARY_MISMATCH
    if len(library.objects) < recipe.minimum_library_items:
        return LIBRARY_BELOW_MINIMUM

    if recipe.format_slug in IMAGE_VIDEO_FORMATS:
        plans = recipe_image_video_plans(recipe, library)
        if plans is None:
            return PHOTO_PLAN_UNAVAILABLE
        if not plans:
            # The closed plan's pinned base photos are gone from the live
            # library (bytes changed or missing), not variant exhaustion.
            return PINNED_PHOTOS_MISSING
        if all(plan["signature"] in unavailable_signatures for plan in plans):
            return PHOTO_VARIANTS_EXHAUSTED
        return PLAN_AVAILABLE

    if recipe.format_slug in CLEAN_VIDEO_FORMATS:
        has_curated_plan = requires_curated_video_plan(recipe)
        variants = curated_plans_for_recipe(recipe, library)
        if has_curated_plan and not variants:
            return CURATED_LIBRARY_NOT_READY
        if variants:
            if all(plan["signature"] in unavailable_signatures for plan in variants):
                return CURATED_VARIANTS_EXHAUSTED
            return PLAN_AVAILABLE
        template = recipe.templates[0]
        objects_in_order = list(library.objects)
        for chunk in range(len(objects_in_order) + 1):
            plan = _generic_clean_video_chunk(template, objects_in_order, chunk)
            if plan is None:
                if chunk == 0:
                    return TEMPLATE_MEDIA_EXCEEDS_LIBRARY
                return GENERIC_MEDIA_EXHAUSTED
            if plan["signature"] in unavailable_signatures:
                continue
            return PLAN_AVAILABLE
        return GENERIC_MEDIA_EXHAUSTED

    # Stride/start permutation slideshow family.
    objects = list(library.objects)
    if not any(template.media_count <= len(objects) for template in recipe.templates):
        return TEMPLATE_MEDIA_EXCEEDS_LIBRARY
    if _permutation_plan_candidates(recipe, library, unavailable_signatures):
        return PLAN_AVAILABLE
    return SLIDESHOW_PERMUTATIONS_EXHAUSTED
