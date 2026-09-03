"""The ontology cannot advertise a label without its exact creative contract."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as control_plane
import routers.control_plane_recipes as recipes
from services.content_engine_registry import (
    REGISTRY_PATH,
    load_engine_registry,
)
from services.content_format_contracts import (
    CONTRACTS_PATH,
    CreativeAuthority,
    load_format_contracts,
)


def _entry_hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def test_every_known_format_has_a_strict_contract_and_registry_binding():
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    assert set(contracts) == set(profiles) == {
        "boat-lake", "coffee-tok", "construction-scenic", "lyric-edits",
        "meme-slideshow", "pov-dirt-bike", "pov-dusk-core",
        "pov-night-core", "pov-night-core-ai", "pov-scenic",
        "silhouette-truck", "truck-scenic", "truck-ugc",
    }
    for slug, profile in profiles.items():
        assert profile.format_contract_version == (
            "sha256:" + contracts[slug].contract_hash
        )


def test_only_complete_hash_bound_formats_are_commissioned():
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    assert {
        slug for slug, profile in profiles.items()
        if profile.execution_status == "commissioned"
    } == {
        "pov-dirt-bike", "pov-night-core", "pov-scenic", "truck-scenic",
    }
    assert all(
        contracts[slug].definition_status == "complete"
        for slug, profile in profiles.items()
        if profile.execution_status == "commissioned"
    )
    assert contracts["truck-ugc"].definition_status == "complete"
    assert profiles["truck-ugc"].execution_status == "uncommissioned"
    assert contracts["boat-lake"].definition_status == "incomplete"
    assert profiles["boat-lake"].execution_status == "uncommissioned"
    assert contracts["silhouette-truck"].definition_status == "complete"
    assert profiles["silhouette-truck"].execution_status == "uncommissioned"


def test_failed_ai_visual_libraries_expose_their_real_definition_gaps():
    contracts, _ = load_format_contracts()
    assert contracts["boat-lake"].definition_gaps == (
        "creativeAuthority", "duration", "lighting", "motion",
        "negativeRules", "referenceExamples", "reviewAuthority", "setting",
        "shotGrammar", "subject",
    )


def test_silhouette_contract_is_complete_but_stays_executorially_quarantined():
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    silhouette = contracts["silhouette-truck"]
    assert silhouette.definition_status == "complete"
    assert silhouette.definition_gaps == ()
    # The 8249ba1 quarantine stays in force: definition completion does not
    # re-commission the executor after a failed visual-DNA review.
    profile = profiles["silhouette-truck"]
    assert profile.execution_status == "uncommissioned"
    assert profile.executor_kind is None
    assert profile.executor_id is None
    assert profile.executor_version is None
    assert profile.max_quantity == 0
    # The restored rules are hash-bound to the live silhouette prompt family.
    from services.control_plane_generation import load_prompt_catalog
    _, catalog_hash = load_prompt_catalog()
    assert silhouette.creative_authority == CreativeAuthority(
        "prompt_family", "silhouette", f"sha256:{catalog_hash}",
    )
    assert silhouette.review_authority == (
        "promptCatalog.families.silhouette.quality_guards"
    )


def test_failed_legacy_archives_are_not_registered_recipes():
    recipes_dir = CONTRACTS_PATH.parent
    for marker in ("jetski-boat-20260722.json", "silhoutte.json"):
        value = json.loads((recipes_dir / marker).read_text())
        assert value["registered"] is False
        assert value["quarantinedReason"] == "failed_visual_dna_review"


def test_night_core_is_source_only_and_explicitly_rejects_trucks_and_ai():
    value = json.loads(CONTRACTS_PATH.read_text())["contracts"]["pov-night-core"]
    assert value["contentNiche"] == "POV — Night Core"
    assert value["contentEngine"] == "sourced_video"
    assert value["materialSource"] == "source_library"
    assert "not a truck page" in value["dimensions"]["subject"]["rule"]
    rejects = value["dimensions"]["negativeRules"]["rule"]
    assert "trucks" in rejects
    assert "AI-generated visuals" in rejects
    assert value["definitionStatus"] == "complete"
    assert value["definitionGaps"] == []
    assert value["creativeAuthority"]["kind"] == "source_dna_recut"
    assert value["reviewAuthority"] == "sourceDna.provenance"
    assert "cut_window_lineage" in value["reviewGates"]


def test_scenic_is_page_bound_source_recut_not_a_generated_substitute():
    value = json.loads(CONTRACTS_PATH.read_text())["contracts"]["pov-scenic"]
    assert value["contentNiche"] == "POV — Scenic"
    assert value["contentEngine"] == "sourced_video"
    assert value["materialSource"] == "source_library"
    assert value["definitionStatus"] == "complete"
    assert value["definitionGaps"] == []
    assert value["creativeAuthority"]["kind"] == "source_dna_recut"
    assert value["reviewAuthority"] == "sourceDna.provenance"
    assert "cut_window_lineage" in value["reviewGates"]
    assert "exact page" in value["dimensions"]["subject"]["rule"]
    rejects = value["dimensions"]["negativeRules"]["rule"]
    assert "cross-page" in rejects
    assert "not synthesized" in value["dimensions"]["setting"]["rule"]


def test_contract_drift_with_a_stale_registry_hash_fails_closed(
    monkeypatch, tmp_path,
):
    contracts = json.loads(CONTRACTS_PATH.read_text())
    contracts["contracts"]["truck-scenic"]["dimensions"]["subject"]["rule"] += " Changed."
    path = tmp_path / "format-contracts.json"
    path.write_text(json.dumps(contracts, sort_keys=True, separators=(",", ":")))
    monkeypatch.setenv("CONTENT_LAB_FORMAT_CONTRACTS", str(path))
    with pytest.raises(ValueError, match="diverges from its format contract"):
        load_engine_registry()


def test_an_incomplete_format_cannot_be_commissioned_by_adding_an_executor(
    monkeypatch, tmp_path,
):
    contracts = json.loads(CONTRACTS_PATH.read_text())
    coffee = contracts["contracts"]["coffee-tok"]
    coffee["creativeAuthority"] = {
        "kind": "prompt_family",
        "id": "coffee",
        "version": "sha256:" + "a" * 64,
    }
    coffee["definitionGaps"].remove("creativeAuthority")
    contracts_path = tmp_path / "format-contracts.json"
    contracts_path.write_text(json.dumps(
        contracts, sort_keys=True, separators=(",", ":"),
    ))

    registry = json.loads(REGISTRY_PATH.read_text())
    profile = registry["profiles"]["coffee-tok"]
    profile.update({
        "executionStatus": "commissioned",
        "executorKind": "prompt_family",
        "executorId": "coffee",
        "executorVersion": "sha256:" + "a" * 64,
        "formatContractVersion": "sha256:" + _entry_hash(coffee),
        "maxQuantity": 10,
    })
    registry_path = tmp_path / "engine-registry.json"
    registry_path.write_text(json.dumps(
        registry, sort_keys=True, separators=(",", ":"),
    ))
    monkeypatch.setenv("CONTENT_LAB_FORMAT_CONTRACTS", str(contracts_path))
    monkeypatch.setenv("CONTENT_LAB_ENGINE_REGISTRY", str(registry_path))
    with pytest.raises(ValueError, match="executor is invalid"):
        load_engine_registry()


def test_unimplemented_slideshow_engines_are_visible_but_not_executable():
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    assert contracts["lyric-edits"].content_engine == "lyrics_slideshows"
    assert contracts["meme-slideshow"].content_engine == "sourced_slideshow"
    assert profiles["lyric-edits"].execution_status == "uncommissioned"
    assert profiles["meme-slideshow"].execution_status == "uncommissioned"


def test_service_read_model_exposes_rules_and_gaps_without_promoting_them(
    monkeypatch,
):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "format-contract-test-token")
    app = FastAPI()
    app.include_router(control_plane.router, prefix="/api/control-plane")
    client = TestClient(app)
    endpoint = "/api/control-plane/v1/format-contracts"
    assert client.get(endpoint).status_code == 400
    assert client.get(endpoint, headers={
        "X-RT-Lane": recipes.LANE,
    }).status_code == 401
    response = client.get(endpoint, headers={
        "X-RT-Lane": recipes.LANE,
        "Authorization": "Bearer format-contract-test-token",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "content-lab.format-contract-status.v1"
    assert payload["contractsRegistryVersion"].startswith("sha256:")
    by_slug = {row["formatSlug"]: row for row in payload["formats"]}
    assert by_slug["truck-scenic"]["executor"]["id"] == "truck"
    assert by_slug["coffee-tok"]["executor"] is None
    assert by_slug["coffee-tok"]["definitionStatus"] == "incomplete"
    assert "subject" in by_slug["coffee-tok"]["definitionGaps"]
    assert "not a truck page" in (
        by_slug["pov-night-core"]["dimensions"]["subject"]["rule"]
    )
