"""Burnt's retired lyric lane must not regain access to footage."""
import hashlib
import json
from pathlib import Path

import pytest

from services import control_plane_slideshows as slides
from services.content_engine_registry import load_engine_registry
from services.control_plane_lyric_video import validate_lyric_recipe, validate_lyric_wire_plan

PAGE = "acct:burnt-mill-rd"
ACCOUNT = "burnt.mill.rd"


def spec():
    return {
        "masterPages": {"pageId": PAGE, "handle": ACCOUNT,
                        "notionPageId": "3451465b-b829-80a8-b9d1-fb7e34b98ff5",
                        "automationMode": "Automation", "archived": False},
        "production": {"controls": {"lyricTimingSetId": "burnt-heavy-foot-chorus-v1"}},
        "renderTreatment": {"filters": {"brightness": .85}, "clipSpeed": 1},
    }


def recipe_and_library():
    """Compatibility fixture for curated-plan tests after lyric retirement.

    The callers exercise the generic curated planner and runner mechanics.
    They need Burnt's reviewed footage, but must not make the retired lyric
    executor reachable.  Model that footage through the live UGC authority.
    """
    raw = slides.UGC_EXECUTOR_PATH.read_bytes()
    config = json.loads(raw)["pageTreatments"][PAGE]["curatedVideoPlan"]
    objects = tuple(
        slides.SyzygyObject(
            f"slideshow-webweaving/burnt-mill-rd/clips/{source['file']}",
            source["bytes"], "https://example.invalid/unused",
        )
        for source in config["sources"]
    )
    library = slides.SyzygyLibrary(
        "syzygy:slideshow-webweaving:burnt-mill-rd",
        "slideshow-webweaving", "burnt-mill-rd", objects, "e" * 64,
    )
    recipe = slides.SlideshowRecipe(
        "ugc-slideshow:master", "ugc-slideshow", "sourced_slideshow",
        "a" * 64, "sha256:" + "b" * 64, "image_library", "video/mp4",
        "sha256:" + hashlib.sha256(raw).hexdigest(), library.library_id,
        library.lane, library.subject, 20,
        (slides.SyzygyTemplate("curated-video-montage", 20),), 1, 1080, 1920,
        "tiktok_delivery_v1", {
            "masterPages": {"pageId": PAGE},
            "renderTreatment": {
                "filters": {"brightness": .85, "contrast": 1, "saturation": 1,
                            "warmth": 0, "fade": 0, "grain": 0, "vignette": 0},
                "clipSpeed": 1, "clipCrop": {"zoom": 1, "focusX": .5, "focusY": .5},
            },
        },
    )
    return recipe, library


def test_burnt_lyric_recipe_has_named_static_caption_refusal():
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_recipe(spec())


def test_burnt_lyric_planner_and_wire_cannot_reach_legacy_footage():
    raw = json.loads(slides.LYRIC_VIDEO_EXECUTOR_PATH.read_text())
    assert PAGE not in raw["pageTreatments"]
    assert slides.ugc_page_treatment(PAGE, "lyric-video") is None
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        validate_lyric_wire_plan({}, PAGE)


def test_lyric_executor_and_timing_authority_remain_loadable_after_retirement():
    profiles, _ = load_engine_registry()
    _, digest = slides._executor_contract("lyric-video")
    assert profiles["lyric-video"].executor_version == f"sha256:{digest}"
    from services.lyric_timing import lyric_timing_catalog
    timing = lyric_timing_catalog(page_id=PAGE, account=ACCOUNT)[0]
    assert timing["sourceProgramSha256"] == hashlib.sha256(slides.LYRIC_VIDEO_EXECUTOR_PATH.read_bytes()).hexdigest()
