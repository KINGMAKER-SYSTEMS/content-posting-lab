"""Focused job-time guard tests for unsigned curated plan tampering.

Existing tamper helpers resign altered windows, so the router's first
signature comparison against the canonical variant already rejects them.
The missing second comparison (``signature != curated_plan_signature(plan)``)
would allow a stored plan whose first-window focalX changed without a new
signature. These cases cover that exact gap for both curated pages.

Scope is intentionally narrow: prepared job/local store, real router
``_run_syzygy_slideshow``, real planner/digest functions, and fake external
I/O boundaries with real call counters. No full API/auth, publication,
admission, or real render is exercised here. Recipe/store authority and
cross-job uniqueness are stubbed; Culture has no publication validation
proof. Burnt alone keeps the real managed-lyric validator and its matching
existing authority fixture, which the runner requires to reach this guard.
"""

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from services import control_plane_slideshows as slides
from tests.test_control_plane_lyric_video import (
    PAGE as BURNT_PAGE,
    recipe_and_library as burnt_recipe_and_library,
)
from tests.test_control_plane_ugc_slideshow import _culture_library, _culture_recipe
from tests.test_burnt_lyric_binding import fixture as burnt_managed_fixture


CULTURE_PAGE = "acct:culture-core-1"

# Valid treatment for Culture runner; treated as server-owned renderTreatment.
CULTURE_RENDER_TREATMENT = {
    "filters": {
        "brightness": .85,
        "contrast": 1,
        "saturation": 1,
        "warmth": 0,
        "fade": 0,
        "grain": 0,
        "vignette": 0,
    },
    "clipSpeed": 1,
    "clipCrop": {"zoom": 1, "focusX": .5, "focusY": .5},
}


def _burnt_page():
    recipe, library = burnt_recipe_and_library()
    return recipe, library, BURNT_PAGE


def _culture_page():
    recipe = _culture_recipe()
    recipe = replace(
        recipe,
        recipe_spec={
            **recipe.recipe_spec,
            "renderTreatment": CULTURE_RENDER_TREATMENT,
        },
    )
    library = _culture_library()
    return recipe, library, CULTURE_PAGE


PAGES = pytest.mark.parametrize("page", [_culture_page], ids=["culture"])


def _prepare_runner(monkeypatch, tmp_path, page, *, tamper_focal=False):
    """Prepare job/runner with real router and fake, counted boundaries.

    Returns ``(routes, job, plan, boundary, recipe, library)`` where
    ``plan`` is the stored plan (tampered if requested) and ``boundary``
    is a dict of real call counters/lists updated by the patched functions.
    """
    from routers import control_plane as routes

    recipe, library, page_id = page()
    plans = slides.curated_plans_for_recipe(recipe, library)
    assert plans, "curated plans must exist"
    plan = copy.deepcopy(plans[0])

    if tamper_focal:
        original_focal = plan["windows"][0]["focalX"]
        new_focal = 0.0 if original_focal != 0.0 else 0.5
        plan["windows"][0]["focalX"] = new_focal
        expected = copy.deepcopy(plans[0])
        expected["windows"][0]["focalX"] = new_focal
        assert plan == expected, "tamper must change only first-window focalX"
        assert 0 <= new_focal <= 1 and new_focal != original_focal
        # Deliberately DO NOT resign: signature stays the canonical variant
        # digest while the actual plan content no longer matches it.

    sizes = {item.key: item.bytes for item in library.objects}
    plan["media"] = [
        {"objectKey": key, "bytes": sizes[key]} for key in plan["mediaKeys"]
    ]

    job = {
        "jobId": f"test-{page_id.replace('acct:', '')}",
        "pageId": page_id,
        "recipeId": recipe.recipe_id,
        "engine": recipe.engine,
        "recipeVersion": "test-version",
        "recipeSpecHash": "a" * 64,
        "artifactRoot": str(tmp_path),
        "sourceLibraryId": recipe.library_id,
        "formatContractVersion": recipe.format_contract_version,
        "executorVersion": recipe.executor_version,
        "materialSource": recipe.material_source,
        "assetType": recipe.asset_type,
        "slideshowPlan": [plan],
        "status": "queued",
    }

    publication = {}
    if page_id == BURNT_PAGE:
        _, publication, authority_job = burnt_managed_fixture()
        job.update(authority_job)
        job["status"] = "queued"

    monkeypatch.setattr(routes, "_get_job_or_404", lambda _: job)
    monkeypatch.setattr(routes, "_job_matches_current_master_pages", lambda _: True)
    monkeypatch.setattr(routes, "load_registered_recipe", lambda *a: publication)
    monkeypatch.setattr(routes, "resolve_slideshow_recipe", lambda _: recipe)
    monkeypatch.setattr(routes, "typed_recipe_spec", lambda _: recipe.recipe_spec)
    monkeypatch.setattr(routes, "job_profile_authority_matches", lambda *a: True)
    monkeypatch.setattr(routes, "load_recipe_library", lambda _: library)
    monkeypatch.setattr(
        routes, "_update_job", lambda _job_id, **changes: job.update(changes)
    )
    monkeypatch.setattr(routes, "_claim_unique_slideshow_clip", lambda *a: None)
    monkeypatch.setattr(routes, "_source_provenance", lambda _, source: source)

    pinned_by_key = {
        window["key"]: (window["sourceSha256"], window["sourceBytes"])
        for window in plan["windows"]
    }
    boundary = {"observe": 0, "revision": 0, "submit": [], "download": 0, "render": 0}

    async def fake_revision():
        boundary["revision"] += 1
        return "d" * 64

    async def fake_observe(_library, keys):
        boundary["observe"] += 1
        return [
            {
                "objectKey": key,
                "bytes": pinned_by_key[key][1],
                "sha256": pinned_by_key[key][0],
                "etag": f'"etag-{key}"',
            }
            for key in keys
        ]

    async def fake_submit(plan, *, page_id, recipe_version, format_slug=None):
        boundary["submit"].append(plan)
        return "render-job-test", "draft"

    async def fake_poll(render_job_id):
        return "/render-file/test.mp4"

    async def fake_download(play_url, destination):
        boundary["download"] += 1
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"raw-render")

    async def fake_render(src, dst, correction, **kwargs):
        boundary["render"] += 1
        Path(dst).write_bytes(b"derived:" + Path(dst).name.encode())

    async def fake_thumbnail(*a):
        return {"sha256": "f" * 64, "path": "thumb.jpg", "bytes": 123}

    monkeypatch.setattr(routes, "syzygy_revision", fake_revision)
    monkeypatch.setattr(routes, "observe_library_media", fake_observe)
    monkeypatch.setattr(routes, "submit_syzygy_render", fake_submit)
    monkeypatch.setattr(routes, "poll_syzygy_render", fake_poll)
    monkeypatch.setattr(routes, "download_syzygy_artifact", fake_download)
    monkeypatch.setattr(routes, "run_color_correct", fake_render)
    monkeypatch.setattr(routes, "_thumbnail_manifest", fake_thumbnail)

    return routes, job, plan, boundary, recipe, library


@PAGES
@pytest.mark.asyncio
async def test_untampered_curated_plan_reaches_completed_and_one_submission(
    monkeypatch, tmp_path, page
):
    """Positive control: a correct stored plan completes and submits once."""
    routes, job, plan, boundary, _, _ = _prepare_runner(
        monkeypatch, tmp_path, page, tamper_focal=False
    )
    await routes._run_syzygy_slideshow(job["jobId"])

    assert job["status"] == "completed", job.get("error")
    assert len(boundary["submit"]) == 1
    submitted_plan = boundary["submit"][0]
    assert submitted_plan["signature"] == plan["signature"]
    assert job["clips"][0]["source"]["planSignature"] == plan["signature"]

    # Real boundary counters, not self-defined guarantees.
    assert boundary["observe"] == 2  # before and after source observation
    assert boundary["revision"] == 1
    assert boundary["download"] == 1
    assert boundary["render"] == 1


@PAGES
@pytest.mark.asyncio
async def test_unsigned_first_window_focal_change_fails_before_external_io(
    monkeypatch, tmp_path, page
):
    """Negative: focalX changed without resigning must fail before any I/O."""
    routes, job, plan, boundary, recipe, library = _prepare_runner(
        monkeypatch, tmp_path, page, tamper_focal=True
    )

    # Stored signature is still the canonical variant digest, but the actual
    # recomputed signature of the tampered stored plan differs.
    canonical = slides.curated_plan_for_recipe(
        recipe, library, slides.curated_variant_number(plan)
    )
    assert plan["signature"] == canonical["signature"]
    assert slides.curated_plan_signature(plan) != plan["signature"]

    await routes._run_syzygy_slideshow(job["jobId"])

    assert job["status"] == "failed"
    assert job["error"] == "slideshow_plan_invalid"

    # No external boundary was reached.
    assert boundary["observe"] == 0
    assert boundary["revision"] == 0
    assert boundary["submit"] == []
    assert boundary["download"] == 0
    assert boundary["render"] == 0
