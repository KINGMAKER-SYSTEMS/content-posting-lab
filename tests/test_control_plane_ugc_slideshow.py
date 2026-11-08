"""Culture Core UGC slideshow: curated ordered video plan, blank text/no audio,
and the page-scoped curated wire — without touching legacy lyric/meme hashes."""

import hashlib
import json
import math
from pathlib import Path

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
    _parse_curated_video_plan,
    curated_plan_for_recipe,
    curated_plan_signature,
    plan_slideshows,
    slideshow_library_address,
    ugc_page_cut_policy,
    ugc_page_treatment,
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
    # Ordinal filenames encode the approved arc through the real lexical fetch sort.
    keys = ["01-0058", "02-0051", "03-0052"]
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


def _culture_recipe() -> SlideshowRecipe:
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
        library_id="syzygy:slideshow-webweaving:culture-core-1",
        lane="slideshow-webweaving",
        subject="culture-core-1",
        minimum_library_items=3,
        templates=(SyzygyTemplate("clean-slideshow", 3),),
        max_quantity=1,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={"masterPages": {"pageId": "acct:culture-core-1"}},
    )


def _culture_library() -> SyzygyLibrary:
    treatment = ugc_page_treatment("acct:culture-core-1")
    curated = treatment["curatedVideoPlan"]
    # Library holds the 15 curated clips plus two extras that must never be
    # selected. Bytes come straight from the pinned config.
    files = [source["file"] for source in curated["sources"]] + [
        "ytal-v-0010.mp4", "tk-v-0002.mp4",
    ]
    by_file = {source["file"]: source for source in curated["sources"]}
    objects = tuple(
        SyzygyObject(
            f"slideshow-webweaving/culture-core-1/clips/{file}",
            by_file[file]["bytes"] if file in by_file else 1234,
            f"https://r2.example/slideshow-webweaving/culture-core-1/clips/{file}",
        )
        for file in files
    )
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return SyzygyLibrary(
        library_id="syzygy:slideshow-webweaving:culture-core-1",
        lane="slideshow-webweaving",
        subject="culture-core-1",
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _culture_plan():
    return curated_plan_for_recipe(_culture_recipe(), _culture_library())



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
        "ugc/french-core/photos/img-01-0058.jpg",
        "ugc/french-core/photos/img-02-0051.jpg",
        "ugc/french-core/photos/img-03-0052.jpg",
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


def test_ugc_page_treatment_curated_for_culture_only():
    treatment = ugc_page_treatment("acct:culture-core-1")
    assert isinstance(treatment, dict)
    curated = treatment["curatedVideoPlan"]
    # 2026-11-04 longer cuts: 19 short windows merged to 8 (every cut >= 1.0 s); the
    # pinned sources are exactly the 7 clips those windows use, so every variant
    # keeps the approved mediaKeys (test_curated_variants render constants).
    assert len(curated["sources"]) == 7
    assert {s["file"] for s in curated["sources"]} == {w["source"] for w in curated["windows"]}
    assert len(curated["windows"]) == 8
    assert curated["muted"] is True
    assert curated["speed"] == 1
    assert curated["cropMode"] == "cover"
    assert curated["lane"] == "slideshow-webweaving"
    assert curated["audioSectionEnd"] == 12
    # Culture no longer carries the legacy cut_style policy.
    assert ugc_page_cut_policy("acct:culture-core-1") is None
    assert ugc_page_treatment("acct:other-page") is None
    assert ugc_page_treatment("") is None


def test_curated_cuts_are_longer_for_the_two_glitchy_pages():
    """F-LAB-CHOPPY-CUTS-BURNT-CULTURE-1104: Eric's glitchy/shaky complaint.

    Every cut window on acct:burnt-mill-rd and acct:culture-core-1 is at least
    1.0 s, output windows stay contiguous (no gaps, no overlaps) and still tile
    exactly [0, audioSectionEnd], at speed 1 with the merged group's first
    window's source clip.
    """
    for page, audio_end in (("acct:burnt-mill-rd", 20), ("acct:culture-core-1", 12)):
        treatment = ugc_page_treatment(page)
        # The cps.py loader (_parse_curated_video_plan via _executor_contract)
        # must accept the merged plan; a rejected plan reads as None here.
        assert isinstance(treatment, dict) and "curatedVideoPlan" in treatment, page
        curated = treatment["curatedVideoPlan"]
        windows = curated["windows"]
        lens = [w["outputEnd"] - w["outputStart"] for w in windows]
        # The handheld protest footage reads glitchy under fast hard cuts.
        assert min(lens) >= 1.0, (page, min(lens))
        assert sum(lens) / len(lens) <= 1.75, (page, sum(lens) / len(lens))
        assert curated["speed"] == 1
        assert curated["audioSectionEnd"] == audio_end
        # Contiguous tiling of the whole audio section, no gaps or overlaps.
        assert windows[0]["outputStart"] == 0
        for previous, window in zip(windows, windows[1:]):
            assert previous["outputEnd"] == window["outputStart"]
        assert windows[-1]["outputEnd"] == audio_end
        # Speed 1: each window's source span equals its output span, inside
        # its pinned clip duration.
        by_file = {s["file"]: s for s in curated["sources"]}
        for window in windows:
            span = window["outputEnd"] - window["outputStart"]
            assert window["sourceEnd"] - window["sourceStart"] == pytest.approx(span, abs=1e-9)
            assert 0 <= window["sourceStart"] < window["sourceEnd"] <= by_file[window["source"]]["duration"]


def test_curated_executor_change_is_scoped_to_the_two_pages():
    """Only Burnt and Culture's windows moved; every other page's bytes are pinned."""
    # The other executor files are byte-identical to their reviewed base sha256.
    other_executors = {
        "syzygy-slideshow.v1.json": ORIGINAL_EXECUTOR_SHA,
        "syzygy-coffee-slideshow.v1.json": (
            "142f7a9d740b701df44dc9c340556c5a051c9cb5c30b37a6853d967b335414d4"
        ),
        "syzygy-slideshow-lyric-video.v1.json": (
            "f2ef06869ea2723e83555be2a6a90558315c346196d2139f83870f7444423e78"
        ),
        "syzygy-image-video.v1.json": (
            "fee820be22a84f50cfa80330c5c0d6006b456a468a0bfa0373f9a652d25d599c"
        ),
        "source-dna-recut.v1.json": (
            "2d0dd20fad4302f799272e7d5e1ba7de1ecb8b9a85646f68cf9720ac14569171"
        ),
    }
    executors_dir = UGC_EXECUTOR_PATH.parent
    for name, digest in other_executors.items():
        assert hashlib.sha256((executors_dir / name).read_bytes()).hexdigest() == digest, name
    # The UGC executor still curates exactly these two pages, and each plan's
    # pinned source library is pinned: Burnt untouched; Culture narrowed to the
    # base entries its merged windows use (fix3), each entry byte-identical.
    value = json.loads(UGC_EXECUTOR_PATH.read_bytes())
    assert set(value["pageTreatments"]) == {"acct:burnt-mill-rd", "acct:culture-core-1"}
    for page, count in (("acct:burnt-mill-rd", 84), ("acct:culture-core-1", 7)):
        curated = value["pageTreatments"][page]["curatedVideoPlan"]
        assert len(curated["sources"]) == count
        canonical = json.dumps(curated["sources"], sort_keys=True, separators=(",", ":"))
        pinned = {
            "acct:burnt-mill-rd": "676f2f822f99eab5bf2f3aa60a2a414cf8875a4da7fb941fcd7f29d36565b682",
            # Culture re-pinned (fix3): base entries filtered to the 7 clips the merged windows use.
            "acct:culture-core-1": "b6e168045cf77c59703e9ffa793a5a3be3807092f7490fdd9603b523c5071e78",
        }[page]
        assert hashlib.sha256(canonical.encode()).hexdigest() == pinned, page
    # Compare every other format with the base of #351, which already
    # included the H3 Highspeed POV Seasonal recipe from #350.
    contracts_doc = json.loads(
        (UGC_EXECUTOR_PATH.parents[1] / "content-format-contracts.v1.json").read_bytes()
    )
    others = {slug: entry for slug, entry in contracts_doc["contracts"].items() if slug != "ugc-slideshow"}
    assert hashlib.sha256(json.dumps(
        others, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        "753a86b41a737ad3b922829c3b6db3cc7481c2752e1c412f52382cfdd06b2051"
    )
    registry_doc = json.loads(
        (UGC_EXECUTOR_PATH.parents[1] / "content-engine-registry.v1.json").read_bytes()
    )
    profiles = {slug: entry for slug, entry in registry_doc["profiles"].items() if slug != "ugc-slideshow"}
    assert hashlib.sha256(json.dumps(
        profiles, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        "ba81c64a62c0f2c662bc8330ac4d44094100840df26ce58cbd5a1c726ae31743"
    )


def test_ugc_executor_version_pins_still_bind_the_file_bytes():
    """Registry/contract pins must track the edited executor file (drift guard)."""
    from services.content_engine_registry import load_engine_registry

    digest = hashlib.sha256(UGC_EXECUTOR_PATH.read_bytes()).hexdigest()
    profiles, _ = load_engine_registry()
    assert profiles["ugc-slideshow"].executor_version == f"sha256:{digest}"


def test_culture_curated_plan_7_unique_keys_8_windows_then_next_variant():
    recipe = _culture_recipe()
    library = _culture_library()
    plans = plan_slideshows(recipe, library, 1, set(), "run-a")
    assert len(plans) == 1
    plan = plans[0]
    # 7 unique ordered source keys for observation/receipt (2026-11-04 merge:
    # 8 windows, each keeping its merged group's first window's clip)…
    assert len(plan["mediaKeys"]) == 7
    assert len(set(plan["mediaKeys"])) == 7
    assert all("/clips/" in key for key in plan["mediaKeys"])
    # …and the full 8-window expansion bound into the signature.
    assert len(plan["windows"]) == 8
    assert plan["templateSlug"] == "curated-video-montage"
    assert plan["signature"] == curated_plan_signature(plan)
    # Extras in the library are never selected.
    assert all(key.count("/clips/") == 1 for key in plan["mediaKeys"])
    assert "slideshow-webweaving/culture-core-1/clips/ytal-v-0010.mp4" not in plan["mediaKeys"]
    # Reserving the approved plan offers the next re-chop variant (same 8
    # merged output windows), never the reserved plan again.  Culture pins
    # only the clips its windows use, so the variant keeps the approved keys.
    following = plan_slideshows(recipe, library, 1, {plan["signature"]}, "run-a")
    assert len(following) == 1 and following[0]["variant"] == 1
    assert following[0]["signature"] != plan["signature"]
    assert len(following[0]["windows"]) == 8
    assert set(following[0]["mediaKeys"]) == {w["key"] for w in following[0]["windows"]}
    assert following[0]["mediaKeys"] == plan["mediaKeys"]
    assert all(key.startswith("slideshow-webweaving/culture-core-1/clips/") for key in following[0]["mediaKeys"])
    # Extras in the library are still never selected.
    assert "slideshow-webweaving/culture-core-1/clips/ytal-v-0010.mp4" not in following[0]["mediaKeys"]


async def test_culture_curated_wire_is_video_format_no_template_no_cuts(monkeypatch):
    captured = {}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=60.0):
        captured["body"] = body
        return {"job_id": "job-ugc", "quality": "draft"}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    plan = _culture_plan()
    assert plan is not None
    await slideshows.submit_syzygy_render(
        plan, page_id="acct:culture-core-1", recipe_version="v1",
        format_slug="ugc-slideshow",
    )
    spec = captured["body"]["spec"]
    # Renderer selects on spec.format (video), not kind.
    assert spec["format"] == "video"
    # 8 explicit windows; no template_slug, no cuts, no audio section.
    assert len(spec["media"]) == 8
    assert "template_slug" not in spec
    assert "cuts" not in spec
    assert "audio" not in spec
    # Blank text / no original audio preserved.
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["lyrics"] == []
    assert spec["uses_original_audio"] is False
    assert all(window["type"] == "video" for window in spec["media"])
    assert all(window["muted"] is True for window in spec["media"])
    assert all(window.get("transition") == {"type": "cut", "duration": 0} for window in spec["media"])
    assert "transitions" not in spec
    assert all(window["speed"] == 1 for window in spec["media"])
    assert all(window["crop_mode"] == "cover" for window in spec["media"])
    assert all("focal_x" in window and "focal_y" in window for window in spec["media"])
    assert all(window["clip_start"] < window["clip_end"] for window in spec["media"])
    # Ordered: first window is shot 1, last is shot 8 after the 2026-11-04 merge.
    assert spec["media"][0]["library"].endswith("/clips/tk-v-0005.mp4")
    assert spec["media"][-1]["library"].endswith("/clips/ytal-v-0004.mp4")


def test_curated_plan_parser_rejects_bad_bounds():
    good = ugc_page_treatment("acct:culture-core-1")["curatedVideoPlan"]

    def bad(**mutations):
        plan = json.loads(json.dumps(good))
        for key, value in mutations.items():
            plan[key] = value
        return plan

    cases = [
        bad(muted=False),
        bad(speed=1.5),
        bad(speed=float("nan")),
        bad(cropMode="fill"),
        bad(audioSectionEnd=0),
    ]
    for plan in cases:
        with pytest.raises(ValueError):
            _parse_curated_video_plan(plan)
    # A window overrunning its source duration.
    overrun = bad()
    overrun["windows"][1]["sourceEnd"] = 999.0
    with pytest.raises(ValueError):
        _parse_curated_video_plan(overrun)
    # Negative source start.
    negative = bad()
    negative["windows"][0]["sourceStart"] = -1
    with pytest.raises(ValueError):
        _parse_curated_video_plan(negative)
    # NaN bound.
    nan = bad()
    nan["windows"][0]["focalX"] = float("nan")
    with pytest.raises(ValueError):
        _parse_curated_video_plan(nan)
    # Source/output length mismatch at speed 1.
    mismatch = bad()
    mismatch["windows"][0]["outputEnd"] = 1.0
    with pytest.raises(ValueError):
        _parse_curated_video_plan(mismatch)
    # Reordered (non-contiguous) windows.
    reordered = bad()
    reordered["windows"] = list(reordered["windows"])
    reordered["windows"][0], reordered["windows"][1] = (
        reordered["windows"][1], reordered["windows"][0],
    )
    with pytest.raises(ValueError):
        _parse_curated_video_plan(reordered)
    # Duplicate source file.
    dup = bad()
    dup["sources"] = [dup["sources"][0], dup["sources"][0]]
    with pytest.raises(ValueError):
        _parse_curated_video_plan(dup)
    # Window referencing an unknown source.
    unknown = bad()
    unknown["windows"][0]["source"] = "missing.mp4"
    with pytest.raises(ValueError):
        _parse_curated_video_plan(unknown)


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


def test_renderer_revision_pattern_is_separate_from_strict_sha256():
    # The renderer revision may be a 40-hex git id or a 64-hex SHA-256…
    assert slideshows.RENDERER_REVISION.fullmatch("a" * 40)
    assert slideshows.RENDERER_REVISION.fullmatch("a" * 64)
    assert not slideshows.RENDERER_REVISION.fullmatch("a" * 39)
    assert not slideshows.RENDERER_REVISION.fullmatch("a" * 41)
    assert not slideshows.RENDERER_REVISION.fullmatch("A" * 40)
    assert not slideshows.RENDERER_REVISION.fullmatch("g" * 40)
    # …while plan signatures and source hashes stay strict 64-hex SHA-256.
    assert slideshows.SHA256.fullmatch("a" * 64)
    assert not slideshows.SHA256.fullmatch("a" * 40)


async def test_syzygy_revision_accepts_40_or_64_hex(monkeypatch):
    holder = {"revision": "a" * 40}

    async def fake_request_json(method, endpoint, *, body=None, idempotency_key=None,
                                timeout=60.0):
        assert method == "GET"
        assert endpoint == "/readyz"
        return {"revision": holder["revision"]}

    monkeypatch.setattr(slideshows, "_request_json", fake_request_json)
    assert await slideshows.syzygy_revision() == "a" * 40
    holder["revision"] = "f" * 64
    assert await slideshows.syzygy_revision() == "f" * 64
    for bad in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 40, ""):
        holder["revision"] = bad
        with pytest.raises(slideshows.SyzygyError):
            await slideshows.syzygy_revision()


def test_ugc_encode_args_drops_audio_and_legacy_preset_unchanged():
    from services.ffmpeg import delivery_encode_args

    preset = "tiktok_delivery_v1"
    legacy = delivery_encode_args(preset)
    assert "-an" not in legacy
    ugc = slideshows.ugc_encode_args(preset)
    # Same closed preset, plus a trailing -an to drop renderer-shipped audio.
    assert ugc == legacy + ["-an"]
    assert ugc[-1] == "-an"


def test_missing_curated_source_never_falls_back_to_generic_three_media():
    from dataclasses import replace
    library = _culture_library()
    missing = replace(library, objects=library.objects[1:])
    assert len(missing.objects) >= 3
    assert plan_slideshows(_culture_recipe(), missing, 1, set(), "run") == []


def test_curated_page_never_borrows_another_subject():
    from dataclasses import replace
    recipe = replace(_culture_recipe(), subject="other-page", library_id="syzygy:slideshow-webweaving:other-page")
    original = _culture_library()
    library = replace(original, subject="other-page", library_id=recipe.library_id,
        objects=tuple(replace(item, key=item.key.replace("/culture-core-1/", "/other-page/")) for item in original.objects))
    assert plan_slideshows(recipe, library, 1, set(), "run") == []


def test_curated_changed_source_bytes_are_unavailable_at_planning():
    from dataclasses import replace
    library = _culture_library()
    changed = replace(library, objects=(replace(library.objects[0], bytes=library.objects[0].bytes + 1), *library.objects[1:]))
    assert plan_slideshows(_culture_recipe(), changed, 1, set(), "run") == []


@pytest.mark.parametrize("stored_plan_shape", ["curated_bad_sha", "generic", "missing", "string"])
async def test_curated_runner_refuses_changed_sha_or_generic_plan_shape(monkeypatch, tmp_path, stored_plan_shape):
    from dataclasses import replace
    import routers.control_plane as cp
    recipe = _culture_recipe()
    recipe = replace(recipe, recipe_spec={**recipe.recipe_spec, "renderTreatment": {
        "filters": {"brightness": .85, "contrast": 1, "saturation": 1, "warmth": 0, "fade": 0, "grain": 0, "vignette": 0},
        "clipSpeed": 1, "clipCrop": {"zoom": 1, "focusX": .5, "focusY": .5}}})
    library = _culture_library()
    plan = curated_plan_for_recipe(recipe, library)
    byte_map = {item.key: item.bytes for item in library.objects}
    approved_plan = plan
    if stored_plan_shape != "curated_bad_sha":
        identity = {"templateSlug": "clean-slideshow", "mediaKeys": plan["mediaKeys"][:3]}
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        plan = {**identity, "signature": signature, "librarySnapshotHash": library.snapshot_hash}
        if stored_plan_shape == "string":
            plan["windows"] = "not-a-window-list"
        elif stored_plan_shape == "missing":
            plan["windows"] = None
    plan = {**plan, "media": [{"objectKey": key, "bytes": byte_map[key]} for key in plan["mediaKeys"]]}
    job = {"jobId": "curated-test", "pageId": "acct:culture-core-1", "recipeId": recipe.recipe_id,
        "engine": recipe.engine, "recipeVersion": "dossier-test", "recipeSpecHash": "a" * 64,
        "artifactRoot": str(tmp_path / "job"), "sourceLibraryId": recipe.library_id,
        "formatContractVersion": recipe.format_contract_version, "executorVersion": recipe.executor_version,
        "materialSource": recipe.material_source, "assetType": recipe.asset_type, "slideshowPlan": [plan],
        "sourceKind": "syzygy_slideshow", "status": "queued", "slideshowClaimVersion": 1,
        "slideshowAttemptId": "test-attempt", "runtimeId": cp._GENERATION_RUNTIME_ID}
    updates = []
    calls = []
    monkeypatch.setattr(cp, "_get_job_or_404", lambda *_: job)
    monkeypatch.setattr(cp, "_job_matches_current_master_pages", lambda *_: True)
    monkeypatch.setattr(cp, "load_registered_recipe", lambda *_: {})
    monkeypatch.setattr(cp, "resolve_slideshow_recipe", lambda *_: recipe)
    monkeypatch.setattr(cp, "job_profile_authority_matches", lambda *_: True)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    def update(_id, attempt, runtime, **kwargs):
        assert _id == job["jobId"] and attempt == job["slideshowAttemptId"] and runtime == job["runtimeId"]
        updates.append(kwargs)
        job.update(kwargs)
        return dict(job)
    monkeypatch.setattr(cp, "_update_slideshow_job", update)
    monkeypatch.setattr(cp, "load_recipe_library", lambda *_: library)
    async def observe(*_):
        pins = {w["key"]: w for w in approved_plan["windows"]}
        return [{"objectKey": key, "bytes": byte_map[key], "sha256": "0" * 64 if stored_plan_shape == "curated_bad_sha" and i == 0 else pins[key]["sourceSha256"]}
            for i, key in enumerate(plan["mediaKeys"])]
    async def submit(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("render must never be submitted for changed source bytes")
    async def revision():
        return "d" * 40
    monkeypatch.setattr(cp, "syzygy_revision", revision)
    monkeypatch.setattr(cp, "observe_library_media", observe)
    monkeypatch.setattr(cp, "submit_syzygy_render", submit)
    await cp._run_syzygy_slideshow("curated-test")
    assert calls == []
    assert updates[-1]["status"] == "failed"
    expected_error = "slideshow_source_sha_mismatch" if stored_plan_shape == "curated_bad_sha" else "slideshow_plan_invalid"
    assert expected_error in updates[-1]["error"]


@pytest.mark.parametrize("tamper", [False, True])
async def test_culture_managed_api_job_binds_plan_and_emits_real_boundary_receipt(monkeypatch, tmp_path, tamper):
    """Exercise normal authenticated recipe/jobs doors; only external media/render work is stubbed."""
    import tests.test_control_plane_slideshow_execution as harness
    import routers.control_plane as cp
    constants = {"PAGE_ID": "acct:culture-core-1", "HANDLE": "culture.core.1", "SUBJECT": "culture-core-1",
        "LANE": "slideshow-webweaving", "FORMAT": "ugc-slideshow", "ENGINE": "sourced_slideshow",
        "NICHE": "slideshow", "LIBRARY_ID": "syzygy:slideshow-webweaving:culture-core-1",
        "RECIPE_ID": "ugc-slideshow:master"}
    for name, value in constants.items():
        monkeypatch.setattr(harness, name, value)
    library = _culture_library()
    monkeypatch.setattr(harness, "_library", lambda *_: library)
    original_publication = harness.publication
    def publication(*args, **kwargs):
        payload = original_publication(*args, **kwargs)
        spec = json.loads(payload["recipeSpecCanonical"])
        spec["renderTreatment"]["filters"] = {"brightness": .85}
        canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        return {**payload, "recipeSpecCanonical": canonical,
            "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()}
    monkeypatch.setattr(harness, "publication", publication)
    client, _, _, _, payload = harness.lab.__wrapped__(monkeypatch, tmp_path)
    response = client.post("/api/control-plane/v1/jobs", json=harness.job_body(1, payload),
        headers=harness.headers("culture-api-job-0001"))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    queued = cp._load_jobs()["jobs"][job_id]
    assert len(queued["slideshowPlan"]) == 1
    assert len(queued["slideshowPlan"][0]["windows"]) == 8
    assert len(queued["slideshowPlan"][0]["mediaKeys"]) == 7
    if tamper:
        plan = json.loads(json.dumps(queued["slideshowPlan"][0]))
        plan["windows"][0]["focalX"] = .1
        plan["signature"] = curated_plan_signature(plan)
        cp._update_job(job_id, slideshowPlan=[plan])
    boundary = harness._fake_syzygy_boundary(monkeypatch, library)
    pins = {row["key"]: row for row in queued["slideshowPlan"][0]["windows"]}
    observations = []
    async def observe(_library, keys):
        observations.append(list(keys))
        return [{"objectKey": key, "bytes": pins[key]["sourceBytes"], "sha256": pins[key]["sourceSha256"]}
            for key in keys]
    requests = []
    async def request_json(method, endpoint, *, body=None, **kwargs):
        if method == "GET":
            assert endpoint == "/readyz"
            return {"revision": "d" * 40}
        assert endpoint == "/api/v1/render"
        requests.append(body["spec"])
        return {"job_id": "real-boundary-test-id", "quality": "draft"}
    treatments = []
    async def treat(src, dst, correction, **kwargs):
        treatments.append((correction, kwargs))
        Path(dst).write_bytes(b"treated-background-test-bytes")
    monkeypatch.setattr(cp, "observe_library_media", observe)
    monkeypatch.setattr(cp, "submit_syzygy_render", slideshows.submit_syzygy_render)
    monkeypatch.setattr(cp, "syzygy_revision", slideshows.syzygy_revision)
    monkeypatch.setattr(slideshows, "_request_json", request_json)
    monkeypatch.setattr(cp, "run_color_correct", treat)
    await cp._run_syzygy_slideshow(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    if tamper:
        assert stored["status"] == "failed"
        assert stored["error"] == "slideshow_plan_invalid"
        assert requests == []
        return
    assert stored["status"] == "completed", stored.get("error")
    assert len(requests) == 1
    assert requests[0]["format"] == "video"
    assert len(requests[0]["media"]) == 8
    assert "cuts" not in requests[0] and "template_slug" not in requests[0]
    assert all(media["transition"] == {"type": "cut", "duration": 0} for media in requests[0]["media"])
    assert len(observations) == 2 and all(len(keys) == 7 for keys in observations)
    assert treatments[0][0]["brightness"] == pytest.approx(-15)
    assert treatments[0][1]["encode_args"][-1] == "-an"
    artifact = stored["clips"][0]
    assert artifact["source"]["renderJobId"] == "real-boundary-test-id"
    assert artifact["source"]["rendererRevision"] == "d" * 40
    assert artifact["source"]["templateSlug"] == "curated-video-montage"
    assert len(artifact["source"]["media"]) == 7
    assert artifact["sourceTreatment"]["sourceSha256"] == artifact["sha256"]
    assert artifact["sourceTreatment"]["visualTreatment"]["filters"]["brightness"] == .85


def test_generic_photo_arc_survives_actual_library_fetch_sort(monkeypatch):
    expected = _ugc_library()
    clips = tuple(SyzygyObject(
        f"ugc/french-core/clips/000{index}.mp4", 2_000 + index,
        f"https://r2.example/ugc/french-core/clips/000{index}.mp4",
    ) for index in range(3))
    mixed = (*expected.objects, *clips)
    payload = {"lane": "ugc", "subject": "french-core", "truncated": False, "skipped": 0,
        "count": len(mixed), "objects": [{"key": item.key, "bytes": item.bytes, "url": item.url}
            for item in reversed(mixed)]}
    monkeypatch.setattr(slideshows, "_runtime", lambda: ("https://syzygy.test", "syz_test"))
    monkeypatch.setattr(slideshows.httpx, "get", lambda *args, **kwargs: slideshows.httpx.Response(200, json=payload))
    library = slideshows._fetch_library(expected.library_id, expected.lane, expected.subject)
    planned = plan_slideshows(_ugc_recipe(), library, 1, set(), "run")
    assert planned[0]["mediaKeys"] == [item.key for item in expected.objects]


def test_generic_ugc_needs_three_photos_even_when_clips_fill_the_library():
    from services.control_plane_plan_reasons import (
        TEMPLATE_MEDIA_EXCEEDS_LIBRARY, explain_empty_slideshow_plan,
    )

    recipe = _ugc_recipe()
    photos = _ugc_library().objects[:2]
    clips = tuple(SyzygyObject(
        f"ugc/french-core/clips/000{index}.mp4", 2_000 + index,
        f"https://r2.example/ugc/french-core/clips/000{index}.mp4",
    ) for index in range(3))
    library = SyzygyLibrary(
        recipe.library_id, recipe.lane, recipe.subject,
        tuple(sorted((*photos, *clips), key=lambda item: item.key)),
        "a" * 64,
    )
    assert plan_slideshows(recipe, library, 1, set(), "run") == []
    assert explain_empty_slideshow_plan(recipe, library, 1, set()) == TEMPLATE_MEDIA_EXCEEDS_LIBRARY


def test_generic_ugc_reports_exhaustion_when_all_photos_are_reserved():
    from services.control_plane_plan_reasons import (
        GENERIC_MEDIA_EXHAUSTED, explain_empty_slideshow_plan,
    )

    recipe = _ugc_recipe()
    library = _ugc_library()
    reserved = {item.key for item in library.objects}
    assert plan_slideshows(recipe, library, 1, set(), "run", reserved_media_keys=reserved) == []
    assert explain_empty_slideshow_plan(
        recipe, library, 1, set(), reserved_media_keys=reserved,
    ) == GENERIC_MEDIA_EXHAUSTED


def _burnt_recipe() -> SlideshowRecipe:
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
        library_id="syzygy:slideshow-webweaving:burnt-mill-rd",
        lane="slideshow-webweaving",
        subject="burnt-mill-rd",
        minimum_library_items=3,
        templates=(SyzygyTemplate("clean-slideshow", 3),),
        max_quantity=1,
        output_width=1080,
        output_height=1920,
        encode_preset="tiktok_delivery_v1",
        recipe_spec={"masterPages": {"pageId": "acct:burnt-mill-rd"}},
    )


def _burnt_library() -> SyzygyLibrary:
    treatment = ugc_page_treatment("acct:burnt-mill-rd")
    curated = treatment["curatedVideoPlan"]
    files = [source["file"] for source in curated["sources"]]
    by_file = {source["file"]: source for source in curated["sources"]}
    objects = tuple(
        SyzygyObject(
            f"slideshow-webweaving/burnt-mill-rd/clips/{file}",
            by_file[file]["bytes"],
            f"https://r2.example/slideshow-webweaving/burnt-mill-rd/clips/{file}",
        )
        for file in files
    )
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return SyzygyLibrary(
        library_id="syzygy:slideshow-webweaving:burnt-mill-rd",
        lane="slideshow-webweaving",
        subject="burnt-mill-rd",
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def test_burnt_ugc_plan_is_independent_of_the_retired_lyric_lane():
    treatment = ugc_page_treatment("acct:burnt-mill-rd")
    assert isinstance(treatment, dict)
    curated = treatment["curatedVideoPlan"]
    lyric = json.loads(slideshows.LYRIC_VIDEO_EXECUTOR_PATH.read_bytes())
    assert "acct:burnt-mill-rd" not in lyric["pageTreatments"]
    assert len(curated["sources"]) == 84
    # 2026-11-04 longer cuts: 27 short windows merged to 13 (every cut >= 1.0 s).
    assert len(curated["windows"]) == 13
    assert curated["audioSectionEnd"] == 20
    assert curated["lane"] == "slideshow-webweaving"
    assert curated["subject"] == "burnt-mill-rd"
    assert all(
        w["source"] in {s["file"] for s in curated["sources"]}
        for w in curated["windows"]
    )

    recipe = _burnt_recipe()
    library = _burnt_library()
    plans = plan_slideshows(recipe, library, 5, set(), "run-a")
    assert len(plans) >= 1
    assert all(
        p["mediaKeys"]
        and all("slideshow-webweaving/burnt-mill-rd/clips/" in k for k in p["mediaKeys"])
        and len(p["windows"]) == 13
        for p in plans
    )

    from services.content_engine_registry import load_engine_registry

    profiles, _ = load_engine_registry()
    profile = profiles["ugc-slideshow"]
    assert profile.executor_version == "sha256:" + hashlib.sha256(
        UGC_EXECUTOR_PATH.read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# fix5: no same-clip adjacency (base plan and every generated variant), the
# 0.75 s end-of-clip margin on the base plan, and retired-executor jobs.
# ---------------------------------------------------------------------------

_CUT_PAGES = ("acct:burnt-mill-rd", "acct:culture-core-1")


def test_no_adjacent_windows_share_a_clip_in_any_generated_variant():
    """A same-clip neighbour is a jump cut inside one shot (review MED-3).

    Variant rotation maps each source to exactly one other source, so adjacency
    in the base plan decides adjacency in every variant: check all of them.
    """
    for page in _CUT_PAGES:
        curated = ugc_page_treatment(page)["curatedVideoPlan"]
        generated = 0
        for variant in range(slideshows.CURATED_VARIANT_LIMIT):
            plan = slideshows.curated_variant_plan(curated, variant)
            if plan is None:
                continue
            generated += 1
            files = [window["key"] for window in plan["windows"]]
            assert all(a != b for a, b in zip(files, files[1:])), (page, variant, files)
        assert generated == slideshows.CURATED_VARIANT_LIMIT, (page, generated)


def test_base_plan_keeps_the_variant_end_of_clip_margin():
    """The approved montage stays clear of the clip end exactly as variants do."""
    for page in _CUT_PAGES:
        curated = ugc_page_treatment(page)["curatedVideoPlan"]
        by_file = {s["file"]: s["duration"] for s in curated["sources"]}
        for window in curated["windows"]:
            assert window["sourceEnd"] <= by_file[window["source"]] - slideshows._VARIANT_TAIL_MARGIN + 1e-9, (
                page, window["source"], window["sourceEnd"], by_file[window["source"]])


_RETIRED_UGC = "sha256:1097ebe82d9a955c63c9f89c4e840c70eeacb0fab62f6c848891c96ca6dffc09"


def _retired_library(page: str, subject: str) -> SyzygyLibrary:
    curated = ugc_page_treatment(page, "ugc-slideshow", _RETIRED_UGC)["curatedVideoPlan"]
    objects = tuple(
        SyzygyObject(f"slideshow-webweaving/{subject}/clips/{s['file']}", s["bytes"],
                     f"https://r2.example/slideshow-webweaving/{subject}/clips/{s['file']}")
        for s in curated["sources"]
    )
    canonical = json.dumps([[o.key, o.bytes] for o in objects], separators=(",", ":"), ensure_ascii=False)
    return SyzygyLibrary(
        library_id=f"syzygy:slideshow-webweaving:{subject}", lane="slideshow-webweaving",
        subject=subject, objects=objects, snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest())


def test_retired_ugc_executor_is_bound_to_its_exact_bytes():
    entry = slideshows.RETIRED_UGC_EXECUTORS[_RETIRED_UGC]
    raw = (UGC_EXECUTOR_PATH.parent / entry["path"]).read_bytes()
    assert f"sha256:{hashlib.sha256(raw).hexdigest()}" == _RETIRED_UGC
    assert _RETIRED_UGC != "sha256:" + hashlib.sha256(UGC_EXECUTOR_PATH.read_bytes()).hexdigest()
    # The retired plans are the pre-change ones (Burnt 27 windows, Culture 19).
    assert len(ugc_page_treatment("acct:burnt-mill-rd", "ugc-slideshow", _RETIRED_UGC)["curatedVideoPlan"]["windows"]) == 27
    assert len(ugc_page_treatment("acct:culture-core-1", "ugc-slideshow", _RETIRED_UGC)["curatedVideoPlan"]["windows"]) == 19
    # An unknown version never resolves a retired file: the live bytes answer.
    live = ugc_page_treatment("acct:culture-core-1", "ugc-slideshow", "sha256:" + "0" * 64)
    assert len(live["curatedVideoPlan"]["windows"]) == 8


def test_retired_recipe_fails_closed(monkeypatch, tmp_path):
    recipe = _culture_recipe()
    entry = slideshows.RETIRED_UGC_EXECUTORS[_RETIRED_UGC]
    good = {"executorVersion": _RETIRED_UGC, "formatContractVersion": entry["formatContractVersion"],
            "engineProfileHash": entry["engineProfileHash"]}
    resolved = slideshows.retired_ugc_recipe(recipe, good)
    assert resolved is not None and resolved.executor_version == _RETIRED_UGC
    assert resolved.format_contract_version == entry["formatContractVersion"]
    assert resolved.engine_profile_hash == entry["engineProfileHash"]
    # Unknown version, current version, wrong contract pin, wrong profile pin, other format.
    assert slideshows.retired_ugc_recipe(recipe, {**good, "executorVersion": "sha256:" + "0" * 64}) is None
    assert slideshows.retired_ugc_recipe(recipe, {**good, "executorVersion": recipe.executor_version}) is None
    assert slideshows.retired_ugc_recipe(recipe, {**good, "formatContractVersion": "sha256:" + "1" * 64}) is None
    assert slideshows.retired_ugc_recipe(recipe, {**good, "engineProfileHash": "sha256:" + "2" * 64}) is None
    from dataclasses import replace
    assert slideshows.retired_ugc_recipe(replace(recipe, format_slug="coffee-slideshow"), good) is None
    # Changed retired bytes no longer hash to their version: refused.
    tampered = tmp_path / "tampered.json"
    tampered.write_bytes((UGC_EXECUTOR_PATH.parent / entry["path"]).read_bytes() + b"\n")
    monkeypatch.setitem(slideshows.RETIRED_UGC_EXECUTORS, _RETIRED_UGC, {**entry, "path": str(tampered)})
    assert slideshows.retired_ugc_recipe(recipe, good) is None


@pytest.mark.parametrize("page,subject,make_recipe", [
    ("acct:culture-core-1", "culture-core-1", _culture_recipe),
    ("acct:burnt-mill-rd", "burnt-mill-rd", None),
])
@pytest.mark.parametrize("version", ["retired", "current", "unknown"])
async def test_job_planned_under_a_retired_executor_still_runs_its_old_plan(monkeypatch, tmp_path, page, subject, make_recipe, version):
    """MED-1: a deploy must not drop jobs planned before it (any page, any time)."""
    from dataclasses import replace
    import routers.control_plane as cp
    recipe = make_recipe() if make_recipe else _burnt_recipe()
    recipe = replace(recipe, recipe_spec={**recipe.recipe_spec, "renderTreatment": {
        "filters": {"brightness": .85, "contrast": 1, "saturation": 1, "warmth": 0, "fade": 0, "grain": 0, "vignette": 0},
        "clipSpeed": 1, "clipCrop": {"zoom": 1, "focusX": .5, "focusY": .5}}})
    entry = slideshows.RETIRED_UGC_EXECUTORS[_RETIRED_UGC]
    old_recipe = replace(recipe, executor_version=_RETIRED_UGC,
                         format_contract_version=entry["formatContractVersion"],
                         engine_profile_hash=entry["engineProfileHash"])
    live_recipe = replace(recipe, engine_profile_hash="sha256:" + "9" * 64)
    library = _retired_library(page, subject) if version == "retired" else (
        _culture_library() if subject == "culture-core-1" else _burnt_library())
    pin_recipe = old_recipe if version == "retired" else live_recipe
    plan = curated_plan_for_recipe(pin_recipe, library)
    assert plan is not None
    if version == "retired":
        assert len(plan["windows"]) == (19 if subject == "culture-core-1" else 27)
    byte_map = {item.key: item.bytes for item in library.objects}
    plan = {**plan, "media": [{"objectKey": key, "bytes": byte_map[key]} for key in plan["mediaKeys"]]}
    job_version = {"retired": _RETIRED_UGC, "current": live_recipe.executor_version,
                   "unknown": "sha256:" + "0" * 64}[version]
    job = {"jobId": "retired-test", "pageId": page, "recipeId": recipe.recipe_id,
        "engine": recipe.engine, "recipeVersion": "dossier-test", "recipeSpecHash": "a" * 64,
        "artifactRoot": str(tmp_path / "job"), "sourceLibraryId": recipe.library_id,
        "engineProfileHash": entry["engineProfileHash"] if version != "current" else live_recipe.engine_profile_hash,
        "formatContractVersion": entry["formatContractVersion"] if version != "current" else live_recipe.format_contract_version,
        "executorVersion": job_version,
        "materialSource": recipe.material_source, "assetType": recipe.asset_type, "slideshowPlan": [plan],
        "sourceKind": "syzygy_slideshow", "status": "queued", "slideshowClaimVersion": 1,
        "slideshowAttemptId": "test-attempt", "runtimeId": cp._GENERATION_RUNTIME_ID}
    updates, submitted = [], []
    monkeypatch.setattr(cp, "_get_job_or_404", lambda *_: job)
    monkeypatch.setattr(cp, "_job_matches_current_master_pages", lambda *_: True)
    monkeypatch.setattr(cp, "load_registered_recipe", lambda *_: {})
    monkeypatch.setattr(cp, "resolve_slideshow_recipe", lambda *_: live_recipe)  # the LIVE registry recipe
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    def update(_id, attempt, runtime, **kwargs):
        updates.append(kwargs); job.update(kwargs); return dict(job)
    monkeypatch.setattr(cp, "_update_slideshow_job", update)
    monkeypatch.setattr(cp, "load_recipe_library", lambda *_: library)
    pins = {w["key"]: w for w in plan["windows"]}
    async def observe(_library, keys):
        return [{"objectKey": k, "bytes": pins[k]["sourceBytes"], "sha256": pins[k]["sourceSha256"]} for k in keys]
    class Reached(Exception):
        pass
    async def submit(submitted_plan, **kwargs):
        submitted.append(submitted_plan)
        raise Reached("render boundary reached")
    async def revision():
        return "d" * 40
    monkeypatch.setattr(cp, "syzygy_revision", revision)
    monkeypatch.setattr(cp, "observe_library_media", observe)
    monkeypatch.setattr(cp, "submit_syzygy_render", submit)
    await cp._run_syzygy_slideshow("retired-test")
    if version == "unknown":
        assert submitted == []
        assert updates[-1]["status"] == "failed" and updates[-1]["error"] == "slideshow_recipe_executor_unavailable"
        return
    # Retired and current both pass every authority/plan check and reach the render boundary
    # with exactly the plan the job was given.
    assert len(submitted) == 1, updates
    assert submitted[0]["windows"] == plan["windows"]
    assert submitted[0]["signature"] == plan["signature"]
    assert "slideshow_recipe_executor_unavailable" not in str(updates[-1].get("error"))
    assert "slideshow_plan_invalid" not in str(updates[-1].get("error"))
