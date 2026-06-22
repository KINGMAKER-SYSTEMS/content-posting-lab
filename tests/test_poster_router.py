"""Tests for poster_router.resolve_poster_for_page.

This is King-Maker critical path: a page's Notion `poster_name` must resolve to
a registered TelegramPoster, or content won't assign/forward. The pipeline tests
all monkeypatch this function away, so its own matching branches (exact
normalized match, fuzzy first-name match, the empty/no-match None paths) are
otherwise unexercised. These tests pin that behavior.
"""

import services.poster_router as pr


def _patch_posters(monkeypatch, posters):
    monkeypatch.setattr(pr, "list_posters", lambda: posters)


POSTERS = [
    {"poster_id": "po_seffra", "name": "Seffra"},
    {"poster_id": "po_jake", "name": "Jake Balik"},
    {"poster_id": "po_john", "name": "John Smathers"},
]


def test_exact_match_case_and_punctuation_insensitive(monkeypatch):
    _patch_posters(monkeypatch, POSTERS)
    # exact, but different case + punctuation/spacing
    resolved = pr.resolve_poster_for_page({"poster_name": "  jake  balik "})
    assert resolved is not None
    assert resolved["poster_id"] == "po_jake"


def test_fuzzy_first_name_initial(monkeypatch):
    _patch_posters(monkeypatch, POSTERS)
    # "Jake B." should fuzzy-match "Jake Balik" via first-name prefix
    resolved = pr.resolve_poster_for_page({"poster_name": "Jake B."})
    assert resolved is not None
    assert resolved["poster_id"] == "po_jake"


def test_no_match_returns_none(monkeypatch):
    _patch_posters(monkeypatch, POSTERS)
    assert pr.resolve_poster_for_page({"poster_name": "Nonexistent Person"}) is None


def test_empty_poster_name_returns_none(monkeypatch):
    _patch_posters(monkeypatch, POSTERS)
    assert pr.resolve_poster_for_page({"poster_name": ""}) is None
    assert pr.resolve_poster_for_page({"poster_name": "   "}) is None
    assert pr.resolve_poster_for_page({}) is None
    assert pr.resolve_poster_for_page(None) is None


def test_exact_match_preferred_over_fuzzy(monkeypatch):
    # If two posters share a first name, an exact full-name match wins over the
    # fuzzy first-name prefix (exact loop runs first).
    posters = [
        {"poster_id": "po_jakeb", "name": "Jake Balik"},
        {"poster_id": "po_jaked", "name": "Jake Donnelly"},
    ]
    _patch_posters(monkeypatch, posters)
    resolved = pr.resolve_poster_for_page({"poster_name": "Jake Donnelly"})
    assert resolved is not None
    assert resolved["poster_id"] == "po_jaked"
