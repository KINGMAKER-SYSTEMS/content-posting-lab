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

# Captured at import, BEFORE conftest's autouse `no_live_codex_image` fixture stubs
# abn_factory._codex_image to a None-returning no-op. The codex SOURCE-gating tests
# below must exercise the REAL function, so they restore this original first.
_REAL_CODEX_IMAGE = abn_factory._codex_image


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


# ---------------------------------------------------------------------------
# _cross_scratch_path: the ONE chokepoint for cross-episode throwaway writes.
# It must (a) land valid names in a GC-reapable _scratch/ root the gateway
# recognises, and (b) RAISE on any off-schema name BEFORE bytes are written, so
# the GC is never handed an unreapable stray. No test covered either branch.
# ---------------------------------------------------------------------------

def test_cross_scratch_path_lands_in_reapable_managed_scratch(store):
    """A valid cross-scratch name resolves under _scratch/, with its parent created,
    and the gateway recognises that parent as BOTH managed (is_managed) and a
    GC-reapable root (scratch_dirs) — the same set the chokepoint asserts against."""
    dest = abn_factory._cross_scratch_path("_tmp_bg_0")
    assert dest == store / "_scratch" / "_tmp_bg_0"
    assert dest.parent.is_dir()                       # parent created
    assert abn_assets.is_managed(dest)               # on-schema
    reapable = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert dest.parent.resolve() in reapable         # GC can reap it
    # And once a real file is written there, the GC enumerates it as reapable.
    dest.write_bytes(b"z" * 64)
    assert dest.resolve() in {f.resolve() for f in abn_assets.reapable_scratch()}


def test_cross_scratch_path_allows_leading_underscore(store):
    """Real cross-scratch names carry a leading underscore (_tmp_bg_0); the
    per-episode slug rule forbids it, but this chokepoint must allow it."""
    dest = abn_factory._cross_scratch_path("_codex_keep.png")
    assert dest.name == "_codex_keep.png"
    assert dest.parent == store / "_scratch"


@pytest.mark.parametrize("bad", [
    "../escape.png",          # parent traversal
    "sub/dir.png",            # forward slash
    "sub\\dir.png",           # backslash
    ".hidden",                # leading dot
    "",                       # empty
    "   ",                    # whitespace-only (strips to empty)
    "/abs.png",               # absolute-ish
    "a b.png",                # space (not in the closed charset)
])
def test_cross_scratch_path_rejects_off_schema_names(store, bad):
    """An off-schema basename RAISES before any bytes/dir are written — the GC can
    never be handed an unreapable stray. _scratch/ is created lazily, so verify the
    bad name produced no file at the (would-be) location either."""
    with pytest.raises(ValueError):
        abn_factory._cross_scratch_path(bad)
    # No stray landed: nothing matching the rejected name exists under the store.
    leaked = [p for p in store.rglob("*") if p.is_file()]
    assert leaked == []


def test_cross_scratch_path_rejects_when_resolved_parent_not_reapable(store, monkeypatch):
    """The reapable-root check is the real guard, not just the regex: if scratch_dirs()
    ever stopped reporting _scratch/ as reapable (a schema regression), the chokepoint
    must REFUSE the write rather than hand the GC an unreapable target."""
    # Force the reapable set to exclude _scratch/ — simulating the regression the
    # `dest.parent not in scratch_dirs()` assert exists to catch.
    monkeypatch.setattr(abn_assets, "scratch_dirs", lambda: [])
    monkeypatch.setattr(abn_factory, "scratch_dirs", lambda: [])
    with pytest.raises(ValueError, match="non-reapable"):
        abn_factory._cross_scratch_path("_tmp_bg_0")


# ---------------------------------------------------------------------------
# _codex_image SOURCE gating: the glob walks ~/.codex/generated_images, an
# UNTRUSTED root. The picked "new" file must be a regular file whose real
# (symlink-resolved) location stays inside gen_dir, or a planted symlink could
# pull an arbitrary file into the asset store, bypassing the gateway. The dest
# is already guarded by _cross_scratch_path; these pin the SOURCE guard.
# ---------------------------------------------------------------------------

def test_codex_image_copies_legit_in_tree_file(store, tmp_path, monkeypatch):
    """A normal codex-produced regular file inside gen_dir is accepted and copied
    into the reapable _scratch/ root, returning its managed URL."""
    import glob as _glob
    gen_dir = store.parent / ".codex" / "generated_images"
    sub = gen_dir / "abc"
    sub.mkdir(parents=True)
    made = sub / "ig_legit.png"
    made.write_bytes(b"img" * 64)

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    seen = {"n": 0}
    def fake_glob(_pat):
        seen["n"] += 1
        return [] if seen["n"] == 1 else [str(made)]
    monkeypatch.setattr(_glob, "glob", fake_glob)
    monkeypatch.setenv("CODEX_HOME", str(gen_dir.parent))
    monkeypatch.setattr(abn_factory, "_codex_image", _REAL_CODEX_IMAGE)

    url = abn_factory._codex_image("a cinematic background", "_tmp_bg_9")
    assert url is not None
    dest = abn_factory._resolve_asset(url)
    assert dest.parent.resolve() == (store / "_scratch").resolve()
    assert dest.read_bytes() == made.read_bytes()


def test_codex_image_rejects_symlinked_source(store, tmp_path, monkeypatch):
    """A SYMLINK planted in ~/.codex/generated_images pointing at an arbitrary
    out-of-tree file must be rejected — nothing gets copied into the store."""
    import glob as _glob
    gen_dir = store.parent / ".codex" / "generated_images"
    sub = gen_dir / "abc"
    sub.mkdir(parents=True)
    # The attacker's target lives entirely outside gen_dir.
    secret = tmp_path / "outside" / "secret.png"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"SECRET-DATA")
    evil = sub / "ig_evil.png"
    evil.symlink_to(secret)

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    seen = {"n": 0}
    def fake_glob(_pat):
        seen["n"] += 1
        return [] if seen["n"] == 1 else [str(evil)]
    monkeypatch.setattr(_glob, "glob", fake_glob)
    monkeypatch.setenv("CODEX_HOME", str(gen_dir.parent))
    monkeypatch.setattr(abn_factory, "_codex_image", _REAL_CODEX_IMAGE)

    url = abn_factory._codex_image("anything", "_tmp_bg_8")
    assert url is None
    # No bytes from the secret file leaked into the store.
    leaked = [p for p in store.rglob("*") if p.is_file() and p.read_bytes() == b"SECRET-DATA"]
    assert leaked == []


def test_codex_image_rejects_source_escaping_via_symlinked_gen_subdir(store, tmp_path, monkeypatch):
    """Even a file whose path string is *under* gen_dir is rejected if a symlink
    hop in its real path escapes the tree (realpath containment, not string prefix)."""
    import glob as _glob
    gen_dir = store.parent / ".codex" / "generated_images"
    gen_dir.mkdir(parents=True)
    # gen_dir/linkdir -> outside; the "produced" file string is gen_dir/linkdir/ig_x.png
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ig_x.png").write_bytes(b"ESCAPED")
    (gen_dir / "linkdir").symlink_to(outside)
    fake = gen_dir / "linkdir" / "ig_x.png"  # exists via the symlink, realpath is outside

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    seen = {"n": 0}
    def fake_glob(_pat):
        seen["n"] += 1
        return [] if seen["n"] == 1 else [str(fake)]
    monkeypatch.setattr(_glob, "glob", fake_glob)
    monkeypatch.setenv("CODEX_HOME", str(gen_dir.parent))
    monkeypatch.setattr(abn_factory, "_codex_image", _REAL_CODEX_IMAGE)

    url = abn_factory._codex_image("anything", "_tmp_bg_7")
    assert url is None
    leaked = [p for p in store.rglob("*") if p.is_file() and p.read_bytes() == b"ESCAPED"]
    assert leaked == []
