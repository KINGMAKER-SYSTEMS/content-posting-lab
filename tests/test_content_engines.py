"""THE ONE ENGINE LIST (B9): engines come from recipes/content-engines.v1.json.

A new engine on an executor kind the Lab already has code for is a config
change: add it to the engines file, give it formats in the two registries, and
GET /v1/format-contracts serves it with a hash the Worker checks.
"""

import copy
import hashlib
import json

import pytest

# Imported at module level ON PURPOSE: the router reads the engines file once at
# import, so a test that sets CONTENT_LAB_CONTENT_ENGINES must not be the first
# importer (its tmp engine would leak into the import-time family sets).
import routers.control_plane as control_plane
from services import content_engine_registry as registry
from services.content_engine_registry import (
    ENGINES_PATH,
    REGISTRY_PATH,
    allowed_bindings,
    canonical_engines_hash,
    engine_family,
    engines_in_family,
    load_content_engines,
    load_engine_registry,
)
from services.content_format_contracts import CONTRACTS_PATH, load_format_contracts

# What the Python literal said on 2026-11-22, before the file replaced it.
BINDINGS_BEFORE = {
    "ai_video": ("generated_video", "prompt_family"),
    "sourced_video": ("source_library", "source_dna_recut"),
    "sourced_slideshow": ("image_library", "slideshow_renderer"),
    "sourced_image": ("image_library", "slideshow_renderer"),
    "lyrics_slideshows": ("lyric_image_library", "slideshow_renderer"),
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def test_the_committed_engines_file_is_exactly_the_old_literal():
    engines, version = load_content_engines()
    assert allowed_bindings() == BINDINGS_BEFORE
    assert set(engines) == set(BINDINGS_BEFORE)
    assert version.startswith("sha256:") and len(version) == 71
    # The hash is over canonical JSON of the list, not the file's bytes, so the
    # Worker can recompute it from what it receives.
    expected = hashlib.sha256(_canonical({
        engine: {"executorKind": e.executor_kind, "label": e.label, "materialSource": e.material_source}
        for engine, e in engines.items()
    }).encode("utf-8")).hexdigest()
    assert version == "sha256:" + expected == canonical_engines_hash(engines)


def test_families_follow_the_executor_kind():
    assert engine_family("ai_video") == "generated"
    assert engine_family("sourced_video") == "sourced"
    assert engine_family("sourced_slideshow") == engine_family("lyrics_slideshows") == "slideshow"
    assert engine_family("not_an_engine") is None
    assert engine_family(None) is None
    assert engines_in_family("slideshow") == frozenset({"sourced_slideshow", "sourced_image", "lyrics_slideshows"})
    assert engines_in_family("generated") == frozenset({"ai_video"})


def test_every_registry_profile_names_a_listed_engine():
    engines, _ = load_content_engines()
    profiles, _ = load_engine_registry()
    assert {profile.content_engine for profile in profiles.values()} <= set(engines)
    for profile in profiles.values():
        assert profile.family == engines[profile.content_engine].family


def _write(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(_canonical(value))
    return path


def _with_test_engine(tmp_path, monkeypatch, *, engine="test_engine_x", executor_kind="slideshow_renderer",
                      material_source="image_library", fields=None, tag=""):
    """Add engine + one commissioned format for it, purely in config files."""
    engines_file = json.loads(ENGINES_PATH.read_text())
    entry = {"label": "Test engine X", "materialSource": material_source, "executorKind": executor_kind}
    if fields is not None:
        entry = fields
    engines_file["engines"][engine] = entry
    monkeypatch.setenv("CONTENT_LAB_CONTENT_ENGINES", str(_write(tmp_path, f"engines{tag}.json", engines_file)))

    contracts = json.loads(CONTRACTS_PATH.read_text())
    contract = copy.deepcopy(contracts["contracts"]["meme-slideshow"])
    contract["contentNiche"] = "TESTX"
    contract["contentEngine"] = engine
    contract["materialSource"] = material_source
    contracts["contracts"]["test-format-x"] = contract
    monkeypatch.setenv("CONTENT_LAB_FORMAT_CONTRACTS", str(_write(tmp_path, f"contracts{tag}.json", contracts)))

    registry_file = json.loads(REGISTRY_PATH.read_text())
    profile = copy.deepcopy(registry_file["profiles"]["meme-slideshow"])
    profile["contentNiche"] = "TESTX"
    profile["contentEngine"] = engine
    profile["materialSource"] = material_source
    profile["formatContractVersion"] = "sha256:" + _entry_hash(contract)
    registry_file["profiles"]["test-format-x"] = profile
    monkeypatch.setenv("CONTENT_LAB_ENGINE_REGISTRY", str(_write(tmp_path, f"registry{tag}.json", registry_file)))


def test_a_new_engine_on_an_existing_executor_kind_loads_and_is_served(tmp_path, monkeypatch):
    _with_test_engine(tmp_path, monkeypatch)
    engines, version = load_content_engines()
    assert engines["test_engine_x"].label == "Test engine X"
    assert engine_family("test_engine_x") == "slideshow"
    profiles, _ = load_engine_registry()
    profile = profiles["test-format-x"]
    assert profile.content_engine == "test_engine_x"
    assert profile.execution_status == "commissioned"
    assert profile.executor_kind == "slideshow_renderer"

    # Served to the Worker: the list, its version, and the format on it.
    snapshot = control_plane._format_contract_snapshot()
    assert snapshot["contentEnginesVersion"] == version
    served = {entry["engine"]: entry for entry in snapshot["engines"]}
    assert served["test_engine_x"] == {
        "engine": "test_engine_x", "label": "Test engine X",
        "materialSource": "image_library", "executorKind": "slideshow_renderer",
    }
    assert [entry["engine"] for entry in snapshot["engines"]] == sorted(served)
    test_format = next(f for f in snapshot["formats"] if f["formatSlug"] == "test-format-x")
    assert test_format["contentEngine"] == "test_engine_x"
    assert test_format["contentNiche"] == "TESTX"
    assert test_format["executionStatus"] == "commissioned"
    # Existing engines are untouched by the addition.
    assert {e for e in served if e != "test_engine_x"} == set(BINDINGS_BEFORE)


def test_an_engine_on_an_executor_kind_without_code_is_refused(tmp_path, monkeypatch):
    _with_test_engine(tmp_path, monkeypatch, executor_kind="hologram_renderer")
    with pytest.raises(ValueError, match="test_engine_x is invalid"):
        load_content_engines()
    with pytest.raises(ValueError):
        load_engine_registry()


def test_an_engine_binding_material_its_executor_cannot_read_is_refused(tmp_path, monkeypatch):
    _with_test_engine(tmp_path, monkeypatch, executor_kind="prompt_family", material_source="image_library")
    with pytest.raises(ValueError, match="binds material its executor cannot read"):
        load_content_engines()


def test_a_format_on_an_engine_the_list_lacks_is_refused(tmp_path, monkeypatch):
    _with_test_engine(tmp_path, monkeypatch)
    engines_file = json.loads(ENGINES_PATH.read_text())  # without test_engine_x
    monkeypatch.setenv("CONTENT_LAB_CONTENT_ENGINES", str(_write(tmp_path, "engines-short.json", engines_file)))
    with pytest.raises(ValueError, match="test-format-x is invalid"):
        load_engine_registry()


@pytest.mark.parametrize("bad_id", ["AiVideo", "1video", "a" * 41, "ai-video"])
def test_engine_ids_are_lowercase_identifiers(tmp_path, monkeypatch, bad_id):
    _with_test_engine(tmp_path, monkeypatch, engine=bad_id)
    with pytest.raises(ValueError, match="content engine id is invalid"):
        load_content_engines()


def test_unknown_fields_and_empty_lists_fail_closed(tmp_path, monkeypatch):
    _with_test_engine(tmp_path, monkeypatch, fields={
        "label": "X", "materialSource": "image_library", "executorKind": "slideshow_renderer", "extra": 1,
    })
    with pytest.raises(ValueError, match="fields are invalid"):
        load_content_engines()
    monkeypatch.setenv("CONTENT_LAB_CONTENT_ENGINES", str(_write(tmp_path, "empty.json", {
        "schema": registry.ENGINES_SCHEMA, "engines": {},
    })))
    with pytest.raises(ValueError, match="content engines file is invalid"):
        load_content_engines()


def test_dispatch_asks_the_family_not_the_name(tmp_path, monkeypatch):
    """Each family's recipe resolver admits a NEW engine of its family by the
    engines file alone, and refuses an engine of another family before it
    reads any spec. The spec reader is replaced by a sentinel so "passed the
    engine gate" is observable without building a whole publication."""
    import services.control_plane_generation as generation
    import services.control_plane_slideshows as slideshows
    import services.control_plane_sources as sources

    class PassedTheEngineGate(Exception):
        pass

    def sentinel(_publication):
        raise PassedTheEngineGate()

    # Generated family.
    _with_test_engine(tmp_path, monkeypatch, engine="test_gen", executor_kind="prompt_family",
                      material_source="generated_video", tag="gen")
    assert engine_family("test_gen") == "generated"
    monkeypatch.setattr(generation, "_typed_recipe_spec", sentinel)
    with pytest.raises(PassedTheEngineGate):
        generation._resolve_generation_recipe({"recipeId": "x:master", "engine": "test_gen"}, require_runtime=False)
    with pytest.raises(generation._GenerationUnavailable, match="content_engine"):
        generation._resolve_generation_recipe({"recipeId": "x:master", "engine": "sourced_video"}, require_runtime=False)

    # Sourced family.
    _with_test_engine(tmp_path, monkeypatch, engine="test_src", executor_kind="source_dna_recut",
                      material_source="source_library", tag="src")
    assert engine_family("test_src") == "sourced"
    monkeypatch.setattr(sources, "typed_recipe_spec", sentinel)
    with pytest.raises(PassedTheEngineGate):
        sources.resolve_source_recipe({"recipeId": "x:master", "engine": "test_src"})
    assert sources.resolve_source_recipe({"recipeId": "x:master", "engine": "ai_video"}) is None

    # Slideshow family.
    _with_test_engine(tmp_path, monkeypatch, engine="test_sl", tag="sl")
    assert engine_family("test_sl") == "slideshow"
    monkeypatch.setattr(slideshows, "typed_recipe_spec", sentinel)
    with pytest.raises(PassedTheEngineGate):
        slideshows.resolve_slideshow_recipe({"recipeId": "x:master", "engine": "test_sl"})
    assert slideshows.resolve_slideshow_recipe({"recipeId": "x:master", "engine": "ai_video"}) is None
    # An engine no file names belongs to no family.
    assert slideshows.resolve_slideshow_recipe({"recipeId": "x:master", "engine": "never_listed"}) is None


def test_the_router_reads_the_engines_file_once_at_import_and_loudly():
    """The import-time family sets are the committed five, by family, and a
    broken file is a boot failure, not three empty sets."""
    assert control_plane._GENERATED_ENGINES == frozenset({"ai_video"})
    assert control_plane._SOURCED_ENGINES == frozenset({"sourced_video"})
    assert control_plane._SLIDESHOW_ENGINES == frozenset({"sourced_slideshow", "sourced_image", "lyrics_slideshows"})
    import importlib
    import pytest as _pytest
    import routers.control_plane as module
    import os
    before = os.environ.get("CONTENT_LAB_CONTENT_ENGINES")
    os.environ["CONTENT_LAB_CONTENT_ENGINES"] = "/nonexistent/engines.json"
    try:
        with _pytest.raises(OSError):
            importlib.reload(module)
    finally:
        if before is None:
            os.environ.pop("CONTENT_LAB_CONTENT_ENGINES", None)
        else:
            os.environ["CONTENT_LAB_CONTENT_ENGINES"] = before
        importlib.reload(module)
    assert module._SOURCED_ENGINES == frozenset({"sourced_video"})
