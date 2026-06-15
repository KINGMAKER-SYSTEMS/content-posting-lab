import asyncio
import os
import shutil
import time
from collections import namedtuple

from services import abn_factory
from services import abn_assets


def test_purge_disk_preserves_editor_timeline_referenced_intermediates(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    timelines = assets / "editor_timelines"
    timelines.mkdir(parents=True)
    protected_wav = assets / "ep_real_s0.wav"
    protected_demo = assets / "ep_real_s1_demo.mp4"
    protected_absolute = assets / "ep_real_s2.wav"
    unreferenced_wav = assets / "ep_old_s0.wav"
    protected_wav.write_bytes(b"voice")
    protected_demo.write_bytes(b"demo")
    protected_absolute.write_bytes(b"absolute")
    unreferenced_wav.write_bytes(b"old")
    old = time.time() - 3600
    for file in (protected_wav, protected_demo, protected_absolute, unreferenced_wav):
        os.utime(file, (old, old))
    (timelines / "ep_real.json").write_text(
        f"""
        {{
          "assets": {{
            "voice": {{"src": "/agenticnews-assets/ep_real_s0.wav"}},
            "demo": {{"src": "/agenticnews-assets/ep_real_s1_demo.mp4"}},
            "absolute": {{"src": "{protected_absolute}"}}
          }}
        }}
        """,
    )

    monkeypatch.setattr(abn_factory, "ASSETS", assets)

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert protected_wav.exists()
    assert protected_demo.exists()
    assert protected_absolute.exists()
    assert not unreferenced_wav.exists()


def test_purge_disk_preserves_editor_timeline_referenced_episode_mp4_under_low_disk(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    timelines = assets / "editor_timelines"
    timelines.mkdir(parents=True)
    protected_episode = assets / "ep_deadbeef_episode.mp4"
    unreferenced_episode = assets / "ep_cafebabe_episode.mp4"
    protected_episode.write_bytes(b"keep")
    unreferenced_episode.write_bytes(b"drop")
    old = time.time() - 3600
    for file in (protected_episode, unreferenced_episode):
        os.utime(file, (old, old))
    (timelines / "ep_deadbeef.json").write_text(
        """
        {
          "renderCache": {
            "video": {
              "path": "/agenticnews-assets/ep_deadbeef_episode.mp4"
            }
          }
        }
        """,
    )

    usage = namedtuple("usage", ("total", "used", "free"))
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(100, 100, 0))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=0, low_disk_gb=999)

    assert protected_episode.exists()
    assert not unreferenced_episode.exists()


def test_gc_segments_preserves_editor_timeline_referenced_orphan_assets(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    timelines = assets / "editor_timelines"
    timelines.mkdir(parents=True)
    protected_wav = assets / "ep_deadbeef_s0.wav"
    protected_demo = assets / "ep_deadbeef_demo.mp4"
    protected_timeline = assets / "ep_deadbeef_timeline.json"
    protected_episode = assets / "ep_deadbeef_episode.mp4"
    unreferenced_wav = assets / "ep_cafebabe_s0.wav"
    unreferenced_episode = assets / "ep_cafebabe_episode.mp4"
    for file in (
        protected_wav,
        protected_demo,
        protected_timeline,
        protected_episode,
        unreferenced_wav,
        unreferenced_episode,
    ):
        file.write_bytes(b"asset")
    old = time.time() - (7 * 3600)
    for file in (
        protected_wav,
        protected_demo,
        protected_timeline,
        protected_episode,
        unreferenced_wav,
        unreferenced_episode,
    ):
        os.utime(file, (old, old))
    (timelines / "ep_deadbeef.json").write_text(
        f"""
        {{
          "assets": {{
            "voice": {{"src": "/agenticnews-assets/ep_deadbeef_s0.wav"}},
            "demo": {{"src": "/agenticnews-assets/ep_deadbeef_demo.mp4"}},
            "absolute": {{"src": "{protected_timeline}"}}
          }},
          "renderCache": {{
            "video": {{
              "path": "/agenticnews-assets/ep_deadbeef_episode.mp4"
            }}
          }}
        }}
        """,
    )

    async def list_videos(*args, **kwargs):
        return []

    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert protected_wav.exists()
    assert protected_demo.exists()
    assert protected_timeline.exists()
    assert protected_episode.exists()
    assert not unreferenced_wav.exists()
    assert not unreferenced_episode.exists()


def test_purge_disk_never_eats_schema_dirs_or_backcompat_symlinks(monkeypatch, tmp_path):
    """MIGRATION SAFETY: after the flat->per-episode migration, the legacy GC globs (`ep_*`,
    `ep_*_episode.mp4`, `*_raw.wav`, ...) also match the REAL schema dirs and the back-compat
    symlinks the migration left at old flat paths. _gc_unsafe must protect BOTH so the GC can't
    orphan a real render (unlink the symlink) or shred a whole episode dir."""
    assets = tmp_path / "assets"
    (assets / "editor_timelines").mkdir(parents=True)
    # a REAL schema episode dir with a render inside (the keeper)
    epdir = assets / "ep_a111111"
    (epdir / "renders").mkdir(parents=True)
    real_render = epdir / "renders" / "episode.mp4"
    real_render.write_bytes(b"the real render")
    # a per-episode scratch intermediate whose flat-glob pattern matches `*_raw.wav`
    (epdir / "scratch").mkdir()
    real_raw = epdir / "scratch" / "s0_raw.wav"
    real_raw.write_bytes(b"orig vo")
    # a back-compat SYMLINK at the old flat path (what the migration leaves), matches ep_*_episode.mp4
    flat_link = assets / "ep_a111111_episode.mp4"
    flat_link.symlink_to(real_render)
    # genuine flat junk with NO schema home -> still deletable
    junk = assets / "ep_b222222_s0_raw.wav"
    junk.write_bytes(b"junk")
    old = time.time() - 7200
    for p in (real_render, real_raw, flat_link, junk):
        os.utime(p, (old, old), follow_symlinks=False) if p.is_symlink() else os.utime(p, (old, old))

    usage = namedtuple("usage", ("total", "used", "free"))
    # point BOTH the factory ASSETS and the gateway ASSETS_DIR at the test store so is_managed works
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(100, 100, 0))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=0, low_disk_gb=999)

    # everything schema-shaped survives; only the homeless flat junk is reaped
    assert real_render.exists(), "GC orphaned a real render"
    assert real_raw.exists(), "GC ate an in-schema intermediate (origaudio-loss class bug)"
    assert flat_link.is_symlink(), "GC deleted a back-compat symlink (orphans the real file)"
    assert not junk.exists(), "GC should still reap genuine homeless flat junk"


def test_resolve_asset_subpath_url_preserves_subdir(monkeypatch, tmp_path):
    """A /agenticnews-assets/<subpath> URL must resolve to the FULL subpath under the store,
    not get flattened to the basename. Flattening was the bug that read a migrated per-episode
    asset back as 'missing' (basename collided with / pointed at the wrong dir)."""
    assets = tmp_path / "assets"
    monkeypatch.setattr(abn_factory, "ASSETS", assets)

    resolved = abn_factory._resolve_asset("/agenticnews-assets/ep_a111111/css/s0_card.png")
    assert resolved == assets / "ep_a111111" / "css" / "s0_card.png"
    # the per-episode subdir is preserved, not collapsed to the bare basename
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

    # bare relative name (legacy flat) -> under the store root, also non-raising
    bare = abn_factory._resolve_asset("ep_old_s0.wav")
    assert bare == assets / "ep_old_s0.wav"
    assert not bare.exists()

    # empty / None -> store root, never a raise
    assert abn_factory._resolve_asset(None) == assets
    assert abn_factory._resolve_asset("") == assets


def test_asset_url_roundtrips_through_resolve_asset(monkeypatch, tmp_path):
    """path -> _asset_url -> _resolve_asset must land back on the SAME on-disk path, including
    via the gateway's URL builder (asset_url_from_slug). This is the contract the timeline write +
    GC read both depend on; a mismatch silently blanks a segment."""
    assets = tmp_path / "assets"
    monkeypatch.setattr(abn_factory, "ASSETS", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)

    # direct path round-trip
    p = assets / "ep_a111111" / "css" / "s0_card.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"card")
    assert abn_factory._resolve_asset(abn_factory._asset_url(p)) == p.resolve()

    # gateway round-trip: factory slug -> managed URL -> resolved path, on a real written file
    url = abn_assets.asset_url_from_slug("ep_a111111_s0", "card")
    gw_path = abn_assets.asset_path_from_slug("ep_a111111_s0", "card")
    gw_path.write_bytes(b"gw card")
    resolved = abn_factory._resolve_asset(url)
    assert resolved == gw_path
    assert resolved.exists(), "gateway URL didn't round-trip back to the written file"


def test_is_editor_timeline_protected_asset_standalone(monkeypatch, tmp_path):
    """The protection predicate must hold OUTSIDE the GC loop: a path in the protected set is
    protected (modulo normalization — symlinks/.. resolve to the same real path), one outside
    it is not. It's the gate every GC unlink site (purge_disk + _gc_segments) consults before
    deleting, so its normalization contract is worth pinning independently of the loop."""
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

    # normalization: a non-canonical path to the SAME file still reads as protected
    noncanonical = assets / "sub" / ".." / "ep_a111111_s0.wav"
    (assets / "sub").mkdir()
    assert abn_factory._is_editor_timeline_protected_asset(noncanonical, protected) is True
