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
    assert len(curated["sources"]) == 15
    assert len(curated["windows"]) == 19
    assert curated["muted"] is True
    assert curated["speed"] == 1
    assert curated["cropMode"] == "cover"
    assert curated["lane"] == "slideshow-webweaving"
    assert curated["audioSectionEnd"] == 12
    # Culture no longer carries the legacy cut_style policy.
    assert ugc_page_cut_policy("acct:culture-core-1") is None
    assert ugc_page_treatment("acct:other-page") is None
    assert ugc_page_treatment("") is None


def test_culture_curated_plan_15_unique_keys_19_windows_then_next_variant():
    recipe = _culture_recipe()
    library = _culture_library()
    plans = plan_slideshows(recipe, library, 1, set(), "run-a")
    assert len(plans) == 1
    plan = plans[0]
    # 15 unique ordered source keys for observation/receipt…
    assert len(plan["mediaKeys"]) == 15
    assert len(set(plan["mediaKeys"])) == 15
    assert all("/clips/" in key for key in plan["mediaKeys"])
    # …and the full 19-window expansion bound into the signature.
    assert len(plan["windows"]) == 19
    assert plan["templateSlug"] == "curated-video-montage"
    assert plan["signature"] == curated_plan_signature(plan)
    # Extras in the library are never selected.
    assert all(key.count("/clips/") == 1 for key in plan["mediaKeys"])
    assert "slideshow-webweaving/culture-core-1/clips/ytal-v-0010.mp4" not in plan["mediaKeys"]
    # Reserving the approved plan offers the next re-chop variant (same
    # sources, same output timing), never the reserved plan again.
    following = plan_slideshows(recipe, library, 1, {plan["signature"]}, "run-a")
    assert len(following) == 1 and following[0]["variant"] == 1
    assert following[0]["signature"] != plan["signature"]
    assert following[0]["mediaKeys"] == plan["mediaKeys"]


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
    # 19 explicit windows; no template_slug, no cuts, no audio section.
    assert len(spec["media"]) == 19
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
    # Ordered: first window is shot 1, last is shot 19.
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
    assert len(queued["slideshowPlan"][0]["windows"]) == 19
    assert len(queued["slideshowPlan"][0]["mediaKeys"]) == 15
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
    assert len(requests[0]["media"]) == 19
    assert "cuts" not in requests[0] and "template_slug" not in requests[0]
    assert all(media["transition"] == {"type": "cut", "duration": 0} for media in requests[0]["media"])
    assert len(observations) == 2 and all(len(keys) == 15 for keys in observations)
    assert treatments[0][0]["brightness"] == pytest.approx(-15)
    assert treatments[0][1]["encode_args"][-1] == "-an"
    artifact = stored["clips"][0]
    assert artifact["source"]["renderJobId"] == "real-boundary-test-id"
    assert artifact["source"]["rendererRevision"] == "d" * 40
    assert artifact["source"]["templateSlug"] == "curated-video-montage"
    assert len(artifact["source"]["media"]) == 15
    assert artifact["sourceTreatment"]["sourceSha256"] == artifact["sha256"]
    assert artifact["sourceTreatment"]["visualTreatment"]["filters"]["brightness"] == .85


def test_generic_photo_arc_survives_actual_library_fetch_sort(monkeypatch):
    expected = _ugc_library()
    payload = {"lane": "ugc", "subject": "french-core", "truncated": False, "skipped": 0,
        "count": 3, "objects": [{"key": item.key, "bytes": item.bytes, "url": item.url}
            for item in reversed(expected.objects)]}
    monkeypatch.setattr(slideshows, "_runtime", lambda: ("https://syzygy.test", "syz_test"))
    monkeypatch.setattr(slideshows.httpx, "get", lambda *args, **kwargs: slideshows.httpx.Response(200, json=payload))
    library = slideshows._fetch_library(expected.library_id, expected.lane, expected.subject)
    planned = plan_slideshows(_ugc_recipe(), library, 1, set(), "run")
    assert planned[0]["mediaKeys"] == [item.key for item in expected.objects]


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
    assert len(curated["windows"]) == 27
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
        and len(p["windows"]) == 27
        for p in plans
    )

    from services.content_engine_registry import load_engine_registry

    profiles, _ = load_engine_registry()
    profile = profiles["ugc-slideshow"]
    assert profile.executor_version == "sha256:" + hashlib.sha256(
        UGC_EXECUTOR_PATH.read_bytes()
    ).hexdigest()
