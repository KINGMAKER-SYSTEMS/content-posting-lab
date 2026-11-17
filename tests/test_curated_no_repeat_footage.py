"""Burnt must never be offered a re-chop that re-shows footage it already used."""
import hashlib
import math

import pytest

from routers import control_plane as cp
from services import control_plane_slideshows as slides
from services.control_plane_plan_reasons import (
    CURATED_HISTORY_UNREADABLE,
    CURATED_VARIANTS_EXHAUSTED,
    explain_empty_slideshow_plan,
    reason_for,
)

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


def _store(recipe, active=None, archived=None):
    store = {"jobs": {}}
    if active is not None:
        store["jobs"]["job-1"] = {
            "sourceKind": "syzygy_slideshow", "pageId": BURNT, "sourceLibraryId": recipe.library_id,
            "status": "queued", "slideshowPlan": [active],
        }
    if archived is not None:
        store["archiveIndex"] = {"slideshow": archived}
    return store


def _archived(recipe, **fields):
    return {"pageId": BURNT, "sourceLibraryId": recipe.library_id, "signature": "a" * 64, **fields}


def test_readable_history_counts_exact_windows_or_whole_clips():
    recipe, library = burnt_recipe_and_library()
    first = slides.curated_plans_for_recipe(recipe, library)[0]
    used = cp._curated_used_windows(_store(recipe, active=first), recipe, BURNT)
    assert used == spans([first])
    key = first["windows"][0]["key"]
    used = cp._curated_used_windows(_store(recipe, archived=[_archived(recipe, mediaKeys=[key])]), recipe, BURNT)
    assert used == {key: [(0.0, math.inf)]}
    # Other pages keep signature-only reservation, even with broken history.
    broken = _store(recipe, archived=[_archived(recipe, pageId="acct:culture-core")])
    assert cp._curated_used_windows(broken, recipe, "acct:culture-core") is None


def _window(**change):
    return [{"key": "slideshow-webweaving/burnt-mill-rd/clips/a.mp4", "sourceStart": 1.0, "sourceEnd": 2.0, **change}]


@pytest.mark.parametrize("entry", [
    {},                                    # only a signature: footage unknown
    {"mediaKeys": "bmr-v-0001.mp4"},       # string, not a list of keys
    {"mediaKeys": []},
    {"mediaKeys": 7},
    {"mediaKeys": ["ok", 3]},
    {"mediaKeys": ["ok", ""]},
    {"windows": _window(sourceStart=float("nan"))},
    {"windows": _window(sourceEnd=float("inf"))},
    {"windows": _window(sourceStart=3.0)},  # end before start
    {"windows": _window(sourceStart=-1.0)},
    {"windows": _window(sourceStart=True)},
    {"windows": _window(key="")},
    {"windows": "1-2"},
])
def test_unreadable_burnt_history_fails_closed_with_a_typed_reason(entry):
    recipe, library = burnt_recipe_and_library()
    used = cp._curated_used_windows(_store(recipe, archived=[_archived(recipe, **entry)]), recipe, BURNT)
    assert used is slides.UNREADABLE_CURATED_HISTORY
    assert slides.plan_slideshows(recipe, library, 1, set(), "job-x", used_windows=used) == []
    assert explain_empty_slideshow_plan(recipe, library, 1, set(), used_windows=used) == CURATED_HISTORY_UNREADABLE
    assert reason_for(CURATED_HISTORY_UNREADABLE)[1] is False


@pytest.mark.parametrize("store", [
    {"jobs": {}, "archiveIndex": {"slideshow": "not-a-list"}},
    {"jobs": {}, "archiveIndex": {"slideshow": ["not-a-dict"]}},
    {"jobs": {}, "archiveIndex": ["not-a-dict"]},
])
def test_malformed_archive_index_fails_closed(store):
    recipe, _ = burnt_recipe_and_library()
    assert cp._curated_used_windows(store, recipe, BURNT) is slides.UNREADABLE_CURATED_HISTORY


def test_unreadable_active_plan_fails_closed():
    recipe, _ = burnt_recipe_and_library()
    store = _store(recipe, active={"signature": "b" * 64})
    assert cp._curated_used_windows(store, recipe, BURNT) is slides.UNREADABLE_CURATED_HISTORY
