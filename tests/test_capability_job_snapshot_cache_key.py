"""C6/T2 (Tides review of PR #185, round 3): the capabilities() response
cache must never serve a stale answer at an unchanged job-snapshot
generation. Each test here mutates ONE external input the cache key must
track — a format contract, a prompt catalog, a live slideshow library, or
the SET of engines among a page's registered bindings — while explicitly
holding the job store unwritten (same generation throughout), and asserts
the next capabilities() call reflects the change rather than replaying a
cached answer.

Every test in this file is RED on 06f9b66 (the response cache existed but
its key did not cover these inputs) and GREEN after (contracts_signature,
catalog_signature, and binding_engines / the slideshow bypass added to the
key).
"""

from __future__ import annotations

import types

import pytest

from routers import control_plane as cp
from services.json_store import atomic_save

PAGE_ID = "acct:cache-key-page"


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    cp._capabilities_cache.clear()
    atomic_save(path, {"jobs": {}, "version": 1, "byIdempotency": {}, "served": {}})
    # Publish once so the snapshot's generation is established and stays
    # fixed for the rest of the test — nothing here writes the job store
    # again, so every capabilities() call in a test sees the SAME
    # generation.
    cp._read_jobs_snapshot()
    return path


def _base_scaffold(monkeypatch, *, master_pages=None):
    monkeypatch.setattr(
        cp, "_current_intent_for_capabilities",
        lambda _: (master_pages or {"contentNiche": "OTHER", "contentEngine": "ai_video"}, "fixed-intent-hash"),
    )


def test_format_contract_only_change_is_reflected(job_path, tmp_path, monkeypatch):
    """(a) A format-contract-only change at an unchanged job generation:
    the bootstrap entry (built from load_engine_registry's profiles, which
    are parsed FROM the matching contract) must reflect the new value on
    the very next call, not a cached one."""
    _base_scaffold(monkeypatch, master_pages={"contentNiche": "TRUCK", "contentEngine": "ai_video"})
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [])

    contracts_file = tmp_path / "contracts.json"
    contracts_file.write_text("{}")
    monkeypatch.setenv("CONTENT_LAB_FORMAT_CONTRACTS", str(contracts_file))

    state = {"max_quantity": 5}

    def stub_load_engine_registry():
        profile = types.SimpleNamespace(
            content_niche="TRUCK", content_engine="ai_video",
            execution_status="commissioned", format_slug="truck-scenic",
            format_contract_version="stub-version", max_quantity=state["max_quantity"],
        )
        return {"truck-scenic": profile}, "fixed-registry-hash"

    monkeypatch.setattr(cp, "load_engine_registry", stub_load_engine_registry)

    first = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert first["capabilities"][0]["maxQuantity"] == 5

    # Simulate a format-contract edit: the resolved profile now reflects a
    # different max_quantity (as a real load_engine_registry would after
    # re-parsing an edited contracts.json), and the contracts FILE itself
    # changes (this is the only thing the fix can actually observe —
    # stub_load_engine_registry's own state change stands in for what a
    # real re-parse would produce).
    state["max_quantity"] = 9
    contracts_file.write_text('{"changed": true}')

    second = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert second["capabilities"][0]["maxQuantity"] == 9, (
        "served a cached bootstrap entry after a format-contract-only change"
    )


def test_prompt_catalog_only_change_is_reflected(job_path, tmp_path, monkeypatch):
    """(b) A prompt-catalog-only change at an unchanged job generation: an
    ai_video binding's maxQuantity (from plan_prompt_combinations, which
    reads the catalog for resolve_generation_recipe) must reflect the new
    value on the very next call."""
    _base_scaffold(monkeypatch)
    publication = {
        "recipeId": "some-format:master", "engine": "ai_video",
        "recipeVersion": "v1", "recipeSpecHash": "sha256:" + ("ab" * 32),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "fixed-registry-hash"))

    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text("{}")
    monkeypatch.setenv("CONTENT_LAB_PROMPT_CATALOG", str(catalog_file))

    stub_recipe = types.SimpleNamespace(
        recipe_id="some-format:master", engine="ai_video",
        prompt_catalog_hash="h", family_name="f", provider_model="m",
        clips_per_generation=1, planned_provider_calls=lambda n: min(n, 10),
    )
    monkeypatch.setattr(cp, "resolve_generation_recipe", lambda publication: stub_recipe)

    state = {"count": 3}
    monkeypatch.setattr(
        cp, "plan_prompt_combinations",
        lambda recipe, run_id, count, hashes, slots=None: list(range(state["count"])),
    )

    first = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert first["capabilities"][0]["maxQuantity"] == 3

    # Simulate a prompt-catalog edit: a real resolve_generation_recipe
    # would now plan differently (state change stands in for that); the
    # catalog FILE itself changes, which is what the fix can observe.
    state["count"] = 7
    catalog_file.write_text('{"changed": true}')

    second = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert second["capabilities"][0]["maxQuantity"] == 7, (
        "served a cached ai_video entry after a prompt-catalog-only change"
    )


def _wire_slideshow_binding(monkeypatch, state):
    publication = {
        "recipeId": "some-slideshow:master", "engine": "sourced_slideshow",
        "recipeVersion": "v1", "recipeSpecHash": "sha256:" + ("cd" * 32),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "fixed-registry-hash"))
    stub_recipe = types.SimpleNamespace(
        recipe_spec={}, max_quantity=10, executor_version="v1",
        library_id="lib-1",
    )
    monkeypatch.setattr(cp, "resolve_slideshow_recipe", lambda publication: stub_recipe)
    monkeypatch.setattr(
        cp, "resolve_material_profile", lambda publication, spec: object(),
    )
    monkeypatch.setattr(
        cp, "load_syzygy_library", lambda profile, master_pages: object(),
    )
    monkeypatch.setattr(
        cp, "plan_slideshows",
        lambda recipe, library, quantity, unavailable, seed: list(range(state["quantity"])),
    )
    return publication


def test_slideshow_page_never_served_from_cache(job_path, monkeypatch):
    """(c) A page with a registered slideshow binding must never be served
    a cached response: its live-library state (here, plan_slideshows'
    output, standing in for what a live Syzygy library fetch would now
    return) must be reflected on every call, with NOTHING else changing at
    all — no job write, no file touched, nothing but the live state."""
    _base_scaffold(monkeypatch)
    state = {"quantity": 2}
    _wire_slideshow_binding(monkeypatch, state)

    first = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert first["capabilities"][0]["maxQuantity"] == 2

    state["quantity"] = 5  # the live library "changed" — nothing else did
    second = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert second["capabilities"][0]["maxQuantity"] == 5, (
        "served a cached slideshow entry — the live library change was not reflected"
    )

    # And the cache must genuinely never have stored anything for this key:
    # not "expired fast", never written at all.
    assert cp._capabilities_cache == {}


def test_binding_switch_from_ai_video_to_slideshow_same_generation_not_stale(
    job_path, monkeypatch,
):
    """(d) A page whose registered bindings change from ai_video to
    slideshow between two calls, at the SAME job generation, must not be
    served the earlier ai_video response — the switch itself must be
    reflected immediately."""
    _base_scaffold(monkeypatch)
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "fixed-registry-hash"))

    ai_video_publication = {
        "recipeId": "gen-format:master", "engine": "ai_video",
        "recipeVersion": "v1", "recipeSpecHash": "sha256:" + ("ef" * 32),
    }
    stub_generation_recipe = types.SimpleNamespace(
        recipe_id="gen-format:master", engine="ai_video",
        prompt_catalog_hash="h", family_name="f", provider_model="m",
        clips_per_generation=1, planned_provider_calls=lambda n: min(n, 10),
    )
    monkeypatch.setattr(cp, "resolve_generation_recipe", lambda publication: stub_generation_recipe)
    monkeypatch.setattr(
        cp, "plan_prompt_combinations",
        lambda recipe, run_id, count, hashes, slots=None: list(range(4)),
    )

    bindings_state = {"current": [("k", ai_video_publication)]}
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: bindings_state["current"])

    first = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert first["capabilities"][0]["recipeId"] == "gen-format:master"

    slideshow_state = {"quantity": 6}
    slideshow_publication = _wire_slideshow_binding(monkeypatch, slideshow_state)
    bindings_state["current"] = [("k", slideshow_publication)]

    second = cp.capabilities(x_rt_page_id=PAGE_ID, x_page_id=None)
    assert len(second["capabilities"]) == 1
    assert second["capabilities"][0]["recipeId"] == "some-slideshow:master", (
        "switching a page's bindings from ai_video to slideshow at the same "
        "job generation served the stale ai_video response"
    )
    assert second["capabilities"][0]["maxQuantity"] == 6
