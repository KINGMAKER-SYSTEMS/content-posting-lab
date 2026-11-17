"""Burnt must never be offered a re-chop that re-shows footage it already used."""
import hashlib
import math

from services import control_plane_slideshows as slides
from services.control_plane_plan_reasons import CURATED_VARIANTS_EXHAUSTED, explain_empty_slideshow_plan

BURNT = "acct:burnt-mill-rd"


def burnt_recipe_and_library():
    curated = slides.ugc_page_treatment(BURNT)["curatedVideoPlan"]
    raw = slides.UGC_EXECUTOR_PATH.read_bytes()
    recipe = slides.SlideshowRecipe(
        "ugc-slideshow:master", "ugc-slideshow", "sourced_slideshow", "e" * 64,
        "sha256:" + "f" * 64, "image_library", "video/mp4",
        "sha256:" + hashlib.sha256(raw).hexdigest(), "syzygy:slideshow-webweaving:burnt-mill-rd",
        "slideshow-webweaving", "burnt-mill-rd", 3,
        (slides.SyzygyTemplate("curated-video-montage", 3),), 1, 1080, 1920,
        "tiktok_delivery_v1", {"masterPages": {"pageId": BURNT}},
    )
    objects = tuple(slides.SyzygyObject(
        f"slideshow-webweaving/burnt-mill-rd/clips/{source['file']}", source["bytes"], "https://example.invalid/x",
    ) for source in curated["sources"])
    return recipe, slides.SyzygyLibrary(recipe.library_id, recipe.lane, recipe.subject, objects, "e" * 64)


def spans(plans):
    used = {}
    for plan in plans:
        for w in plan["windows"]:
            used.setdefault(w["key"], []).append((w["sourceStart"], w["sourceEnd"]))
    return used


def test_used_footage_and_same_job_repeats_are_never_offered():
    recipe, library = burnt_recipe_and_library()
    # Old behaviour, kept as the red case: signature-only filtering offers
    # neighbouring variants that re-show the same seconds of the same clips.
    raw = [p for p in slides.curated_plans_for_recipe(recipe, library)]
    assert slides.curated_plan_repeats_footage(raw[1], spans([raw[0]]))

    assert BURNT in slides.CURATED_FRESH_FOOTAGE_PAGES
    first, second = slides.plan_slideshows(recipe, library, 2, set(), "job-a", used_windows={})
    assert not slides.curated_plan_repeats_footage(second, spans([first]))

    posted = spans([first, second])
    later = slides.plan_slideshows(recipe, library, 3, {first["signature"], second["signature"]},
                                   "job-b", used_windows=posted)
    assert later and all(not slides.curated_plan_repeats_footage(p, posted) for p in later)

    every_clip = {obj.key: [(0.0, math.inf)] for obj in library.objects}
    assert slides.plan_slideshows(recipe, library, 1, set(), "job-c", used_windows=every_clip) == []
    assert explain_empty_slideshow_plan(recipe, library, 1, set(), used_windows=every_clip) == CURATED_VARIANTS_EXHAUSTED
