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


@pytest.mark.parametrize("filename,key,expected", [
    ("content-engine-registry.v1.json", "profiles",
     "1e1179e56de576a360aa2cdb1592304c387bc7878c9087560bdbe0c68cf94f44"),
    ("content-format-contracts.v1.json", "contracts",
     "b69353fefd9ebca4e2464df76714f91dc64a1f7f379a2212c0dbfec90a17c186"),
])
def test_legacy_authority_maps_match_reviewed_burnt_retirement(filename, key, expected):
    # #315 deliberately changed the UGC authority for Burnt's own footage.
    # Pin the resulting legacy maps, rather than deleting their drift guard
    # or restoring the retired lyric page. The four additive formats retain
    # their separate contract tests, as in the original legacy-map guard.
    root = Path(__file__).resolve().parents[1]
    current = json.loads((root / "recipes" / filename).read_bytes())[key]
    for slug in ("lyric-video", "image-video", "single-image-video", "meme"):
        current.pop(slug)
    encoded = json.dumps(current, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    assert hashlib.sha256(encoded).hexdigest() == expected


def test_static_renderer_retains_reviewed_768p_pin():
    # #282 changed the upright minimum to 768p. #315's recipe retirement and
    # #328's caption-module change did not change this prepared renderer.
    root = Path(__file__).resolve().parents[1]
    assert hashlib.sha256((root / "services/post_render.py").read_bytes()).hexdigest() == (
        "017c619ab1b58df982b8a9ec908110c18f8c5ee7363d5c0d5f528af5d7c380ab"
    )
