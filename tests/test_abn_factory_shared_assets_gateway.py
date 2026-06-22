"""
Regression guard for the asset-gateway audit (hard-gate 35: "Asset writes go through the
services/abn_assets.py gateway").

The shared pool — the music bed, the whoosh SFX, and the ABN logo lockups — used to be
read FLAT at the store root: ``ASSETS / "bed.mp3"``, ``ASSETS / "whoosh.mp3"``,
``ASSETS / "abn_logo.png"`` (abn_factory lines 2515-2519, 2733, 3257). A flat name at the
store root is schema-escape — it bypasses the gateway's namespace isolation and is exactly
the kind of bare root file the glob-GC has historically eaten. These assets belong under
``_shared/`` per the abn_assets schema: beds + sfx are the ``audio`` category, logo lockups
are the ``brand`` category. These tests pin the factory to the gateway:

  * a bed/sfx/logo persisted via ``shared_path`` is on-schema (``is_managed``) and under
    ``_shared``;
  * the served URL (``shared_url``) round-trips back (via ``_resolve_asset``) to the exact
    on-disk gateway path — read == write, no store-root drift;
  * the factory no longer reads ``ASSETS / "bed.mp3"`` / ``whoosh.mp3`` / ``abn_logo*.png``
    directly (source grep) and no longer emits the flat ``/agenticnews-assets/bed.mp3`` URL.
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


@pytest.mark.parametrize("category,name", [
    ("audio", "bed.mp3"),
    ("audio", "whoosh.mp3"),
    ("brand", "abn_logo.png"),
    ("brand", "abn_logo_transparent.png"),
])
def test_shared_asset_is_managed_and_under_shared(store, category, name):
    """A shared pool asset persisted via the gateway is on-schema (is_managed) and inside
    _shared/<category> — NOT a stray store-root write the GC-protection scan would miss."""
    dest = abn_assets.shared_path(category, name)
    dest.write_bytes(b"x" * 256)
    assert abn_assets.is_managed(dest)
    rel = dest.resolve().relative_to(store.resolve())
    assert rel.parts[0] == "_shared" and rel.parts[1] == category
    # NOT the old off-schema root location
    assert dest != store / name


@pytest.mark.parametrize("category,name", [
    ("audio", "bed.mp3"),
    ("audio", "whoosh.mp3"),
    ("brand", "abn_logo.png"),
])
def test_shared_url_roundtrips_to_gateway_path(store, category, name):
    """The URL the factory emits for a shared asset (shared_url) resolves back via
    _resolve_asset to the exact on-disk gateway path — read == write, no root drift."""
    disk = abn_assets.shared_path(category, name)
    disk.write_bytes(b"y" * 256)
    url = abn_assets.shared_url(category, name)
    assert url.startswith(abn_assets.URL_PREFIX)
    assert "/_shared/" in url
    assert abn_factory._resolve_asset(url).resolve() == disk.resolve()


def test_factory_does_not_flat_read_shared_pool():
    """Source guard: the factory must not read the shared pool at the store root
    (ASSETS / 'bed.mp3' | 'whoosh.mp3' | 'abn_logo*.png') — all access goes via
    shared_path / shared_url. Catches a future regression reintroducing the bypass."""
    pat = re.compile(r"ASSETS\s*/\s*['\"](?:bed\.mp3|whoosh\.mp3|abn_logo[\w]*\.png)['\"]")
    offenders = []
    for ln in Path(abn_factory.__file__).read_text().splitlines():
        code = ln.split("#", 1)[0]  # ignore the part of the line after a comment marker
        if pat.search(code):
            offenders.append(ln.strip())
    assert not offenders, (
        "abn_factory reads a shared-pool asset flat at the store root — route it through "
        f"abn_assets.shared_path('audio'|'brand', name) instead (gateway hard-gate 35): {offenders}"
    )


def test_factory_does_not_emit_flat_shared_url():
    """Source guard: the factory must not emit the flat /agenticnews-assets/bed.mp3 |
    whoosh.mp3 | abn_logo*.png URLs (store-root, schema-escape). Shared URLs come from
    shared_url(), which lands them under /_shared/."""
    pat = re.compile(r"/agenticnews-assets/(?:bed\.mp3|whoosh\.mp3|abn_logo[\w]*\.png)")
    offenders = []
    for ln in Path(abn_factory.__file__).read_text().splitlines():
        code = ln.split("#", 1)[0]
        if pat.search(code):
            offenders.append(ln.strip())
    assert not offenders, (
        "abn_factory emits a flat shared-pool URL — use abn_assets.shared_url('audio'|'brand', "
        f"name) (lands under /_shared/) instead: {offenders}"
    )
