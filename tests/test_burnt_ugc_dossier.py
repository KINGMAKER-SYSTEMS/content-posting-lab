"""Actual Dossier catalog callers with isolated, page-scoped library fixtures."""
import hashlib
import json

import services.dossier_ingredients as ingredients
import services.control_plane_slideshows as slideshows
from tests.master_pages_fixtures import master_pages
from services.master_pages_contract import intent_hash
from services.lyric_timing import lyric_timing_catalog


def _install_owned_clip_library(monkeypatch):
    calls = []
    def owned_library(profile, intent, *, timeout):
        page = intent["pageId"]
        executor = (slideshows.LYRIC_VIDEO_EXECUTOR_PATH if page == "acct:burnt-mill-rd"
                    else slideshows.UGC_EXECUTOR_PATH)
        treatment = json.loads(executor.read_text())["pageTreatments"][page]
        curated = treatment["curatedVideoPlan"]
        lane = curated["lane"]
        subject = "burnt-mill-rd" if page == "acct:burnt-mill-rd" else "culture-core-1"
        address = slideshows.slideshow_library_address(profile, intent)
        expected = (f"syzygy:{lane}:{subject}", lane, subject)
        calls.append((page, profile.format_slug, address))
        if address != expected:
            raise ingredients.SyzygyError("fixture owned clip library does not match resolved address")
        objects = tuple(slideshows.SyzygyObject(
            f"{lane}/{subject}/clips/{row['file']}", row["bytes"],
            f"https://media.example.invalid/{row['file']}",
        ) for row in curated["sources"])
        snapshot = "\n".join(f"{item.key}:{item.bytes}" for item in objects)
        return slideshows.SyzygyLibrary(
            library_id=expected[0], lane=lane, subject=subject,
            objects=objects, snapshot_hash=hashlib.sha256(snapshot.encode()).hexdigest(),
        )
    monkeypatch.setattr(ingredients, "load_syzygy_library", owned_library)
    return calls


def _actual_version(page, *, lyric=False, controls=None):
    slug = "lyric-video" if lyric else "ugc-slideshow"
    intent, revision = master_pages(
        page, handle="burnt.mill.rd" if page == "acct:burnt-mill-rd" else "culture.core.1",
        content_niche="lyric-edits" if lyric else "slideshow",
        content_engine="lyrics_slideshows" if lyric else "sourced_slideshow",
    )
    if lyric:
        timings = lyric_timing_catalog(page_id=page, account="burnt.mill.rd")
        assert timings
        intent["notionPageId"] = timings[0]["notionPageId"]
        revision = intent_hash(intent)
    subject = "burnt-mill-rd" if page == "acct:burnt-mill-rd" else "culture-core-1"
    production = {"sourceLibraryId": f"syzygy:slideshow-webweaving:{subject}"}
    if lyric:
        production["controls"] = controls if controls is not None else {
            "lyricTimingSetId": "burnt-heavy-foot-chorus-v1"}
    catalog = ingredients.build_dossier_ingredient_catalog(page, intent, revision)
    direct = ingredients.selected_dossier_catalog_version(
        page, intent, revision, slug, production,
    )
    advertised = ingredients.catalog_selection_version(catalog, slug, production)
    assert direct == advertised
    return direct, catalog


# Filled from the successful exact-base real caller observation, never a local hash oracle.
BASELINE_CULTURE_SELECTION = "sha256:a4dd19491951de241978bdf619808b7377fc379c4ca92fcdfa3938a8a0f5c007"
BASELINE_BURNT_LYRIC_SELECTION = "sha256:a3f7cd8fdb628477fb80636a8798fc7f1b611c2d9319353ed07355588584abcb"


def test_burnt_ugc_catalog_and_public_selector_agree_on_owned_library(monkeypatch, record_property):
    calls = _install_owned_clip_library(monkeypatch)
    version, catalog = _actual_version("acct:burnt-mill-rd")
    assert version is not None and version.startswith("sha256:")
    assert "ugc-slideshow" in catalog["currentFormatCandidates"]
    entry = next(row for row in catalog["formats"] if row["formatId"] == "ugc-slideshow")
    source = next(row for row in entry["ingredients"] if row["ingredientId"] == "image-library")
    assert source["status"] == "bound"
    assert source["binding"]["libraryId"] == "syzygy:slideshow-webweaving:burnt-mill-rd"
    assert source["binding"]["mediaCount"] == 20
    assert calls and all(row[2] == (
        "syzygy:slideshow-webweaving:burnt-mill-rd", "slideshow-webweaving", "burnt-mill-rd"
    ) for row in calls)
    record_property("observedBurntUGCSelection", version)


def test_culture_actual_selection_changes_with_shared_ugc_authority(monkeypatch, record_property):
    _install_owned_clip_library(monkeypatch)
    version, catalog = _actual_version("acct:culture-core-1")
    assert version is not None and version.startswith("sha256:")
    assert version != BASELINE_CULTURE_SELECTION
    assert "ugc-slideshow" in catalog["currentFormatCandidates"]
    record_property("observedCultureSelection", version)
    record_property("actualBaselineCultureSelection", BASELINE_CULTURE_SELECTION)


def test_burnt_legacy_lyric_selection_and_control_refusal_are_preserved(monkeypatch, record_property):
    _install_owned_clip_library(monkeypatch)
    version, _ = _actual_version("acct:burnt-mill-rd", lyric=True)
    assert version == BASELINE_BURNT_LYRIC_SELECTION
    invalid, _ = _actual_version("acct:burnt-mill-rd", lyric=True,
        controls={"lyricTimingSetId": "test-only-wrong-timing"})
    assert invalid is None
    record_property("observedBurntLyricSelection", version)
