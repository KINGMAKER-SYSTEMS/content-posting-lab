"""F-XMAS-SLIDESHOW-CANDIDATES-1104.

tis.the.szn7 and all.i.want.for.xma were retagged Notion Content Niche = XMAS
on 2026-11-04 while keeping the sourced slideshow engine. The catalog matched
format contracts by exact niche, so both pages were offered no format at all
(currentFormatCandidates == []) and their Dossier revert publish failed 409
dossier_master_pages_format_mismatch. A Christmas sourced-slideshow page keeps
the generic UGC option; the page-bound image-video option belongs only to
PenPal. The AI Christmas page keeps pov-seasonal only.
"""
from services.content_format_contracts import page_niche_admits
from services.content_engine_registry import resolve_material_profile
from services.master_pages_contract import intent_hash
from tests.master_pages_fixtures import master_pages

PAGE_ID = "acct:tis-the-szn7"


def _catalog(monkeypatch, niche, engine, handle="tis.the.szn7", page_id=PAGE_ID):
    import services.dossier_ingredients as ingredients

    def unavailable(profile, master_pages_, *, timeout):
        raise ingredients.SyzygyError("test unavailable")

    monkeypatch.setattr(ingredients, "load_syzygy_library", unavailable)
    intent, revision = master_pages(page_id, handle=handle, content_niche=niche, content_engine=engine)
    return ingredients, intent, revision, ingredients.build_dossier_ingredient_catalog(page_id, intent, revision)


def test_christmas_sourced_slideshow_page_keeps_ugc_without_pen_pal_photo_plan(monkeypatch):
    _, _, _, slideshow = _catalog(monkeypatch, "slideshow", "sourced_slideshow")
    _, _, _, xmas = _catalog(monkeypatch, "XMAS", "sourced_slideshow")
    assert slideshow["currentFormatCandidates"] == ["ugc-slideshow"]
    assert xmas["currentFormatCandidates"] == ["ugc-slideshow"]


def test_other_sourced_slideshow_niche_offers_only_generic_ugc(monkeypatch):
    _, intent, _, catalog = _catalog(
        monkeypatch, "TRUCK", "sourced_slideshow",
        handle="truck.slides", page_id="acct:truck-slides",
    )
    assert catalog["currentFormatCandidates"] == ["ugc-slideshow"]
    profile = resolve_material_profile(
        {"recipeId": "ugc-slideshow:master", "engine": "sourced_slideshow"},
        {"masterPages": intent, "demand": {"formatMix": {"ugc-slideshow": 1}}},
    )
    assert profile is not None and profile.format_slug == "ugc-slideshow"


def test_other_niche_ugc_selection_resolves_from_its_own_library(monkeypatch):
    import services.dossier_ingredients as ingredients
    from services.control_plane_slideshows import SyzygyLibrary, SyzygyObject

    page_id = "acct:truck-slides"
    intent, revision = master_pages(
        page_id, handle="truck.slides", content_niche="TRUCK", content_engine="sourced_slideshow",
    )
    library = SyzygyLibrary(
        library_id="syzygy:ugc:truck-slides", lane="ugc", subject="truck-slides",
        objects=tuple(SyzygyObject(
            f"ugc/truck-slides/photos/photo-{index}.jpg", 100 + index,
            f"https://r2.example/ugc/truck-slides/photos/photo-{index}.jpg",
        ) for index in range(3)),
        snapshot_hash="a" * 64,
    )
    monkeypatch.setattr(ingredients, "load_syzygy_library", lambda *_args, **_kwargs: library)
    catalog = ingredients.build_dossier_ingredient_catalog(page_id, intent, revision)
    entry = next(row for row in catalog["formats"] if row["formatId"] == "ugc-slideshow")
    selection = next(row for row in entry["productionSelections"] if row["sourceLibraryId"] == library.library_id)
    production = {key: value for key, value in selection.items() if key != "catalogVersion"}
    assert ingredients.selected_dossier_catalog_version(
        page_id, intent, revision, "ugc-slideshow", production,
    ) == selection["catalogVersion"]


def test_coffee_keeps_its_own_slideshow_format(monkeypatch):
    _, _, _, catalog = _catalog(monkeypatch, "Coffee", "sourced_slideshow")
    assert catalog["currentFormatCandidates"] == ["coffee-slideshow"]


def test_christmas_ai_page_keeps_only_pov_seasonal(monkeypatch):
    _, _, _, xmas_ai = _catalog(monkeypatch, "XMAS", "ai_video", handle="joy.to.the.world709")
    assert xmas_ai["currentFormatCandidates"] == ["pov-seasonal"]


def test_selected_version_refuses_image_video_for_a_non_pen_pal_page(monkeypatch):
    ingredients, intent, revision, _ = _catalog(monkeypatch, "XMAS", "sourced_slideshow")
    assert ingredients.selected_dossier_catalog_version(
        PAGE_ID, intent, revision, "image-video", {},
    ) is None
    assert resolve_material_profile(
        {"recipeId": "image-video:master", "engine": "sourced_slideshow"},
        {"masterPages": intent, "demand": {"formatMix": {"image-video": 1}}},
    ) is None


def test_exact_pen_pal_page_keeps_image_video(monkeypatch):
    import services.dossier_ingredients as ingredients

    def unavailable(*_args, **_kwargs):
        raise ingredients.SyzygyError("test unavailable")

    monkeypatch.setattr(ingredients, "load_syzygy_library", unavailable)
    page_id = "acct:pen-pal3"
    intent, _ = master_pages(page_id, handle="pen.pal3", content_niche="slideshow", content_engine="sourced_slideshow")
    intent["notionPageId"] = "3ed1465b-b829-809f-afb4-da66093e7f9d"
    revision = intent_hash(intent)
    catalog = ingredients.build_dossier_ingredient_catalog(page_id, intent, revision)
    assert "image-video" in catalog["currentFormatCandidates"]
    assert resolve_material_profile(
        {"recipeId": "image-video:master", "engine": "sourced_slideshow"},
        {"masterPages": intent, "demand": {"formatMix": {"image-video": 1}}},
    ) is not None


def test_niche_alias_is_exactly_xmas_on_the_sourced_slideshow_engine():
    assert page_niche_admits("slideshow", "XMAS", "sourced_slideshow")
    assert page_niche_admits("XMAS", "XMAS", "ai_video")
    assert not page_niche_admits("slideshow", "XMAS", "ai_video")
    assert not page_niche_admits("slideshow", "xmas", "sourced_slideshow")
    assert not page_niche_admits("meme", "XMAS", "sourced_slideshow")
    assert not page_niche_admits("slideshow", "TRUCK", "sourced_slideshow")
    assert not page_niche_admits(None, "XMAS", "sourced_slideshow")
