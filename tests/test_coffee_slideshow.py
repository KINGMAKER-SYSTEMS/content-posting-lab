"""Coffee keeps its niche and downstream sound/caption bindings on Syzygy."""

import asyncio
import json

import pytest

from services import control_plane_slideshows as slideshows
from services.content_engine_registry import load_engine_registry
from tests import test_control_plane_slideshow_execution as shared


@pytest.fixture
def lab(monkeypatch, tmp_path):
    for name, value in {
        "PAGE_ID": "acct:coffee-tunes",
        "HANDLE": "coffee.tunes",
        "SUBJECT": "coffee-tunes",
        "LANE": "coffee",
        "FORMAT": "coffee-slideshow",
        "NICHE": "Coffee",
        "LIBRARY_ID": "syzygy:coffee:coffee-tunes",
        "RECIPE_ID": "coffee-slideshow:master",
    }.items():
        monkeypatch.setattr(shared, name, value)
    return shared.lab.__wrapped__(monkeypatch, tmp_path)


def test_coffee_recipe_registers_without_changing_niche_or_retiring_ai(lab):
    client, _, _, _, payload = lab
    spec = json.loads(payload["recipeSpecCanonical"])
    assert spec["masterPages"]["contentNiche"] == "Coffee"
    assert payload["engine"] == "sourced_slideshow"
    recipe = slideshows.resolve_slideshow_recipe(payload)
    assert recipe.library_id == "syzygy:coffee:coffee-tunes"
    assert [t.slug for t in recipe.templates] == ["clean-slideshow"]
    profiles, _ = load_engine_registry()
    assert profiles["coffee-tok"].content_engine == "ai_video"
    capabilities = client.get("/api/control-plane/v1/capabilities", headers=shared.headers())
    assert capabilities.status_code == 200
    assert any(row["recipeId"] == "coffee-slideshow:master"
               for row in capabilities.json()["capabilities"])


def test_coffee_plan_variety_is_reserved_and_page_scoped(lab):
    _, _, _, library, payload = lab
    recipe = slideshows.resolve_slideshow_recipe(payload)
    first = slideshows.plan_slideshows(recipe, library, 10, set(), "coffee-first")
    assert len(first) == 10
    signatures = {p["signature"] for p in first}
    assert len(signatures) == 10
    second = slideshows.plan_slideshows(recipe, library, 10, signatures, "coffee-next")
    assert len(second) == 10
    assert not signatures.intersection(p["signature"] for p in second)
    assert all(len(set(p["mediaKeys"])) == 5 for p in first + second)
    foreign = slideshows.SyzygyLibrary("syzygy:coffee:other-page", "coffee", "other-page",
                                     library.objects, library.snapshot_hash)
    assert slideshows.plan_slideshows(recipe, foreign, 1, set(), "foreign") == []


def test_coffee_render_is_neutral_and_job_bound(lab, monkeypatch):
    _, _, _, library, payload = lab
    recipe = slideshows.resolve_slideshow_recipe(payload)
    plan = slideshows.plan_slideshows(recipe, library, 1, set(), "coffee")[0]
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"job_id": "coffee-render", "quality": "full"}

    monkeypatch.setattr(slideshows, "_request_json", request)
    asyncio.run(slideshows.submit_syzygy_render(
        plan, page_id="acct:coffee-tunes", recipe_version="coffee-v1",
        format_slug="coffee-slideshow", lab_job_id="coffee-job-1",
    ))
    spec = calls[0][2]["body"]["spec"]
    assert spec["hook"] == {"text": "", "position": "none"}
    assert spec["lyrics"] == []
    assert spec["captions"] == {"position": "none"}
    assert spec["uses_original_audio"] is False
    assert len(spec["media"]) == 5
    assert all(row["library"].startswith("coffee/coffee-tunes/photos/") for row in spec["media"])
    assert calls[0][2]["idempotency_key"].startswith("cpl-syzygy-")


@pytest.mark.asyncio
async def test_coffee_runs_through_real_http_adapter_and_artifact_receipt(lab, monkeypatch):
    await shared.test_runner_uses_real_syzygy_http_adapter(lab, monkeypatch)
