"""Curated pages: deterministic re-chop variants of the one approved montage.

Variant 0 is the approved plan with its original signature, so reservations
recorded before variants existed keep their meaning. Variants 1..N keep every
output window's timing and change only which pinned source fills a window,
the source moment inside it, and the crop focus. Each is reserved once.
"""
import copy
import hashlib
import json

import pytest

from services import control_plane_slideshows as slides
from tests.test_control_plane_lyric_video import PAGE as BURNT_PAGE, recipe_and_library as burnt_recipe_and_library
from tests.test_control_plane_ugc_slideshow import _culture_library, _culture_recipe

# Signatures computed from base main 05d7ebf4 (before variants existed).
# Kept as a compatibility export for older plan-family imports.  Burnt's
# active UGC authority is intentionally regenerated from the reviewed clips.
BURNT_CANONICAL = None
CULTURE_CANONICAL = "5c1924553e69420398fe2fb7e79f97d189bbc76678881d0a65357f36277a3126"


def _burnt():
    recipe, library = burnt_recipe_and_library()
    return recipe, library, slides.curated_plan_for_recipe(recipe, library)["signature"]


def _culture():
    return _culture_recipe(), _culture_library(), CULTURE_CANONICAL


PAGES = pytest.mark.parametrize("page", [_culture], ids=["culture"])


def test_burnt_ugc_variants_use_the_reviewed_84_clip_authority():
    """Burnt remains a UGC-only curated source after lyric retirement."""
    recipe, library, _ = _burnt()
    curated = slides._recipe_curated_plan(recipe)
    assert len(curated["sources"]) == len(library.objects) == 84
    assert {item.key for item in library.objects} == {
        f"{recipe.lane}/{recipe.subject}/clips/{source['file']}"
        for source in curated["sources"]
    }
    assert all(
        {window["key"] for window in plan["windows"]} <= set(plan["mediaKeys"])
        for plan in slides.curated_plans_for_recipe(recipe, library)
    )


def _old_canonical_plan(recipe, library):
    """The pre-variant _build_curated_plan derivation, kept verbatim in shape."""
    curated = slides._recipe_curated_plan(recipe)
    by_file = {source["file"]: source for source in curated["sources"]}
    keys, windows = [], []
    for window in curated["windows"]:
        key = f"{recipe.lane}/{recipe.subject}/clips/{window['source']}"
        if key not in keys:
            keys.append(key)
        source = by_file[window["source"]]
        windows.append({
            "key": key, "sourceStart": window["sourceStart"], "sourceEnd": window["sourceEnd"],
            "outputStart": window["outputStart"], "outputEnd": window["outputEnd"],
            "focalX": window["focalX"], "sourceSha256": source["sha256"],
            "sourceBytes": source["bytes"], "sourceDuration": source["duration"],
        })
    plan = {
        "templateSlug": "curated-video-montage", "mediaKeys": keys, "muted": curated["muted"],
        "speed": curated["speed"], "cropMode": curated["cropMode"],
        "audioSectionEnd": curated["audioSectionEnd"], "windows": windows,
    }
    return {**plan, "signature": slides.curated_plan_signature(plan),
            "librarySnapshotHash": library.snapshot_hash}


@PAGES
def test_variant_zero_is_the_approved_plan_byte_for_byte(page):
    recipe, library, canonical = page()
    first = slides.plan_slideshows(recipe, library, 1, set(), "run")[0]
    assert "variant" not in first
    assert first["signature"] == canonical
    assert first == _old_canonical_plan(recipe, library)
    assert slides.curated_plan_for_recipe(recipe, library) == first


@PAGES
def test_variants_are_bounded_distinct_and_deterministic(page):
    recipe, library, _ = page()
    variants = slides.curated_plans_for_recipe(recipe, library)
    assert len(variants) == slides.CURATED_VARIANT_LIMIT == 200
    assert [plan.get("variant", 0) for plan in variants] == list(range(200))
    assert len({plan["signature"] for plan in variants}) == 200
    assert len({json.dumps(plan["windows"], sort_keys=True) for plan in variants}) == 200
    assert slides.curated_plans_for_recipe(recipe, library) == variants
    # run_id does not matter: no randomness at plan time.
    assert (slides.plan_slideshows(recipe, library, 20, set(), "a")
            == slides.plan_slideshows(recipe, library, 20, set(), "b")
            == variants[:20])
    assert len(slides.plan_slideshows(recipe, library, 10_000, set(), "a")) == 200


@PAGES
def test_variants_keep_output_timing_and_render_constants(page):
    recipe, library, _ = page()
    variants = slides.curated_plans_for_recipe(recipe, library)
    base = variants[0]
    timing = [(w["outputStart"], w["outputEnd"]) for w in base["windows"]]
    for plan in variants[1:]:
        assert [(w["outputStart"], w["outputEnd"]) for w in plan["windows"]] == timing
        for field in ("templateSlug", "mediaKeys", "muted", "speed", "cropMode", "audioSectionEnd"):
            assert plan[field] == base[field]
        for window in plan["windows"]:
            assert abs((window["sourceEnd"] - window["sourceStart"])
                       - (window["outputEnd"] - window["outputStart"])) <= 1e-9
        assert plan != base


@PAGES
def test_variant_windows_stay_inside_their_pinned_source(page):
    recipe, library, _ = page()
    curated = slides._recipe_curated_plan(recipe)
    prefix = f"{recipe.lane}/{recipe.subject}/clips/"
    pinned = {prefix + source["file"]: source for source in curated["sources"]}
    for plan in slides.curated_plans_for_recipe(recipe, library):
        for window in plan["windows"]:
            source = pinned[window["key"]]
            assert window["key"] in plan["mediaKeys"]
            assert (window["sourceSha256"], window["sourceBytes"], window["sourceDuration"]) == (
                source["sha256"], source["bytes"], source["duration"])
            assert 0 <= window["sourceStart"] < window["sourceEnd"] <= source["duration"]
            assert 0 <= window["focalX"] <= 1


@PAGES
def test_reserved_variants_are_skipped_once_each(page):
    recipe, library, canonical = page()
    variants = slides.curated_plans_for_recipe(recipe, library)
    after_canonical = slides.plan_slideshows(recipe, library, 1, {canonical}, "run")
    assert after_canonical == [variants[1]]
    reserved = {plan["signature"] for plan in variants[:5]}
    assert slides.plan_slideshows(recipe, library, 2, reserved, "run") == variants[5:7]
    everything = {plan["signature"] for plan in variants}
    assert slides.plan_slideshows(recipe, library, 1, everything, "run") == []


@PAGES
def test_signature_binds_the_variant_number(page):
    recipe, library, _ = page()
    plan = slides.curated_plans_for_recipe(recipe, library)[3]
    assert plan["variant"] == 3
    assert slides.curated_plan_signature(plan) == plan["signature"]
    assert slides.curated_plan_signature({**plan, "variant": 4}) != plan["signature"]
    unnumbered = {key: value for key, value in plan.items() if key != "variant"}
    assert slides.curated_plan_signature(unnumbered) != plan["signature"]


@pytest.mark.parametrize("claimed", [{"variant": 0}, {"variant": 200}, {"variant": True},
                                     {"variant": "3"}, {"variant": -1}, "not-a-plan"])
def test_malformed_variant_claims_are_refused(claimed):
    plan = claimed if isinstance(claimed, str) else {"templateSlug": "curated-video-montage", **claimed}
    assert slides.curated_variant_number(plan) is None
    recipe, library, _ = _burnt()
    assert slides.curated_plan_for_recipe(recipe, library, 200) is None
    assert slides.curated_plan_for_recipe(recipe, library, True) is None


def test_missing_source_bytes_still_close_every_variant():
    recipe, library, _ = _burnt()
    missing = slides.SyzygyLibrary(library.library_id, library.lane, library.subject,
                                   library.objects[:-1], library.snapshot_hash)
    assert slides.plan_slideshows(recipe, missing, 5, set(), "run") == []
    assert slides.curated_plans_for_recipe(recipe, missing) == []


def _tamper(plan, variants, mutation):
    plan = copy.deepcopy(plan)
    if mutation == "renumbered":
        plan["variant"] = 4
        plan["signature"] = slides.curated_plan_signature(plan)
    if mutation == "shifted_resigned":
        plan["windows"][0]["sourceStart"] += 0.1
        plan["windows"][0]["sourceEnd"] += 0.1
        plan["signature"] = slides.curated_plan_signature(plan)
    if mutation == "past_source_end":
        window = plan["windows"][0]
        length = window["sourceEnd"] - window["sourceStart"]
        window["sourceEnd"] = window["sourceDuration"] + 0.5
        window["sourceStart"] = window["sourceEnd"] - length
        plan["signature"] = slides.curated_plan_signature(plan)
    if mutation == "retimed_resigned":
        plan["windows"][0]["outputEnd"] += 0.1
        plan["windows"][1]["outputStart"] += 0.1
        plan["signature"] = slides.curated_plan_signature(plan)
    if mutation == "foreign_signature":
        plan["signature"] = variants[4]["signature"]
    if mutation == "explicit_zero":
        plan = copy.deepcopy(variants[0])
        plan["variant"] = 0
    if mutation == "unnumbered":
        plan.pop("variant")
        plan["signature"] = slides.curated_plan_signature(plan)
    return plan


MUTATIONS = ["renumbered", "shifted_resigned", "past_source_end", "retimed_resigned",
             "foreign_signature", "explicit_zero", "unnumbered"]


@pytest.mark.asyncio
async def test_lyric_wire_accepts_a_correct_variant(monkeypatch):
    recipe, library, _ = _burnt()
    plan = slides.curated_plans_for_recipe(recipe, library)[3]
    calls = []

    async def transport(method, path, **kw):
        calls.append(kw)
        return {"job_id": "mock-capture-only", "quality": "draft"}

    monkeypatch.setattr(slides, "_request_json", transport)
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        await slides.submit_syzygy_render(plan, page_id=BURNT_PAGE, recipe_version="v", format_slug="lyric-video")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", MUTATIONS)
async def test_lyric_wire_refuses_a_tampered_variant_before_remote_post(monkeypatch, mutation):
    recipe, library, _ = _burnt()
    variants = slides.curated_plans_for_recipe(recipe, library)
    plan = _tamper(variants[3], variants, mutation)
    calls = []

    async def transport(*a, **kw):
        calls.append(1)
        raise AssertionError("must not post")

    monkeypatch.setattr(slides, "_request_json", transport)
    with pytest.raises(ValueError, match="burnt lyric video disabled: static captions only"):
        await slides.submit_syzygy_render(plan, page_id=BURNT_PAGE, recipe_version="v", format_slug="lyric-video")
    assert not calls


def _runner_fixture(monkeypatch, tmp_path, plan):
    from routers import control_plane as routes
    from tests.test_control_plane_slideshow_execution import _fake_syzygy_boundary
    recipe, library = burnt_recipe_and_library()
    value = {"masterPages": {"pageId": BURNT_PAGE}}
    publication = {}
    job = {"jobId": "test-only-curated-job", "pageId": BURNT_PAGE,
           "recipeId": recipe.recipe_id, "engine": recipe.engine,
           "recipeVersion": "v", "recipeSpecHash": "a" * 64}
    sizes = {item.key: item.bytes for item in library.objects}
    plan = copy.deepcopy(plan)
    plan["media"] = [{"objectKey": key, "bytes": sizes[key]} for key in plan["mediaKeys"]]
    job.update(status="queued", artifactRoot=str(tmp_path), sourceLibraryId=recipe.library_id,
               formatContractVersion=recipe.format_contract_version, executorVersion=recipe.executor_version,
               materialSource=recipe.material_source, assetType=recipe.asset_type, slideshowPlan=[plan])
    monkeypatch.setattr(routes, "_get_job_or_404", lambda _: job)
    monkeypatch.setattr(routes, "_job_matches_current_master_pages", lambda _: True)
    monkeypatch.setattr(routes, "load_registered_recipe", lambda *a: publication)
    monkeypatch.setattr(routes, "resolve_slideshow_recipe", lambda _: recipe)
    monkeypatch.setattr(routes, "typed_recipe_spec", lambda _: value)
    monkeypatch.setattr(routes, "job_profile_authority_matches", lambda *a: True)
    monkeypatch.setattr(routes, "load_recipe_library", lambda _: library)
    monkeypatch.setattr(routes, "_update_job", lambda _, **changes: job.update(changes))
    monkeypatch.setattr(routes, "_claim_unique_slideshow_clip", lambda *a: None)
    monkeypatch.setattr(routes, "_source_provenance", lambda _, source: source)
    calls = _fake_syzygy_boundary(monkeypatch, library)
    pinned = {w["key"]: w for w in plan["windows"]}

    async def observe(_, keys):
        return [{"objectKey": k, "bytes": pinned[k]["sourceBytes"], "sha256": pinned[k]["sourceSha256"]}
                for k in keys]

    async def thumbnail(*a):
        return None

    monkeypatch.setattr(routes, "observe_library_media", observe)
    monkeypatch.setattr(routes, "_thumbnail_manifest", thumbnail)
    return routes, job, calls


@pytest.mark.asyncio
async def test_job_runner_renders_a_correct_variant(monkeypatch, tmp_path):
    recipe, library, _ = _burnt()
    plan = slides.curated_plans_for_recipe(recipe, library)[7]
    routes, job, calls = _runner_fixture(monkeypatch, tmp_path, plan)
    await routes._run_syzygy_slideshow(job["jobId"])
    assert job["status"] == "completed", job.get("error")
    assert len(calls["submit"]) == 1 and calls["submit"][0][0]["signature"] == plan["signature"]
    assert job["clips"][0]["source"]["planSignature"] == plan["signature"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", MUTATIONS)
async def test_job_runner_refuses_a_tampered_variant_before_render(monkeypatch, tmp_path, mutation):
    recipe, library, _ = _burnt()
    variants = slides.curated_plans_for_recipe(recipe, library)
    plan = _tamper(variants[3], variants, mutation)
    routes, job, calls = _runner_fixture(monkeypatch, tmp_path, plan)
    await routes._run_syzygy_slideshow(job["jobId"])
    assert job["status"] == "failed"
    assert job["error"] == "slideshow_plan_invalid"
    assert calls["submit"] == []


async def test_culture_wire_renders_the_variant_windows(monkeypatch):
    recipe, library, _ = _culture()
    plan = slides.curated_plans_for_recipe(recipe, library)[11]
    captured = {}

    async def transport(method, endpoint, *, body=None, idempotency_key=None, timeout=60.0):
        captured.update(body=body, key=idempotency_key)
        return {"job_id": "job-ugc", "quality": "draft"}

    monkeypatch.setattr(slides, "_request_json", transport)
    await slides.submit_syzygy_render(plan, page_id="acct:culture-core-1", recipe_version="v1",
                                      format_slug="ugc-slideshow")
    media = captured["body"]["spec"]["media"]
    assert [(m["library"], m["clip_start"], m["clip_end"], m["focal_x"]) for m in media] == [
        (w["key"], w["sourceStart"], w["sourceEnd"], w["focalX"]) for w in plan["windows"]]
    assert [m["duration"] for m in media] == [
        round(w["outputEnd"] - w["outputStart"], 6) for w in plan["windows"]]
