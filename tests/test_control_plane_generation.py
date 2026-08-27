"""Pure contracts for the new-media dossier recipe executor."""

import json

import pytest

from providers.base import API_KEYS
from services.control_plane_generation import (
    compose_prompt,
    dossier_filters_to_color_correction,
    resolve_generation_recipe,
)


def publication(**overrides):
    spec = {
        "schema": "dossier.recipe-spec.v1",
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
        },
        "demand": {"formatMix": {"truck-scenic": 1.0}},
    }
    result = {
        "pageId": "tt-tucker-reeves",
        "recipeId": "truck-scenic:master",
        "engine": "hailuo",
        "recipeVersion": "dossier-0123456789abcdef",
        "recipeSpecCanonical": json.dumps(spec, sort_keys=True, separators=(",", ":")),
    }
    result.update(overrides)
    return result


@pytest.fixture(autouse=True)
def ready_runtime(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")


def test_truck_recipe_resolves_only_with_the_exact_server_owned_provider():
    recipe = resolve_generation_recipe(publication())
    assert recipe is not None
    assert recipe.family_name == "truck"
    assert recipe.provider_model == "minimax/hailuo-2.3"
    assert recipe.prompt_catalog_hash == "5c3578a13714cec97d7b0a007ac5cfea744ae50661e7cac415d0ba42f894c894"
    assert resolve_generation_recipe(publication(engine="wan-i2v-fast")) is None


def test_generation_mode_and_provider_credential_fail_closed(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "off")
    assert resolve_generation_recipe(publication()) is None
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", None)
    assert resolve_generation_recipe(publication()) is None


def test_i2v_clip_mode_and_unsupported_treatment_are_not_advertised():
    assert resolve_generation_recipe(publication(recipeId="boat-lake:master", engine="wan-i2v-fast")) is None

    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["filters"]["vignette"] = 0.5
    payload["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_generation_recipe(payload) is None


def test_prompt_rotation_is_deterministic_and_non_repeating():
    recipe = resolve_generation_recipe(publication())
    prompts = [compose_prompt(recipe, "page:policy:job", index)[0] for index in range(20)]
    assert prompts == [compose_prompt(recipe, "page:policy:job", index)[0] for index in range(20)]
    assert len(set(prompts)) == 20
    assert all("{" not in prompt and "}" not in prompt for prompt in prompts)


def test_dossier_filter_units_map_to_the_existing_ffmpeg_slider_contract():
    recipe = resolve_generation_recipe(publication())
    assert dossier_filters_to_color_correction(recipe) == {
        "brightness": pytest.approx(-6.0),
        "contrast": pytest.approx(8.0),
        "saturation": pytest.approx(-10.0),
        "temperature": pytest.approx(-20.0),
        "fade": pytest.approx(10.0),
    }
