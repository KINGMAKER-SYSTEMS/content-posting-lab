import asyncio
import importlib
import os
import shutil
import time
from collections import namedtuple

import pytest

from services import abn_factory
from services import abn_assets


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point BOTH the factory ASSETS and the gateway ASSETS_DIR at a throwaway store so the
    schema-aware GC (reapable_scratch / tombstone / _old_episode_renders) operates on it, and
    create the editor_timelines dir the protection scan reads."""
    assets = tmp_path / "assets"
    (assets / "editor_timelines").mkdir(parents=True)
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return assets


def _scratch(assets, ep_id, name, *, age_s=7200, body=b"intermediate"):
    """Write a per-episode scratch file aged `age_s` seconds in the past."""
    d = assets / ep_id / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(body)
    old = time.time() - age_s
    os.utime(f, (old, old))
    return f


# --- the new GC contract: reap ONLY scratch/, tombstone (→ _trash/) instead of unlink ----------


def test_purge_disk_tombstones_old_scratch_instead_of_unlinking(store):
    """Old scratch intermediates are MOVED to _trash/ (recoverable safe-delete), not unlinked.
    The reaped bytes are still counted as freed."""
    f = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * 100)

    freed = abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert not f.exists(), "scratch intermediate should have been reaped"
    tombstoned = store / "_trash" / "ep_a111111" / "scratch" / "s0_raw.wav"
    assert tombstoned.exists(), "reap must tombstone to _trash/, not destroy"
    assert tombstoned.read_bytes() == b"x" * 100
    assert freed >= 0


def test_purge_disk_reaps_cross_episode_scratch(store):
    """Files under the cross-episode _scratch/ root are also reapable (spec line 32)."""
    d = store / "_scratch"
    d.mkdir()
    f = d / "fluxtest_probe.png"
    f.write_bytes(b"probe")
    old = time.time() - 7200
    os.utime(f, (old, old))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert not f.exists()
    assert (store / "_trash" / "_scratch" / "fluxtest_probe.png").exists()


def test_purge_disk_never_touches_schema_layers_dirs_or_symlinks(store, monkeypatch):
    """The GC walks only scratch/ + _scratch/. A real render, an audio VO, a footage capture, a
    whole episode dir, and a back-compat symlink at an old flat path must ALL survive untouched —
    there is no flat-glob blast radius anymore. Only the aged scratch file is reaped."""
    epdir = store / "ep_a111111"
    (epdir / "renders").mkdir(parents=True)
    real_render = epdir / "renders" / "episode.mp4"
    real_render.write_bytes(b"the real render")
    (epdir / "audio").mkdir()
    real_vo = epdir / "audio" / "s0_voice.wav"        # origaudio-loss class file — must survive
    real_vo.write_bytes(b"orig vo")
    (epdir / "footage").mkdir()
    real_ui = epdir / "footage" / "s3_ui.mp4"
    real_ui.write_bytes(b"capture")
    # a back-compat SYMLINK at an old flat path (what the migration leaves)
    flat_link = store / "ep_a111111_episode.mp4"
    flat_link.symlink_to(real_render)
    # the only reapable thing
    scratch_f = _scratch(store, "ep_a111111", "s0_raw.wav")

    old = time.time() - 7200
    for p in (real_render, real_vo, real_ui):
        os.utime(p, (old, old))
    os.utime(flat_link, (old, old), follow_symlinks=False)

    usage = namedtuple("usage", ("total", "used", "free"))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(100, 0, 100 * 1e9))  # plenty → no render trim
    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=0, low_disk_gb=0)

    assert real_render.exists(), "GC touched a render (it must only walk scratch/)"
    assert real_vo.exists(), "GC ate a VO audio file (origaudio-loss class bug)"
    assert real_ui.exists(), "GC touched a footage capture"
    assert epdir.is_dir(), "GC removed a schema episode dir"
    assert flat_link.is_symlink(), "GC deleted a back-compat symlink (orphans the real render)"
    assert not scratch_f.exists(), "GC should have reaped the aged scratch intermediate"


def test_purge_disk_preserves_editor_timeline_referenced_scratch(store):
    """A scratch file still referenced by an Editor Bay timeline must NOT be reaped, even though
    it lives under the reapable scratch/ surface and is old enough."""
    protected = _scratch(store, "ep_aaa000", "s0_raw.wav")
    unreferenced = _scratch(store, "ep_aaa000", "s1_raw.wav")
    rel = protected.relative_to(store)
    (store / "editor_timelines" / "ep_aaa000.json").write_text(
        f'{{"assets": {{"voice": {{"src": "/agenticnews-assets/{rel}"}}}}}}'
    )

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert protected.exists(), "GC reaped a timeline-referenced scratch file"
    assert not unreferenced.exists(), "GC should reap the unreferenced scratch file"


def test_purge_disk_trims_old_episode_renders_under_low_disk(store):
    """Under low disk the GC trims the OLDEST real episode renders (keep N), and never touches a
    render still referenced by an Editor Bay timeline."""
    def render(ep_id, age_s):
        d = store / ep_id / "renders"
        d.mkdir(parents=True)
        f = d / "episode.mp4"
        f.write_bytes(b"render")
        t = time.time() - age_s
        os.utime(f, (t, t))
        return f

    keep_referenced = render("ep_keep0", age_s=9000)   # oldest, but timeline-protected
    drop_old = render("ep_drop0", age_s=8000)
    fresh = render("ep_fresh0", age_s=10)
    rel = keep_referenced.relative_to(store)
    (store / "editor_timelines" / "ep_keep0.json").write_text(
        f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}"}}}}}}'
    )

    usage = namedtuple("usage", ("total", "used", "free"))
    _orig = shutil.disk_usage
    shutil.disk_usage = lambda _: usage(100, 100, 0)  # critically low → trim
    try:
        abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)
    finally:
        shutil.disk_usage = _orig

    assert keep_referenced.exists(), "GC trimmed a timeline-referenced render under low disk"
    assert not drop_old.exists(), "GC should trim the oldest unreferenced render under low disk"
    assert fresh.exists(), "GC trimmed a fresh render it should have kept"


def test_gc_segments_disk_phase_tombstones_scratch_and_spares_schema(store, monkeypatch):
    """The disk phase of _gc_segments reaps only scratch/ (tombstoning to _trash/) and leaves real
    schema dirs, renders, and back-compat symlinks intact — no flat-glob orphan-file pass anymore."""
    epdir = store / "ep_dead00"
    (epdir / "renders").mkdir(parents=True)
    keeper = epdir / "renders" / "episode.mp4"
    keeper.write_bytes(b"whole episode render")
    flat_link = store / "ep_dead00_episode.mp4"
    flat_link.symlink_to(keeper)
    scratch_f = _scratch(store, "ep_dead00", "s0_demo.mp4")

    old = time.time() - (7 * 3600)
    os.utime(keeper, (old, old))
    os.utime(flat_link, (old, old), follow_symlinks=False)

    async def list_videos(*args, **kwargs):
        return []  # nothing tracked

    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)
    # keep disk "healthy" so the low-disk render trim doesn't fire
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 0, 100 * 1e9))

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert keeper.exists(), "GC destroyed a render inside a schema dir"
    assert epdir.is_dir(), "GC removed a schema episode dir"
    assert flat_link.is_symlink(), "GC deleted a back-compat symlink"
    assert not scratch_f.exists(), "GC should have reaped the aged scratch intermediate"
    assert (store / "_trash" / "ep_dead00" / "scratch" / "s0_demo.mp4").exists(), "scratch reap must tombstone"


# --- gateway-level tombstone contract (the new safe-delete primitive) ---------------------------


def test_tombstone_moves_scratch_to_trash(store):
    f = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"data")
    size = abn_factory.tombstone(f)
    assert size == 4
    assert not f.exists()
    dest = store / "_trash" / "ep_a111111" / "scratch" / "s0_raw.wav"
    assert dest.exists() and dest.read_bytes() == b"data"


def test_tombstone_refuses_non_scratch_paths(store):
    """tombstone() is the physical enforcement point: handed a render, an audio file, a symlink,
    a dir, or an off-store path, it RAISES rather than deleting — so a bad GC call can't cause
    whole-episode loss even if the caller's filtering were buggy."""
    epdir = store / "ep_a111111"
    (epdir / "renders").mkdir(parents=True)
    render = epdir / "renders" / "episode.mp4"
    render.write_bytes(b"render")
    (epdir / "audio").mkdir()
    vo = epdir / "audio" / "s0_voice.wav"
    vo.write_bytes(b"vo")
    link = store / "ep_a111111_episode.mp4"
    link.symlink_to(render)

    for bad in (render, vo, link, epdir, store / "outside.txt"):
        with pytest.raises(abn_assets.AssetPathError):
            abn_factory.tombstone(bad)
    assert render.exists() and vo.exists() and link.is_symlink()


def test_tombstone_collision_does_not_clobber(store):
    """Two reaps of the same relative scratch name must not overwrite each other in _trash/."""
    f1 = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"first")
    abn_factory.tombstone(f1)
    f2 = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"second", age_s=3600)
    abn_factory.tombstone(f2)
    trashed = list((store / "_trash" / "ep_a111111" / "scratch").glob("s0_raw*"))
    assert len(trashed) == 2, "collision should suffix, not clobber"


# --- unchanged helpers that the GC still depends on ---------------------------------------------


def test_resolve_asset_subpath_url_preserves_subdir(monkeypatch, tmp_path):
    """A /agenticnews-assets/<subpath> URL must resolve to the FULL subpath under the store,
    not get flattened to the basename. Flattening was the bug that read a migrated per-episode
    asset back as 'missing' (basename collided with / pointed at the wrong dir)."""
    assets = tmp_path / "assets"
    monkeypatch.setattr(abn_factory, "ASSETS", assets)

    resolved = abn_factory._resolve_asset("/agenticnews-assets/ep_a111111/css/s0_card.png")
    assert resolved == assets / "ep_a111111" / "css" / "s0_card.png"
    assert resolved != assets / "s0_card.png"


def test_resolve_asset_invalid_url_returns_nonexistent_path(monkeypatch, tmp_path):
    """An invalid /agenticnews-assets/ URL (asset never written) resolves to a Path under the
    store that simply does not exist — the caller is expected to .exists()-check, not to get a
    raise. (GC dead-review pruning relies on this: missing mp4 -> resolved path .exists() False.)"""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", assets)

    resolved = abn_factory._resolve_asset("/agenticnews-assets/ep_nope/renders/episode.mp4")
    assert resolved == assets / "ep_nope" / "renders" / "episode.mp4"
    assert not resolved.exists()

    bare = abn_factory._resolve_asset("ep_old_s0.wav")
    assert bare == assets / "ep_old_s0.wav"
    assert not bare.exists()

    assert abn_factory._resolve_asset(None) == assets
    assert abn_factory._resolve_asset("") == assets


def test_asset_url_roundtrips_through_resolve_asset(monkeypatch, tmp_path):
    """path -> _asset_url -> _resolve_asset must land back on the SAME on-disk path, including
    via the gateway's URL builder (asset_url_from_slug). This is the contract the timeline write +
    GC read both depend on; a mismatch silently blanks a segment."""
    assets = tmp_path / "assets"
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)

    p = assets / "ep_a111111" / "css" / "s0_card.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"card")
    assert abn_factory._resolve_asset(abn_factory._asset_url(p)) == p.resolve()

    url = abn_assets.asset_url_from_slug("ep_a111111_s0", "card")
    gw_path = abn_assets.asset_path_from_slug("ep_a111111_s0", "card")
    gw_path.write_bytes(b"gw card")
    resolved = abn_factory._resolve_asset(url)
    assert resolved == gw_path
    assert resolved.exists(), "gateway URL didn't round-trip back to the written file"


def test_is_editor_timeline_protected_asset_standalone(monkeypatch, tmp_path):
    """The protection predicate must hold OUTSIDE the GC loop: a path in the protected set is
    protected (modulo normalization — symlinks/.. resolve to the same real path), one outside
    it is not. It's the gate every GC reap site consults before deleting."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(abn_factory, "ASSETS", assets)

    keep = assets / "ep_a111111_s0.wav"
    other = assets / "ep_b222222_s0.wav"
    keep.write_bytes(b"keep")
    other.write_bytes(b"other")

    protected = {abn_factory._normalize_asset_path(keep)}
    assert abn_factory._is_editor_timeline_protected_asset(keep, protected) is True
    assert abn_factory._is_editor_timeline_protected_asset(other, protected) is False

    noncanonical = assets / "sub" / ".." / "ep_a111111_s0.wav"
    (assets / "sub").mkdir()
    assert abn_factory._is_editor_timeline_protected_asset(noncanonical, protected) is True
