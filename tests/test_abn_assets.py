"""Tests for the ABN asset-path gateway (services/abn_assets.py).

The gateway is the single enforcement point for the episode folder schema. These
tests pin the contract: validated paths in, off-schema requests raise, the URL form
matches what the OpenShot bridge resolves, and legacy flat names classify correctly.
"""
import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    """Re-import the gateway pointed at a throwaway ASSETS_DIR so tests never touch T9."""
    monkeypatch.setenv("ABN_ASSETS_DIR", str(tmp_path))
    import services.agenticnews as an
    importlib.reload(an)
    import services.abn_assets as A
    importlib.reload(A)
    return A


def test_episode_asset_path_layout(gw):
    # schema groups by LAYER SOURCE: a card is a css-layer asset
    p = gw.asset_path("ep_648e806a", "card", "s0")
    assert p.parent.name == "css"
    assert p.parent.parent.name == "ep_648e806a"      # episode dir directly under ASSETS_DIR
    assert p.parent.parent.parent == gw.ASSETS_DIR
    assert p.name == "s0_card.png"
    assert p.parent.is_dir()  # gateway creates the dir


def test_singleton_episode_root_files(gw):
    assert gw.asset_path("ep_648e806a", "timeline").name == "timeline.json"
    assert gw.asset_path("ep_648e806a", "timeline").parent.name == "ep_648e806a"
    # episode.mp4 is a renders/ singleton
    ep = gw.asset_path("ep_648e806a", "episode")
    assert ep.name == "episode.mp4" and ep.parent.name == "renders"


def test_url_form_matches_bridge_contract(gw):
    url = gw.asset_url("ep_648e806a", "kinetic", "s1")
    assert url == "/agenticnews-assets/ep_648e806a/css/s1_kinetic.mp4"


def test_kind_routing(gw):
    assert gw.asset_path("ep_a111111", "voice", "s3").parent.name == "audio"
    assert gw.asset_path("ep_a111111", "ui", "s3").parent.name == "footage"
    assert gw.asset_path("ep_a111111", "remotion", "s3").parent.name == "remotion"
    assert gw.asset_path("ep_a111111", "broll", "s3").parent.name == "broll"
    assert gw.asset_path("ep_a111111", "thumb").parent.name == "renders"


def test_bad_episode_id_raises(gw):
    with pytest.raises(gw.AssetPathError):
        gw.asset_path("not-an-episode", "card", "s0")


def test_unknown_kind_raises(gw):
    with pytest.raises(gw.AssetPathError):
        gw.asset_path("ep_648e806a", "frobnicate", "s0")


def test_path_traversal_slug_raises(gw):
    with pytest.raises(gw.AssetPathError):
        gw.asset_path("ep_648e806a", "card", "../../etc/passwd")


def test_shared_categories(gw):
    p = gw.shared_path("audio", "bed_v2.mp3")
    assert p.parent.name == "audio" and p.parent.parent.name == "_shared"
    with pytest.raises(gw.AssetPathError):
        gw.shared_path("nonsense", "x.mp3")


def test_published_path(gw):
    p = gw.published_path("ep_648e806a", "episode.mp4")
    assert p.parent.name == "ep_648e806a" and p.parent.parent.name == "_published"


@pytest.mark.parametrize("name,ep,subdir,kind", [
    ("ep_3c114c5c_s2_card.png", "ep_3c114c5c", "css", "card"),
    ("ep_7a15b8c3_s0_v2sc0_hook.png", "ep_7a15b8c3", "css", "hook"),
    ("ep_30a5a009_s10_kinetic.mp4", "ep_30a5a009", "css", "kinetic"),
    ("ep_7a15b8c3_s3_ui.mp4", "ep_7a15b8c3", "footage", "ui"),
    ("ep_7a15b8c3_thumb_bg.png", "ep_7a15b8c3", "renders", "thumb_bg"),
    ("ep_7a15b8c3_timeline.json", "ep_7a15b8c3", "", "timeline"),
    ("ep_7a15b8c3_episode.mp4", "ep_7a15b8c3", "renders", "episode"),
])
def test_classify_legacy_names(gw, name, ep, subdir, kind):
    c = gw.classify(name)
    assert c is not None
    assert c["ep_id"] == ep
    assert c["subdir"] == subdir
    assert c["kind"] == kind


def test_classify_returns_none_for_shared(gw):
    assert gw.classify("abn_logo.png") is None
    assert gw.classify("bed.mp3") is None


def test_is_managed(gw):
    inside = gw.asset_path("ep_648e806a", "card", "s0")
    assert gw.is_managed(inside)
    assert not gw.is_managed(gw.ASSETS_DIR / "ep_648e806a_s0_card.png")  # flat legacy = unmanaged


# --- flat-slug bridge (the abn_factory refactor entrypoint) ---------------------

@pytest.mark.parametrize("flat,ep,rest", [
    ("ep_648e806a_s0", "ep_648e806a", "s0"),
    ("ep_648e806a_s10", "ep_648e806a", "s10"),
    ("rec_ep_6a09fa6f_s3", "rec_ep_6a09fa6f", "s3"),
    ("ep_648e806a", "ep_648e806a", ""),          # episode-level: bare ep_id
    ("ep5", "ep5", ""),                           # legacy short form
])
def test_split_slug(gw, flat, ep, rest):
    assert gw.split_slug(flat) == (ep, rest)


def test_split_slug_rejects_naked_name(gw):
    with pytest.raises(gw.AssetPathError):
        gw.split_slug("just_a_name")             # no ep_id prefix


def test_asset_path_from_slug_segment(gw):
    # the factory's `sid = f"{ep_id}_s{i}"` -> a card lands in css/ as s0_card.png
    p = gw.asset_path_from_slug("ep_648e806a_s0", "card")
    assert p.parent.name == "css"
    assert p.parent.parent.name == "ep_648e806a"
    assert p.name == "s0_card.png"


def test_asset_path_from_slug_episode_level(gw):
    # an episode-level slug (bare ep_id) yields the kind's default name
    p = gw.asset_path_from_slug("ep_648e806a", "thumb")
    assert p.parent.name == "renders"
    assert p.name == "thumb.png"


def test_asset_url_from_slug(gw):
    url = gw.asset_url_from_slug("ep_648e806a_s1", "kinetic")
    assert url == "/agenticnews-assets/ep_648e806a/css/s1_kinetic.mp4"
