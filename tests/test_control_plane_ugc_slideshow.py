"""Culture Core UGC slideshow: curated ordered plan, blank text/no audio, and
the page-scoped fast-cut wire — without touching legacy lyric/meme hashes."""

import hashlib

import pytest

import services.control_plane_slideshows as slideshows
from services.content_engine_registry import resolve_material_profile
from services.control_plane_slideshows import (
    EXECUTOR_PATH,
    UGC_EXECUTOR_PATH,
    SyzygyLibrary,
    SyzygyObject,
    SyzygyTemplate,
    SlideshowRecipe,
    plan_slideshows,
    slideshow_library_address,
    ugc_page_cut_policy,
)

ORIGINAL_EXECUTOR_SHA = (
    "485feda76c885d7aab19be256f61c749edcce19417ebc560d265ba92b60974ad"
)


def _ugc_recipe() -> SlideshowRecipe:
    ugc_sha = hashlib.sha256(UGC_EXECUTOR_PATH.read_bytes()).hexdigest()
    return SlideshowRecipe(
        recipe_id="ugc-slideshow:master",
        format_slug="ugc-slideshow",
        engine="sourced_slideshow",
        engine_registry_hash="e" * 64,
        format_contract_version="sha256:" + "f" * 64,
        material_source="image_library",
        asset_type="video/mp4",
        executor_version=f"sha256:{ugc_sha}",
        library_id="syzygy:ugc:french-core",
        lane="ugc",
        subject="french-core",
        minimum_library_items=3,
        templates=(SyzygyTemplate("clean-slideshow", 3),),
        max_quantity=1,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={},
    )


def _ugc_library() -> SyzygyLibrary:
    # The curated arc is ordinal, NOT sorted: 0058 -> 0051 -> 0052.
    keys = ["0058", "0051", "0052"]
    objects = tuple(
        SyzygyObject(
            f"ugc/french-core/photos/img-{suffix}.jpg",
            1_000 + index,
            f"https://r2.example/ugc/french-core/photos/img-{suffix}.jpg",
        )
        for index, suffix in enumerate(keys)
    )
    canonical = "\n".join(
        [f"{item.key}:{item.bytes}" for item in objects]
    )
    return SyzygyLibrary(
        library_id="syzygy:ugc:french-core",
        lane="ugc",
        subject="french-core",
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def test_ugc_slideshow_matches_slideshow_niche_on_sourced_slideshow_engine():
    publication = {"recipeId": "ugc-slideshow:master", "engine": "sourced_slideshow"}
    spec = {
        "masterPages": {
            "contentEngine": "sourced_slideshow",
            "contentNiche": "slideshow",
        },
        "demand": {"formatMix": {"ugc-slideshow": 1.0}},
    }
    profile = resolve_material_profile(publication, spec)
    assert profile is not None
    assert profile.format_slug == "ugc-slideshow"
    assert profile.content_niche == "slideshow"
    assert profile.content_engine == "sourced_slideshow"
    assert profile.material_source == "image_library"
    assert profile.executor_kind == "slideshow_renderer"
    assert profile.executor_id == "syzygy-slideshow"
    # The scoped executor must be the pin, never the shared lyric/meme file.
    assert profile.executor_version == (
        "sha256:" + hashlib.sha256(UGC_EXECUTOR_PATH.read_bytes()).hexdigest()
    )


def test_ugc_library_address_is_page_scoped():
    publication = {"recipeId": "ugc-slideshow:master", "engine": "sourced_slideshow"}
    spec = {
        "masterPages": {
            "contentEngine": "sourced_slideshow",
            "contentNiche": "slideshow",
        },
        "demand": {"formatMix": {"ugc-slideshow": 1.0}},
    }
    profile = resolve_material_profile(publication, spec)
    address = slideshow_library_address(profile, {"handle": "French.Core"})
    assert address == ("syzygy:ugc:french-core", "ugc", "french-core")
    # A different page's handle must never borrow this library.
    other = slideshow_library_address(profile, {"handle": "Other.Page"})
    assert other == ("syzygy:ugc:other-page", "ugc", "other-page")


def test_ugc_plan_preserves_ordinal_order_without_permutation():
    recipe = _ugc_recipe()
    library = _ugc_library()
    plans = plan_slideshows(recipe, library, 5, set(), "run-a")
    assert len(plans) == 1
    plan = plans[0]
    assert plan["templateSlug"] == "clean-slideshow"
    assert plan["mediaKeys"] == [
        "ugc/french-core/photos/img-0058.jpg",
        "ugc/french-core/photos/img-0051.jpg",
        "ugc/french-core/photos/img-0052.jpg",
    ]
    # Once the single ordered set is reserved, there is no fake variety left.
    assert plan_slideshows(recipe, library, 5, {plan["signature"]}, "run-a") == []


def test_ugc_plan_requires_the_full_curated_set():
    recipe = _ugc_recipe()
    library = _ugc_library()
    undersized = SyzygyLibrary(
        library_id=library.library_id,
        lane=library.lane,
        subject=library.subject,
        objects=library.objects[:2],
        snapshot_hash="a" * 64,
    )
    assert plan_slideshows(recipe, undersized, 1, set(), "run-a") == []


def test_legacy_executor_bytes_are_unchanged():
    # The original executor file must remain byte-identical so lyric/meme
    # profile versions stay pinned to the original whole-file sha256.
    assert hashlib.sha256(EXECUTOR_PATH.read_bytes()).hexdigest() == (
        ORIGINAL_EXECUTOR_SHA
    )
    from services.content_engine_registry import load_engine_registry

    profiles, _ = load_engine_registry()
    for slug in ("lyric-edits", "meme-slideshow"):
        assert profiles[slug].executor_version == f"sha256:{ORIGINAL_EXECUTOR_SHA}"


def test_ugc_page_cut_policy_is_page_scoped():
    policy = ugc_page_cut_policy("acct:culture-core-1")
    assert policy == {
        "cutMode": "cut_style",
        "cuts": 60,
        "audioSectionEnd": 12,
    }
    assert ugc_page_cut_policy("acct:other-page") is None
    assert ugc_page_cut_policy("") is None


async def test_culture_render_wire_has_60_cuts_and_12s_section(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=60.0):
        captured["body"] = body
        return {"job_id": "job-ugc", "quality": "draft"}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    plan = {
        "templateSlug": "clean-slideshow",
        "signature": "a" * 64,
        "mediaKeys": [
            "ugc/french-core/photos/img-0058.jpg",
            "ugc/french-core/photos/img-0051.jpg",
            "ugc/french-core/photos/img-0052.jpg",
        ],
    }
    await slideshows.submit_syzygy_render(
        plan, page_id="acct:culture-core-1", recipe_version="v1",
        format_slug="ugc-slideshow",
    )
    spec = captured["body"]["spec"]
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["lyrics"] == []
    assert spec["captions"] == {"position": "none"}
    assert spec["textCards"] == []
    assert spec["text_cards"] == []
    assert spec["lyrics_lrc"] == ""
    assert spec["lyrics_required"] is False
    assert spec["uses_original_audio"] is False
    # Culture Core's verified fast-cut wire, and no audio `src` key.
    assert spec["cuts"] == {"mode": "cut_style", "cuts": 60}
    assert spec["audio"] == {"section_start": 0, "section_end": 12}
    assert "src" not in spec["audio"]


async def test_generic_ugc_wire_keeps_15s_and_no_cuts(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=60.0):
        captured["body"] = body
        return {"job_id": "job-ugc", "quality": "draft"}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    plan = {
        "templateSlug": "clean-slideshow",
        "signature": "c" * 64,
        "mediaKeys": [
            "ugc/other/photos/img-1.jpg",
            "ugc/other/photos/img-2.jpg",
            "ugc/other/photos/img-3.jpg",
        ],
    }
    await slideshows.submit_syzygy_render(
        plan, page_id="acct:other-page", recipe_version="v1",
        format_slug="ugc-slideshow",
    )
    spec = captured["body"]["spec"]
    # Blank text/no audio still applies to every UGC page…
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["uses_original_audio"] is False
    # …but the fast-cut wire is Culture-only: no cuts, 15s section.
    assert "cuts" not in spec
    assert spec["audio"] == {"section_start": 0, "section_end": 15}
    assert "src" not in spec["audio"]


async def test_legacy_render_payload_has_no_blank_overrides(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=60.0):
        captured["body"] = body
        return {"job_id": "job-meme", "quality": "draft"}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    plan = {
        "templateSlug": "clean-slideshow",
        "signature": "b" * 64,
        "mediaKeys": [
            "meme/subject/photos/a.jpg",
            "meme/subject/photos/b.jpg",
            "meme/subject/photos/c.jpg",
            "meme/subject/photos/d.jpg",
            "meme/subject/photos/e.jpg",
        ],
    }
    await slideshows.submit_syzygy_render(plan, page_id="p", recipe_version="v1")
    spec = captured["body"]["spec"]
    assert "hook" not in spec
    assert "lyrics" not in spec
    assert "captions" not in spec
    assert "textCards" not in spec
    assert "text_cards" not in spec
    assert "lyrics_lrc" not in spec
    assert "lyrics_required" not in spec
    assert "uses_original_audio" not in spec
