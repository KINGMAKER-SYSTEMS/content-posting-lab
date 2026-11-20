"""A page that picks one crop per render gets one render per post.

The truck family cuts five 9:16 crops from one wide render. The 914 check
reads those five crops as one body (siblings), so a page whose Dossier sets
``crop_mode: none`` must get a separate render for every requested post.
"""
import hashlib
import json
import shutil

import pytest

from providers.base import API_KEYS
import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from services.control_plane_generation import resolve_generation_recipe
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.test_control_plane_dossier_execution import (  # noqa: F401 - fixture
    HEADERS,
    PAGE_ID,
    TOKEN,
    _advertise_zero_reasons,
    job_body,
    lab,
    recipe_publication,
)

VERSION = "dossier-fedcba0987654321"


@pytest.fixture(autouse=True)
def ready_runtime(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")


def one_crop_publication(crop_mode="none"):
    publication = recipe_publication()
    spec = json.loads(publication["recipeSpecCanonical"])
    catalog = build_dossier_ingredient_catalog(
        PAGE_ID, spec["masterPages"], spec["masterPagesHash"],
    )
    production = {
        "catalogVersion": "",
        "providerId": "hailuo",
        "modelId": "minimax/hailuo-2.3",
        "promptModuleId": "truck",
        "referenceSetId": None,
        "sourceLibraryId": None,
        "variationValues": {},
        "controls": {"crop_mode": crop_mode},
    }
    production["catalogVersion"] = catalog_selection_version(
        catalog, "truck-scenic", production,
    )
    assert production["catalogVersion"]
    spec.update(schema="dossier.recipe-spec.v3", production=production)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    publication.update(
        recipeVersion=VERSION,
        dossierRevision="rev-one-crop",
        recipeSpecCanonical=canonical,
        recipeSpecHash="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    )
    return publication


@pytest.mark.parametrize("crop_mode, per_render, calls_for_five", [
    ("none", 1, 5),
    ("dual", 2, 3),
    ("triptych", 3, 2),
    ("both", 5, 1),
])
def test_render_count_follows_the_selected_crop_mode(crop_mode, per_render, calls_for_five):
    recipe = resolve_generation_recipe(one_crop_publication(crop_mode))
    assert recipe is not None
    assert recipe.family["clips_per_gen"] == 5  # family default unchanged
    assert recipe.clips_per_generation == per_render
    assert recipe.planned_provider_calls(5) == calls_for_five


def test_family_default_still_cuts_five_crops_without_a_page_override():
    recipe = resolve_generation_recipe(recipe_publication())
    assert recipe.clips_per_generation == 5
    assert recipe.planned_provider_calls(5) == 1


@pytest.mark.asyncio
async def test_five_posts_are_five_separate_renders_not_five_sibling_crops(lab, monkeypatch):
    client, _, _ = lab
    headers = {**HEADERS, "Idempotency-Key": "tt-tucker-reeves:policy:one-crop"}
    registration = client.post(
        "/api/control-plane/v1/recipes", json=one_crop_publication(), headers=headers,
    )
    assert registration.status_code == 200, registration.text
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(quantity=5, recipeVersion=VERSION),
        headers={**headers, "Idempotency-Key": "tt-tucker-reeves:job:one-crop"},
    )
    assert response.status_code in (200, 201, 202), response.text
    job_id = response.json()["jobId"]
    assert cp._load_jobs()["jobs"][job_id]["providerCallsPlanned"] == 5

    seen_crop_modes = []

    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        seen_crop_modes.append(extra.get("crop_mode"))
        assert aspect_ratio == "9:16"
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        render = folder / "render.mp4"
        render.write_bytes(f"centre-cropped-render:{provider_job_id}:{prompt}".encode())
        jobs[provider_job_id]["videos"][index].update({
            "status": "done", "file": str(render.relative_to(output_dir)),
        })

    async def fake_color_correct(
        source, destination, color_correction, scale=None, playback_speed=1.0,
        clip_crop=None,
    ):
        shutil.copyfile(source, destination)

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"jpeg-thumbnail-{index}".encode())
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "generate_one", fake_generate_one)
    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed", stored.get("error")
    clips = stored["clips"]
    assert len(clips) == 5
    assert seen_crop_modes == ["none"] * 5
    # Five provider renders, one post each: no two clips share a source body,
    # and none carries a multi-crop sibling group the 914 check would merge.
    assert sorted(clip["generationIndex"] for clip in clips) == list(range(5))
    assert len({clip["source"]["sha256"] for clip in clips}) == 5
    assert len({clip["sha256"] for clip in clips}) == 5
    assert all("groupId" not in clip.get("delivery", {}).get("crop", {}) for clip in clips)


def test_one_crop_recipe_never_reuses_a_master_as_five_sibling_crops():
    # Recovery re-cuts a stored wide master into five crops. A page that asked
    # for one crop per render must never be served that way.
    one_crop = resolve_generation_recipe(one_crop_publication("none"))
    store = {"jobs": {}}
    assert cp._truck_master_candidates(
        store, PAGE_ID, 5, content_engine="ai_video",
        recipe_id=cp.TRUCK_RECIPE_ID, generation_recipe=one_crop,
    ) == []
    five_crop = resolve_generation_recipe(one_crop_publication("both"))
    assert five_crop.clips_per_generation == cp.CROP_MODE_CLIPS[cp.TRUCK_CROP_MODE]
