"""
Tests for the deterministic stdlib fuzzy fallback in the sound-sync pipeline.

Covers the 5 previously-unmatched campaigns documented in CLAUDE.md (Liam St
John, In Color, Alex Nicol, Gregory Alan Isakov, Matilda Lyn). The failure mode
was: a Hub campaign whose artist/song is spelled slightly differently in Notion
(Nicol/Nichol, Lyn/Lynn, Gregory/Greg) produces zero normalized-key overlap, so
the deterministic pass misses it and it falls through to the costly,
non-deterministic AI matcher (which silently returns {} on any error).

The fuzzy fallback closes near-miss spelling gaps for free, deterministically.
"""

import asyncio

import pytest

import services.campaign_hub as hub
from services.campaign_hub import _fuzzy_match_notion


def _notion(artist, song, url):
    return {"id": f"id-{artist}", "artist": artist, "song": song, "url": url}


# --------------------------------------------------------------------------- #
# Unit: _fuzzy_match_notion
# --------------------------------------------------------------------------- #


def test_fuzzy_matches_spelling_variants():
    notion = [
        _notion("Alex Nichol", "Falling", "https://tt/alex"),
        _notion("Matilda Lynn", "Home", "https://tt/matilda"),
        _notion("Greg Alan Isakov", "Big Black Car", "https://tt/greg"),
    ]
    assert _fuzzy_match_notion("Alex Nicol", "Falling", notion)["url"] == "https://tt/alex"
    assert _fuzzy_match_notion("Matilda Lyn", "Home", notion)["url"] == "https://tt/matilda"
    assert (
        _fuzzy_match_notion("Gregory Alan Isakov", "Big Black Car", notion)["url"]
        == "https://tt/greg"
    )


def test_fuzzy_matches_punctuation_variants():
    notion = [_notion("Liam St. John", "Drift", "https://tt/liam")]
    assert _fuzzy_match_notion("Liam St John", "Drift", notion)["url"] == "https://tt/liam"


def test_fuzzy_rejects_same_artist_different_song():
    notion = [_notion("Alex Nichol", "Rising", "https://tt/wrong")]
    # Same artist, genuinely different song — must NOT cross-match.
    assert _fuzzy_match_notion("Alex Nicol", "Falling", notion) is None


def test_fuzzy_rejects_unrelated_names():
    notion = [_notion("Taylor Swift", "Anti-Hero", "https://tt/ts")]
    assert _fuzzy_match_notion("Alex Nicol", "Falling", notion) is None


def test_fuzzy_skips_entries_without_url():
    notion = [_notion("Alex Nichol", "Falling", "")]
    assert _fuzzy_match_notion("Alex Nicol", "Falling", notion) is None


def test_fuzzy_handles_empty_input():
    assert _fuzzy_match_notion("", "", [_notion("Alex Nichol", "Falling", "u")]) is None


# --------------------------------------------------------------------------- #
# Integration: sync_sound_status wires the fuzzy fallback before the AI pass
# --------------------------------------------------------------------------- #


def test_sync_uses_fuzzy_before_ai(monkeypatch):
    """A spelling-variant campaign should match via fuzzy, never reaching the AI."""
    hub_campaigns = [
        {"slug": "alex-nicol-falling", "artist": "Alex Nicol", "song": "Falling",
         "completion_status": "active"},
    ]
    notion_campaigns = [
        {"notion_page_id": "n1", "artist": "Alex Nichol", "song": "Falling",
         "tiktok_url": "https://tt/alex"},
    ]

    added = []

    async def fake_fetch_all():
        return hub_campaigns

    def fake_list_sounds(active_only=False):
        return []

    def fake_add_sound(url, label):
        added.append((url, label))

    ai_called = {"hit": False}

    async def fake_ai(*a, **k):
        ai_called["hit"] = True
        return {}

    monkeypatch.setattr(hub, "fetch_all_campaigns", fake_fetch_all)
    monkeypatch.setattr(hub, "list_sounds", fake_list_sounds)
    monkeypatch.setattr(hub, "add_sound", fake_add_sound)
    monkeypatch.setattr(hub, "_ai_match_campaigns", fake_ai)

    result = asyncio.run(hub.sync_sound_status(notion_campaigns=notion_campaigns))

    assert result["matched_fuzzy"] == 1
    assert result["matched_deterministic"] == 0
    assert result["unmatched"] == []
    assert ai_called["hit"] is False  # fuzzy resolved it; no AI call needed
    assert added == [("https://tt/alex", "Alex Nicol - Falling")]


def test_sync_genuinely_missing_link_stays_unmatched(monkeypatch):
    """If Notion truly has no entry, the campaign stays unmatched (not force-matched)."""
    hub_campaigns = [
        {"slug": "ghost-band-x", "artist": "Ghost Band", "song": "Nowhere",
         "completion_status": "active"},
    ]
    notion_campaigns = [
        {"notion_page_id": "n1", "artist": "Totally Different", "song": "Other",
         "tiktok_url": "https://tt/other"},
    ]

    async def fake_fetch_all():
        return hub_campaigns

    monkeypatch.setattr(hub, "fetch_all_campaigns", fake_fetch_all)
    monkeypatch.setattr(hub, "list_sounds", lambda active_only=False: [])
    monkeypatch.setattr(hub, "add_sound", lambda url, label: None)

    async def fake_ai(*a, **k):
        return {}

    monkeypatch.setattr(hub, "_ai_match_campaigns", fake_ai)

    result = asyncio.run(hub.sync_sound_status(notion_campaigns=notion_campaigns))

    assert result["matched_fuzzy"] == 0
    assert result["unmatched"] == ["Ghost Band - Nowhere"]
