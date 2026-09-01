"""Pure contracts for the new-media dossier recipe executor."""

import hashlib
import json

import pytest

from providers.base import API_KEYS
from services.control_plane_generation import (
    GenerationRecipe,
    compose_prompt,
    dossier_clip_crop,
    dossier_clip_speed,
    dossier_filters_to_color_correction,
    load_generation_anchor,
    resolve_generation_recipe,
)
from tests.master_pages_fixtures import master_pages


def publication(**overrides):
    intent, revision = master_pages("tt-tucker-reeves", handle="tucker.reeves")
    spec = {
        "schema": "dossier.recipe-spec.v2",
        "masterPages": intent,
        "masterPagesHash": revision,
        "renderTreatment": {
            "stylePreset": "Dramatic Cool",
            "filters": {
                "brightness": 0.94,
                "contrast": 1.08,
                "saturation": 0.9,
                "warmth": -0.2,
                "fade": 0.1,
            },
            "captionStyle": {},
            "clipSpeed": 1.25,
            "clipCrop": {"zoom": 1.6, "focusX": 0.25, "focusY": 0.7},
        },
        "demand": {"formatMix": {"truck-scenic": 1.0}},
    }
    result = {
        "pageId": "tt-tucker-reeves",
        "recipeId": "truck-scenic:master",
        "engine": "ai_video",
        "recipeVersion": "dossier-0123456789abcdef",
        "recipeSpecCanonical": json.dumps(spec, sort_keys=True, separators=(",", ":")),
    }
    result.update(overrides)
    return result


@pytest.fixture(autouse=True)
def ready_runtime(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")


def test_truck_recipe_resolves_from_the_master_pages_engine_and_server_owned_provider():
    recipe = resolve_generation_recipe(publication())
    assert recipe is not None
    assert recipe.family_name == "truck"
    assert recipe.provider_model == "minimax/hailuo-2.3"
    assert recipe.engine == "hailuo"
    assert recipe.prompt_catalog_hash == "c80cc32e6e762be05e6655190432e945c55afeb196fc488f3b09ad2fad51b9f1"
    assert recipe.family["extra"]["crop_mode"] == "both"
    assert recipe.clips_per_generation == 5
    assert resolve_generation_recipe(publication(engine="wan-i2v-fast")) is None


def test_master_pages_niche_cannot_borrow_another_niches_generator():
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    intent, revision = master_pages(
        "tt-tucker-reeves", handle="tucker.reeves",
        content_niche="Coffee", content_engine="ai_video",
    )
    spec["masterPages"] = intent
    spec["masterPagesHash"] = revision
    payload["recipeSpecCanonical"] = json.dumps(
        spec, sort_keys=True, separators=(",", ":"),
    )
    assert resolve_generation_recipe(payload) is None

    coffee = _format_publication(
        "coffee-tok", "coffee-tok:master", "ai_video",
    )
    assert resolve_generation_recipe(coffee) is None


def test_generation_mode_and_provider_credential_fail_closed(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "off")
    assert resolve_generation_recipe(publication()) is None
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", None)
    assert resolve_generation_recipe(publication()) is None


def _format_publication(format_slug, recipe_id, engine):
    payload = publication(recipeId=recipe_id, engine=engine)
    spec = json.loads(payload["recipeSpecCanonical"])
    niche = {"boat-lake": "BOAT", "silhouette-truck": "silhouette", "pov-scenic": "POV — Scenic"}.get(format_slug, "TRUCK")
    intent, revision = master_pages("tt-tucker-reeves", handle="tucker.reeves", content_niche=niche, content_engine=engine)
    spec["masterPages"] = intent
    spec["masterPagesHash"] = revision
    spec["demand"]["formatMix"] = {format_slug: 1.0}
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return payload


def test_failed_boat_and_silhouette_visual_libraries_are_not_advertised():
    assert resolve_generation_recipe(_format_publication(
        "boat-lake", "boat-lake:master", "ai_video",
    )) is None
    assert resolve_generation_recipe(_format_publication(
        "silhouette-truck", "silhouette-truck:master", "ai_video",
    )) is None

    assert resolve_generation_recipe(_format_publication(
        "pov-scenic", "pov-scenic:master", "sourced_video",
    )) is None


def test_malformed_or_out_of_range_treatment_is_not_advertised():
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"]["vignette"] = 1.5
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_generation_recipe(payload) is None

    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["clipSpeed"] = 2.01
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_generation_recipe(payload) is None

    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["clipCrop"]["focusX"] = -0.01
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_generation_recipe(payload) is None

    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"]["brightness"] = "bright"
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_generation_recipe(payload) is None


@pytest.mark.asyncio
async def test_manifest_anchors_are_hash_verified_and_rotate_without_replacement():
    first = b"first-anchor"
    second = b"second-anchor"
    first_sha = hashlib.sha256(first).hexdigest()
    second_sha = hashlib.sha256(second).hexdigest()
    manifest = json.dumps({
        "schema": "rail.anchor-pool.v1",
        "recipeId": "silhouette-truck:master",
        "format": "silhouette-truck",
        "rotation": "deterministic_without_replacement",
        "anchors": [
            {"path": "anchors/first.png", "sha256": first_sha, "bytes": len(first)},
            {"path": "anchors/second.png", "sha256": second_sha, "bytes": len(second)},
        ],
    }, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    recipe = GenerationRecipe(
        recipe_id="silhouette-truck:master",
        format_slug="silhouette-truck",
        family_name="silhouette",
        engine="wan-i2v-fast",
        provider_model="wan-video/wan-2.2-i2v-fast",
        engine_registry_hash="registry",
        format_contract_version="contract",
        material_source="generated_video",
        asset_type="video/mp4",
        executor_version="executor",
        prompt_catalog_hash="catalog",
        family={
            "method": "i2v",
            "anchor_manifest_sha256": manifest_sha,
            "anchor_count": 2,
        },
        provider_config={},
        recipe_spec={},
    )

    objects = {
        (manifest_sha, "json"): manifest,
        (first_sha, "png"): first,
        (second_sha, "png"): second,
    }

    async def fetcher(sha256, extension, max_bytes):
        value = objects[(sha256, extension)]
        assert len(value) <= max_bytes
        return value

    anchor0 = await load_generation_anchor(recipe, "stable-run", 0, fetcher=fetcher)
    anchor1 = await load_generation_anchor(recipe, "stable-run", 1, fetcher=fetcher)
    assert anchor0[1]["sha256"] != anchor1[1]["sha256"]
    assert anchor0[1]["manifestSha256"] == manifest_sha
    assert anchor0[0].startswith("data:image/png;base64,")

    async def tampered(sha256, extension, max_bytes):
        value = await fetcher(sha256, extension, max_bytes)
        return value if extension == "json" else value + b"tampered"

    with pytest.raises(RuntimeError, match="byte length"):
        await load_generation_anchor(recipe, "stable-run", 0, fetcher=tampered)


def test_prompt_rotation_is_deterministic_and_non_repeating():
    recipe = resolve_generation_recipe(publication())
    prompts = [compose_prompt(recipe, "page:policy:job", index)[0] for index in range(20)]
    assert prompts == [compose_prompt(recipe, "page:policy:job", index)[0] for index in range(20)]
    assert len(set(prompts)) == 20
    assert all("{" not in prompt and "}" not in prompt for prompt in prompts)


def test_dossier_filter_units_map_to_the_existing_ffmpeg_slider_contract():
    recipe = resolve_generation_recipe(publication())
    recipe.recipe_spec["renderTreatment"]["filters"].update({"grain": 0.2, "vignette": 0.3})
    assert dossier_filters_to_color_correction(recipe) == {
        "brightness": pytest.approx(-6.0),
        "contrast": pytest.approx(8.0),
        "saturation": pytest.approx(-10.0),
        "temperature": pytest.approx(-20.0),
        "fade": pytest.approx(10.0),
        "grain": pytest.approx(20.0),
        "vignette": pytest.approx(30.0),
    }
    assert dossier_clip_speed(recipe) == pytest.approx(1.25)
    assert dossier_clip_crop(recipe) == {
        "zoom": pytest.approx(1.6),
        "focusX": pytest.approx(0.25),
        "focusY": pytest.approx(0.7),
    }

    old = publication()
    spec = json.loads(old["recipeSpecCanonical"])
    spec["renderTreatment"].pop("clipSpeed")
    spec["renderTreatment"].pop("clipCrop")
    old["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    legacy = resolve_generation_recipe(old)
    assert dossier_clip_speed(legacy) == pytest.approx(1.0)
    assert dossier_clip_crop(legacy) is None
