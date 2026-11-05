"""F-XMAS-SLIDESHOW-CANDIDATES-1104.

tis.the.szn7 and all.i.want.for.xma were retagged Notion Content Niche = XMAS
on 2026-11-04 while keeping the sourced slideshow engine. The catalog matched
format contracts by exact niche, so both pages were offered no format at all
(currentFormatCandidates == []) and their Dossier revert publish failed 409
dossier_master_pages_format_mismatch. A Christmas sourced-slideshow page is
offered exactly what a plain slideshow page is offered; the AI Christmas page
keeps pov-seasonal only.
"""
from services.content_format_contracts import page_niche_admits
from tests.master_pages_fixtures import master_pages

PAGE_ID = "acct:tis-the-szn7"


def _catalog(monkeypatch, niche, engine, handle="tis.the.szn7"):
    import services.dossier_ingredients as ingredients

    def unavailable(profile, master_pages_, *, timeout):
        raise ingredients.SyzygyError("test unavailable")

    monkeypatch.setattr(ingredients, "load_syzygy_library", unavailable)
    intent, revision = master_pages(PAGE_ID, handle=handle, content_niche=niche, content_engine=engine)
    return ingredients, intent, revision, ingredients.build_dossier_ingredient_catalog(PAGE_ID, intent, revision)


def test_christmas_sourced_slideshow_page_is_offered_the_slideshow_formats(monkeypatch):
    _, _, _, slideshow = _catalog(monkeypatch, "slideshow", "sourced_slideshow")
    _, _, _, xmas = _catalog(monkeypatch, "XMAS", "sourced_slideshow")
    assert "image-video" in slideshow["currentFormatCandidates"]
    assert "ugc-slideshow" in slideshow["currentFormatCandidates"]
    assert xmas["currentFormatCandidates"] == slideshow["currentFormatCandidates"]


def test_christmas_ai_page_keeps_only_pov_seasonal(monkeypatch):
    _, _, _, xmas_ai = _catalog(monkeypatch, "XMAS", "ai_video", handle="joy.to.the.world709")
    assert xmas_ai["currentFormatCandidates"] == ["pov-seasonal"]


def test_selected_version_resolves_image_video_for_a_christmas_slideshow_page(monkeypatch):
    ingredients, intent, revision, catalog = _catalog(monkeypatch, "XMAS", "sourced_slideshow")
    entry = next(e for e in catalog["formats"] if e["formatId"] == "image-video")
    assert entry["productionSelections"], "image-video must offer a production selection"
    selection = entry["productionSelections"][0]
    production = {key: value for key, value in selection.items() if key != "catalogVersion"}
    # Before the fix the exact niche check returned None here (no selectable format).
    assert ingredients.selected_dossier_catalog_version(
        PAGE_ID, intent, revision, "image-video", production,
    ) == selection["catalogVersion"]


def test_niche_alias_is_exactly_xmas_on_the_sourced_slideshow_engine():
    assert page_niche_admits("slideshow", "XMAS", "sourced_slideshow")
    assert page_niche_admits("XMAS", "XMAS", "ai_video")
    assert not page_niche_admits("slideshow", "XMAS", "ai_video")
    assert not page_niche_admits("slideshow", "xmas", "sourced_slideshow")
    assert not page_niche_admits("meme", "XMAS", "sourced_slideshow")
    assert not page_niche_admits("slideshow", "TRUCK", "sourced_slideshow")
    assert not page_niche_admits(None, "XMAS", "sourced_slideshow")
