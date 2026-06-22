"""
Regression guard for the asset-gateway audit (hard-gate 35: "Asset writes go through the
services/abn_assets.py gateway").

The cached b-roll LIBRARY (abn_factory._grow_bg_library) used to hand-build its write path as
``ASSETS / "broll_library" / broll_NN.mp4`` and write straight to it — bypassing the
abn_assets gateway, landing the clips at the STORE ROOT instead of the schema home
(``_shared/broll_library/`` per abn_assets docstring line 30), and evading the gateway's
name validation + GC-protection scan. These tests pin the library to the gateway:

  * the library dir resolves THROUGH ``shared_path`` into ``_shared/broll_library/``;
  * a clip written there is on-schema (``is_managed``) and inside ``_shared``;
  * the served URL round-trips back to the exact on-disk gateway path (read==write);
  * the factory no longer constructs ``ASSETS/"broll_library"`` directly (source grep).
"""
import re
from pathlib import Path

import pytest

from services import abn_factory
from services import abn_assets


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point BOTH the factory ASSETS and the gateway ASSETS_DIR at a throwaway store."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return assets


def test_bg_lib_dir_is_gateway_shared_broll_library(store):
    """The library dir is resolved through the gateway into _shared/broll_library/, not the
    store root — so it tracks ASSETS_DIR and lives where the schema says it should."""
    d = abn_factory._bg_lib_dir()
    assert d == store / "_shared" / "broll_library"
    # NOT the old off-schema root location
    assert d != store / "broll_library"


def test_bg_lib_dir_matches_gateway_shared_path(store):
    """_bg_lib_dir() is literally the parent of shared_path('broll_library', ...): the same
    chokepoint the writes go through, so read and write can never drift apart."""
    assert abn_factory._bg_lib_dir() == abn_assets.shared_path("broll_library", "broll_00.mp4").parent


def test_library_clip_is_managed_and_under_shared(store):
    """A clip persisted into the library via the gateway is on-schema (is_managed) and inside
    _shared — i.e. it is NOT a stray root write the GC-protection scan would miss."""
    dest = abn_assets.shared_path("broll_library", "broll_03.mp4")
    dest.write_bytes(b"x" * 8192)
    assert abn_assets.is_managed(dest)
    rel = dest.resolve().relative_to(store.resolve())
    assert rel.parts[0] == "_shared" and rel.parts[1] == "broll_library"


def test_animated_bg_url_roundtrips_to_gateway_path(store):
    """The URL _animated_bg serves for a cached clip resolves back (via _resolve_asset) to the
    exact on-disk gateway path — proving the served path is the gateway path, read==write."""
    d = abn_factory._bg_lib_dir()
    d.mkdir(parents=True, exist_ok=True)
    clip = d / "broll_00.mp4"
    clip.write_bytes(b"y" * 8192)
    # _bg_library finds it, _asset_url turns it into the served URL
    lib = abn_factory._bg_library()
    assert clip in lib
    url = abn_factory._asset_url(clip)
    assert url.startswith(abn_assets.URL_PREFIX)
    # the served URL must resolve back to the same on-disk file (no root-prefix drift)
    assert abn_factory._resolve_asset(url).resolve() == clip.resolve()
    # and it points under _shared/broll_library, not the store root
    assert "/_shared/broll_library/" in url


def test_factory_does_not_hand_build_broll_library_path():
    """Source guard: the factory must not reconstruct ASSETS/'broll_library' (the off-schema
    write path the gateway exists to replace). All library access goes via shared_path /
    _bg_lib_dir. Catches a future regression reintroducing the bypass."""
    pat = re.compile(r"ASSETS\s*/\s*['\"]broll_library['\"]")
    offenders = []
    for ln in Path(abn_factory.__file__).read_text().splitlines():
        code = ln.split("#", 1)[0]  # ignore the part of the line after a comment marker
        if pat.search(code):
            offenders.append(ln.strip())
    assert not offenders, (
        "abn_factory hand-builds an ASSETS/'broll_library' path — route it through "
        f"abn_assets.shared_path('broll_library', name) instead (gateway hard-gate 35): {offenders}"
    )
