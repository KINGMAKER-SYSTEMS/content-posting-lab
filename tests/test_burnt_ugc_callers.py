"""Real Lab callers for Burnt's temporary caption-led UGC format.

Test-only fixtures; network submission is captured, never sent.
Run beside the unchanged test_control_plane_ugc_slideshow.py suite.
"""

from dataclasses import replace
import hashlib
import json

import services.control_plane_slideshows as slideshows
from services.content_engine_registry import resolve_material_profile
from services.control_plane_slideshows import (
    LYRIC_VIDEO_EXECUTOR_PATH,
    UGC_EXECUTOR_PATH,
    SlideshowRecipe,
    SyzygyLibrary,
    SyzygyObject,
    plan_slideshows,
    slideshow_library_address,
    ugc_page_treatment,
)

PAGE = "acct:burnt-mill-rd"
LIBRARY = "syzygy:slideshow-webweaving:burnt-mill-rd"


def _recipe_and_library():
    treatment = ugc_page_treatment(PAGE)
    assert treatment is not None, "Burnt needs its own UGC treatment"
    curated = treatment["curatedVideoPlan"]
    objects = tuple(
        SyzygyObject(
            f"slideshow-webweaving/burnt-mill-rd/clips/{source['file']}",
            source["bytes"],
            f"https://media.example.invalid/{source['file']}",
        )
        for source in curated["sources"]
    )
    snapshot = "\n".join(f"{item.key}:{item.bytes}" for item in objects)
    library = SyzygyLibrary(
        library_id=LIBRARY,
        lane="slideshow-webweaving",
        subject="burnt-mill-rd",
        objects=objects,
        snapshot_hash=hashlib.sha256(snapshot.encode()).hexdigest(),
    )
    recipe = SlideshowRecipe(
        recipe_id="ugc-slideshow:master",
        format_slug="ugc-slideshow",
        engine="sourced_slideshow",
        engine_registry_hash="e" * 64,
        format_contract_version="sha256:" + "f" * 64,
        material_source="image_library",
        asset_type="video/mp4",
        executor_version="sha256:" + hashlib.sha256(UGC_EXECUTOR_PATH.read_bytes()).hexdigest(),
        library_id=LIBRARY,
        lane="slideshow-webweaving",
        subject="burnt-mill-rd",
        minimum_library_items=3,
        templates=(slideshows.SyzygyTemplate("clean-slideshow", 3),),
        max_quantity=1,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={"masterPages": {"pageId": PAGE}},
    )
    return recipe, library


def test_burnt_real_resolver_selects_ugc_and_own_clip_library():
    profile = resolve_material_profile(
        {"recipeId": "ugc-slideshow:master", "engine": "sourced_slideshow"},
        {
            "masterPages": {
                "pageId": PAGE,
                "contentNiche": "slideshow",
                "contentEngine": "sourced_slideshow",
            },
            "demand": {"formatMix": {"ugc-slideshow": 1.0}},
        },
    )
    assert profile is not None
    assert profile.format_slug == "ugc-slideshow"
    assert slideshow_library_address(
        profile, {"pageId": PAGE, "handle": "burnt.mill.rd"}
    ) == (LIBRARY, "slideshow-webweaving", "burnt-mill-rd")


def test_burnt_real_planner_preserves_approved_twenty_clip_twenty_seven_window_plan():
    recipe, library = _recipe_and_library()
    plans = plan_slideshows(recipe, library, 1, set(), "burnt-caller-test")
    assert len(plans) == 1
    plan = plans[0]
    assert plan["templateSlug"] == "curated-video-montage"
    assert len(plan["mediaKeys"]) == len(set(plan["mediaKeys"])) == 20
    assert all(key.startswith("slideshow-webweaving/burnt-mill-rd/clips/") for key in plan["mediaKeys"])
    assert len(plan["windows"]) == 27
    approved = json.loads(LYRIC_VIDEO_EXECUTOR_PATH.read_text())["pageTreatments"][PAGE]["curatedVideoPlan"]
    assert ugc_page_treatment(PAGE)["curatedVideoPlan"] == approved
    assert plan["windows"][0]["outputStart"] == 0
    assert plan["windows"][-1]["outputEnd"] == 20


def test_burnt_real_planner_refuses_missing_required_clip_instead_of_generic_fallback():
    recipe, library = _recipe_and_library()
    assert plan_slideshows(
        recipe, replace(library, objects=library.objects[1:]), 1, set(), "missing-clip"
    ) == []


def test_burnt_real_planner_refuses_changed_source_bytes():
    recipe, library = _recipe_and_library()
    changed = replace(library.objects[0], bytes=library.objects[0].bytes + 1)
    assert plan_slideshows(
        recipe, replace(library, objects=(changed, *library.objects[1:])), 1, set(), "changed-bytes"
    ) == []


def test_burnt_real_planner_refuses_another_pages_library():
    recipe, library = _recipe_and_library()
    foreign = replace(
        library,
        library_id="syzygy:slideshow-webweaving:other-page",
        subject="other-page",
        objects=tuple(
            replace(item, key=item.key.replace("/burnt-mill-rd/", "/other-page/"))
            for item in library.objects
        ),
    )
    assert plan_slideshows(recipe, foreign, 1, set(), "foreign-library") == []


async def test_burnt_real_wire_has_27_muted_video_windows_and_no_baked_lyrics(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None, timeout=60.0):
        captured["body"] = body
        return {"job_id": "test-only-burnt-caller", "quality": "draft"}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    recipe, library = _recipe_and_library()
    plans = plan_slideshows(recipe, library, 1, set(), "burnt-wire-test")
    assert len(plans) == 1
    await slideshows.submit_syzygy_render(
        plans[0], page_id=PAGE, recipe_version="test-only", format_slug="ugc-slideshow"
    )
    spec = captured["body"]["spec"]
    assert spec["format"] == "video"
    assert len(spec["media"]) == 27
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["lyrics"] == []
    assert spec["uses_original_audio"] is False
    assert "audio" not in spec
    assert "template_slug" not in spec
    assert "cuts" not in spec
    assert all(window["type"] == "video" and window["muted"] is True for window in spec["media"])
    assert all(window["library"].startswith("slideshow-webweaving/burnt-mill-rd/clips/") for window in spec["media"])
    assert all(window.get("transition") == {"type": "cut", "duration": 0} for window in spec["media"])
