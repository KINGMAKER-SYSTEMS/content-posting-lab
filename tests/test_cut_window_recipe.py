"""source.recipe and the CONTENT_LAB_ADVERTISE_ORIGIN wire gate (gap G39).

The Worker validates `contentLabProvenance.source` against EXACT field
allow-lists (control-plane-worker src/adapters/contentLabClient.js,
ARTIFACT_SOURCE_DNA_FIELDS / ARTIFACT_SOURCE_DNA_MASTER_FIELDS /
ARTIFACT_SOURCE_DNA_CUT_FIELDS), so `source.master.origin`
(o-origin-restore) and the new `source.recipe` — a SIBLING of
master/cutWindow, the contract agreed with originality and growth-brain's
Worker allow-list change g-worker-allow-origin-1028 — reach the wire only
behind CONTENT_LAB_ADVERTISE_ORIGIN (default off). Flag off, every receipt
body keeps exactly the base baa2106 key sets; in EVERY mode cutWindow
keeps exactly the Worker's five cut fields.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import routers.control_plane as cp
from services.clip_transforms import cut_transforms
from services.control_plane_sources import RECIPE_FIELDS, cut_window_recipe
from services.slot_source_regeneration import canonical_json, expected_master_lineage
from tests.test_control_plane_source_execution import (
    MASTER_SHA,
    PAGE_ID,
    _pin_cuts_to_first_frame,
    _write_av_test_clip,
    headers,
    job_body,
    lab,
    publication,
)
from tests.source_dna_fixture import isolate_source_dna_registry

NEUTRAL_CROP = {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    """Retired chase/BTL libraries are deleted, so source-dna tests run against
    an isolated copy of the registry plus the non-retired dirt-bike fixture."""
    isolate_source_dna_registry(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _offline_shipstream_manifest(monkeypatch):
    """The same offline vault the execution tests pin (their autouse fixture
    is module-scoped): any manifest read answers unreachable instead of
    leaving the process."""
    import services.shipstream_source_manifest as source_manifest

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable(
            "ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)
# The exact sets the Worker's contentLabClient.js asserts (assertExactFields):
# a receipt with any other key under source.master / source.cutWindow is
# refused with CONTENT_LAB_SCHEMA_MISMATCH, and the source object itself is
# allow-listed too (ARTIFACT_SOURCE_DNA_FIELDS) — `source.recipe` rides the
# g-worker-allow-origin-1028 Worker change, so the Lab gates it behind
# CONTENT_LAB_ADVERTISE_ORIGIN. cutWindow keeps EXACTLY these five fields
# in every mode (recipe is a SIBLING, never a cutWindow key).
WORKER_MASTER_FIELDS = {"sourceId", "sha256", "bytes", "filename", "mimeType",
                        "storageKey", "durationMs", "sourceOffsetMs", "provenance"}
WORKER_CUT_FIELDS = {"libraryStartMs", "libraryEndMs", "durationMs",
                     "originalStartMs", "originalEndMs"}


async def _run_one_cut_job(client, tmp_path, monkeypatch, payload, *, vary=False):
    if payload["recipeVersion"] != "dossier-feedfacefeedface":
        # The lab fixture registered the default publication; any other
        # version registers itself first, exactly like the execution tests.
        assert client.post(
            "/api/control-plane/v1/recipes", json=payload,
            headers=headers("recipe-register-" + payload["recipeVersion"]),
        ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("recipe-job-" + payload["recipeVersion"]),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=15.0)

    async def cached_source(*_args):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    if vary:
        monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    return job


# ---------------------------------------------------------------------------
# The emitter itself: exact field names and the ADDENDUM 2 types.
# ---------------------------------------------------------------------------

def test_a_speed_half_cut_with_reverse_emits_only_speed_and_reverse():
    assert cut_window_recipe(0.5, NEUTRAL_CROP, reverse=True) == {
        "speed": 0.5, "reverse": True, "loop_split": None,
        "crop": None, "hflip": None,
    }


def test_a_plain_cut_emits_all_null():
    assert cut_window_recipe(1.0, NEUTRAL_CROP) == {
        "speed": None, "reverse": None, "loop_split": None,
        "crop": None, "hflip": None,
    }


@pytest.mark.parametrize("speed", [
    0.4, 0.5, 0.6, 0.78, 0.8, 1.1, 1.12, 1.3, 1.5, 2.0,
])
def test_speed_boundaries_are_finite_and_positive(speed):
    assert cut_window_recipe(speed, NEUTRAL_CROP)["speed"] == speed


@pytest.mark.parametrize("speed", [0.0, -0.5, float("inf"), float("nan")])
def test_invalid_speeds_are_refused(speed):
    with pytest.raises(ValueError):
        cut_window_recipe(speed, NEUTRAL_CROP)


@pytest.mark.parametrize("crop,expected", [
    ({"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}, None),  # neutral: not applied
    ({"zoom": 3.0, "focusX": 0.0, "focusY": 1.0},
     {"zoom": 3.0, "focusX": 0.0, "focusY": 1.0}),  # inclusive bounds
    ({"zoom": 1.2, "focusX": 0.42, "focusY": 0.58},
     {"zoom": 1.2, "focusX": 0.42, "focusY": 0.58}),
    (None, None),  # unknown: legacy recipes carry no crop
])
def test_crop_is_the_clipcrop_shape_or_null(crop, expected):
    assert cut_window_recipe(1.0, crop)["crop"] == expected


@pytest.mark.parametrize("crop", [
    {"zoom": 0.99, "focusX": 0.5, "focusY": 0.5},
    {"zoom": 3.01, "focusX": 0.5, "focusY": 0.5},
    {"zoom": 1.5, "focusX": -0.01, "focusY": 0.5},
    {"zoom": 1.5, "focusX": 0.5, "focusY": 1.01},
    {"zoom": 1.5, "focusX": 0.5},  # not exactly the three clipCrop fields
])
def test_out_of_domain_crops_are_refused(crop):
    with pytest.raises(ValueError):
        cut_window_recipe(1.0, crop)


def test_flags_are_true_or_null_never_false():
    for flags in ((True, True, True), (False, None, None), (None, True, None)):
        recipe = cut_window_recipe(1.0, NEUTRAL_CROP, reverse=flags[0],
                                   hflip=flags[1], loop_split=flags[2])
        for key in ("reverse", "hflip", "loop_split"):
            assert recipe[key] is True or recipe[key] is None


def test_transforms_map_to_reverse_and_hflip():
    drawn = cut_transforms(PAGE_ID, f"{MASTER_SHA}:0:6000")
    recipe = cut_window_recipe(1.0, NEUTRAL_CROP,
                               reverse=drawn.reverse or None,
                               hflip=drawn.mirror or None)
    assert recipe["reverse"] is (True if drawn.reverse else None)
    assert recipe["hflip"] is (True if drawn.mirror else None)


# ---------------------------------------------------------------------------
# The wire: flag off keeps the base receipt keys; flag on adds recipe/origin.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_receipt_keeps_exactly_the_base_keys(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.delenv(cp.ADVERTISE_ORIGIN_ENV, raising=False)
    job = await _run_one_cut_job(client, tmp_path, monkeypatch, publication())
    source = job["clips"][0]["source"]
    assert set(source["cutWindow"]) == WORKER_CUT_FIELDS
    assert set(source["master"]) == WORKER_MASTER_FIELDS
    assert "recipe" not in source  # no sibling leg either: base baa2106 keys
    canonical = canonical_json(source)
    assert '"recipe"' not in canonical and '"origin"' not in canonical


@pytest.mark.asyncio
async def test_flag_off_master_with_origin_still_keeps_the_base_master_keys(
    lab, monkeypatch,
):
    """Flag off, an origin-bearing master (o-origin-restore) still does NOT
    put origin on the wire: the Worker's master allow-list refuses it until
    g-worker-allow-origin-1028 lands. Deleting advertise_origin() from the
    master emission site would fail here."""
    client, tmp_path, _ = lab
    monkeypatch.delenv(cp.ADVERTISE_ORIGIN_ENV, raising=False)
    resolve = cp._dossier_source_recipe

    def with_origin(payload):
        recipe = resolve(payload)
        return replace(recipe, masters=tuple(
            replace(master, origin={
                "originSourceId": "vt5im2TRAKw",
                "originStartMs": 120_000,
                "originEndMs": 300_000,
            }) for master in recipe.masters
        ))

    monkeypatch.setattr(cp, "_dossier_source_recipe", with_origin)
    job = await _run_one_cut_job(client, tmp_path, monkeypatch, publication())
    source = job["clips"][0]["source"]
    assert set(source["master"]) == WORKER_MASTER_FIELDS
    assert '"origin"' not in canonical_json(source)


@pytest.mark.asyncio
async def test_flag_on_a_half_speed_cut_advertises_the_actual_recipe(
    lab, monkeypatch,
):
    client, tmp_path, _ = lab
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    job = await _run_one_cut_job(
        client, tmp_path, monkeypatch,
        publication(clip_speed=0.5, recipe_version="dossier-halfspeed"),
    )
    source = job["clips"][0]["source"]
    # The CONTRACT: recipe is a SIBLING of master/cutWindow, and cutWindow
    # keeps exactly the Worker's five cut fields (the Worker allow-lists
    # cutWindow with assertExactFields, so a recipe key there would be
    # refused with CONTENT_LAB_SCHEMA_MISMATCH even once the flag is on).
    assert set(source["cutWindow"]) == WORKER_CUT_FIELDS
    assert source["recipe"] == {
        "speed": 0.5, "reverse": None, "loop_split": None,
        "crop": None, "hflip": None,
    }
    # The recipe speed is the ACTUAL render speed (the manifest's clipSpeed).
    assert source["recipe"]["speed"] == job["clips"][0]["clipSpeed"]


@pytest.mark.asyncio
async def test_flag_on_recipe_is_a_sibling_of_cutWindow_never_inside_it(
    lab, monkeypatch,
):
    """The agreed wire contract (originality ledger / growth-brain
    g-worker-allow-origin-1028): contentLabProvenance.source.recipe sits
    BESIDE source.master and source.cutWindow. A recipe key inside cutWindow
    is refused by the Worker's ARTIFACT_SOURCE_DNA_CUT_FIELDS
    assertExactFields even with the flag on (the 10-28 outage mechanism)."""
    client, tmp_path, _ = lab
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    job = await _run_one_cut_job(
        client, tmp_path, monkeypatch,
        publication(clip_speed=0.5, recipe_version="dossier-halfspeed"),
    )
    source = job["clips"][0]["source"]
    assert "recipe" in source  # the sibling leg is on the wire ...
    assert "recipe" not in source["cutWindow"]  # ... and ONLY there
    assert set(source["cutWindow"]) == WORKER_CUT_FIELDS
    assert set(source["recipe"]) == set(RECIPE_FIELDS)  # all five, always


@pytest.mark.asyncio
async def test_flag_on_a_vary_cut_records_its_drawn_look(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    job = await _run_one_cut_job(
        client, tmp_path, monkeypatch,
        publication(recipe_version="dossier-varyrecipe"), vary=True,
    )
    clip = job["clips"][0]
    drawn = cut_transforms(
        PAGE_ID, f"{MASTER_SHA}:0:{clip['source']['cutWindow']['durationMs']}")
    source = clip["source"]
    recipe = source["recipe"]
    # The contract again, on a varied cut: cutWindow stays exactly the five
    # Worker cut fields; recipe sits BESIDE it under source.
    assert set(source["cutWindow"]) == WORKER_CUT_FIELDS
    assert recipe["speed"] == clip["clipSpeed"] != 1.0  # the drawn speed
    assert recipe["reverse"] is (True if drawn.reverse else None)
    assert recipe["hflip"] is (True if drawn.mirror else None)
    assert recipe["loop_split"] is None  # Layer B vary cuts are not the split loop
    assert recipe["crop"] == clip["clipCrop"]  # the drawn crop, as drawn


@pytest.mark.asyncio
async def test_flag_on_a_reversed_slot_emits_reverse_true(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("recipe-job-reverseslot"),
    )
    job_id = response.json()["jobId"]
    # Pin to a quarter-second start whose deterministic transform draw reverses
    # (the fixture page/master ids have no reversing whole-second start below 9 s).
    start = next(
        candidate for candidate in range(0, 9_000, 250)
        if cut_transforms(PAGE_ID, f"{MASTER_SHA}:{candidate}:6000").reverse
    )
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:{start}:6000",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": start,
        "startMs": start,
        "durationMs": 6_000,
    }]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=15.0)

    async def cached_source(*_args):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    source = job["clips"][0]["source"]
    assert set(source["cutWindow"]) == WORKER_CUT_FIELDS
    recipe = source["recipe"]
    assert recipe["reverse"] is True
    assert recipe["loop_split"] is None
    assert recipe["hflip"] is True or recipe["hflip"] is None


# ---------------------------------------------------------------------------
# Slot-source regeneration compares master lineage under the same gate.
# ---------------------------------------------------------------------------

def _lineage_master(sha):
    from services.source_dna_registry import MasterSource
    return MasterSource(
        "src-1", sha, 1_000, f"{sha}.mp4", "video/mp4",
        f"vault/unit/{sha}.mp4", 600_000, 120_000,
        {"sourceUrl": "https://unit.test/a.mp4",
         "acquiredAt": "2027-01-15T12:00:00+00:00", "authority": "unit"},
        {"originSourceId": "1uMmco2zNZc", "originStartMs": 120_000,
         "originEndMs": 720_000},
    )


def test_master_lineage_expectation_follows_the_advertise_origin_flag(monkeypatch):
    master = _lineage_master("a" * 64)
    monkeypatch.delenv(cp.ADVERTISE_ORIGIN_ENV, raising=False)
    without = expected_master_lineage(master)
    assert set(without) == WORKER_MASTER_FIELDS
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    with_origin = expected_master_lineage(master)
    assert with_origin["origin"] == master.origin
    # One gate on both sides: a flag-off receipt never carries origin, and the
    # flag-off expectation never demands one.
    assert without != with_origin
    assert cp.advertise_origin() is True


def test_a_master_without_origin_never_expects_one(monkeypatch):
    master = replace(_lineage_master("b" * 64), origin=None)
    monkeypatch.setenv(cp.ADVERTISE_ORIGIN_ENV, "1")
    assert set(expected_master_lineage(master)) == WORKER_MASTER_FIELDS
