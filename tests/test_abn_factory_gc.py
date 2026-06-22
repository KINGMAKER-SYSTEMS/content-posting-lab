import asyncio
import importlib
import os
import shutil
import time
from collections import namedtuple
from pathlib import Path

import pytest

from scripts import migrate_abn_assets
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
    """Under low disk the GC trims the OLDEST real episode renders (keep N) by TOMBSTONING them to
    _trash/ (recoverable safe-delete, NOT unlink — a buggy trim must never be permanent data loss),
    and never touches a render still referenced by an Editor Bay timeline."""
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
    # the trimmed render must be RECOVERABLE from _trash/, not destroyed (the whole point of the fix)
    trashed = store / "_trash" / "ep_drop0" / "renders" / "episode.mp4"
    assert trashed.exists(), "trimmed render must be tombstoned to _trash/, not unlinked"
    assert trashed.read_bytes() == b"render", "tombstoned render content must be intact"


# --- gateway-level tombstone_render contract (the render safe-delete primitive) -----------------


def test_tombstone_render_moves_render_to_trash(store):
    """A real episode render is MOVED to _trash/ (recoverable), and its bytes are returned as freed."""
    d = store / "ep_a111111" / "renders"
    d.mkdir(parents=True)
    render = d / "episode.mp4"
    render.write_bytes(b"the real render")
    size = abn_factory.tombstone_render(render)
    assert size == len(b"the real render")
    assert not render.exists()
    dest = store / "_trash" / "ep_a111111" / "renders" / "episode.mp4"
    assert dest.exists() and dest.read_bytes() == b"the real render"


def test_tombstone_render_refuses_non_render_paths(store):
    """tombstone_render() is the physical enforcement point for the low-disk trim: handed a scratch
    file, an audio VO, a footage capture, a symlink, a dir, a reserved-top file, or an off-store
    path, it RAISES rather than deleting — so a buggy disk-trim caller can't cause data loss outside
    an episode's renders/."""
    epdir = store / "ep_a111111"
    (epdir / "renders").mkdir(parents=True)
    render = epdir / "renders" / "episode.mp4"
    render.write_bytes(b"render")
    (epdir / "audio").mkdir()
    vo = epdir / "audio" / "s0_voice.wav"
    vo.write_bytes(b"vo")
    (epdir / "footage").mkdir()
    ui = epdir / "footage" / "s3_ui.mp4"
    ui.write_bytes(b"capture")
    scratch_f = _scratch(store, "ep_a111111", "s0_raw.wav")
    link = store / "ep_a111111_episode.mp4"
    link.symlink_to(render)
    # a file under a RESERVED top dir that happens to sit in a renders/ subdir — must still RAISE
    (store / "_shared" / "renders").mkdir(parents=True)
    reserved = store / "_shared" / "renders" / "episode.mp4"
    reserved.write_bytes(b"shared")

    for bad in (vo, ui, scratch_f, link, epdir, reserved, store / "outside.txt"):
        with pytest.raises(abn_assets.AssetPathError):
            abn_factory.tombstone_render(bad)
    assert vo.exists() and ui.exists() and scratch_f.exists() and link.is_symlink() and reserved.exists()


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


# --- migration completes the flat dump into the schema (+ back-compat symlinks the GC spares) ----


def _migrator(store, monkeypatch):
    """Import the one-time migration script pointed at the throwaway store. It binds
    ASSETS_DIR / episode_dir / classify from services.abn_assets at import time, so we
    patch the module's own globals after the abn_assets.ASSETS_DIR monkeypatch (the
    `store` fixture already did that) to keep plan()/apply() on the throwaway store."""
    from scripts import migrate_abn_assets as m

    monkeypatch.setattr(m, "ASSETS_DIR", store)
    monkeypatch.setattr(m, "episode_dir", lambda ep_id: store / ep_id)
    return m


def test_migration_moves_flat_dump_into_schema_with_backcompat_symlinks(store, monkeypatch):
    """The whole point of the ticket: running --apply turns a flat ep_*_kind.ext dump into the
    per-episode schema, COPIES (never moves) each file to its typed subdir, and leaves a symlink at
    the OLD flat path so in-flight timelines/renders keep resolving. Nothing is destroyed."""
    m = _migrator(store, monkeypatch)

    flat = {
        "ep_a111111_s0_card.png": (b"card-bytes", "ep_a111111/css/s0_card.png"),
        "ep_a111111_s3_ui.mp4": (b"ui-bytes", "ep_a111111/footage/s3_ui.mp4"),
        "ep_a111111_s0_voice.wav": (b"vo-bytes", "ep_a111111/audio/s0_voice.wav"),
        "ep_a111111_episode.mp4": (b"render-bytes", "ep_a111111/renders/episode.mp4"),
        "ep_a111111_timeline.json": (b"{}", "ep_a111111/timeline.json"),
    }
    for name, (body, _dst) in flat.items():
        (store / name).write_bytes(body)

    m.apply(m.plan(None))

    for name, (body, dst_rel) in flat.items():
        dst = store / dst_rel
        assert dst.is_file() and dst.read_bytes() == body, f"{name} not copied to {dst_rel}"
        old = store / name
        assert old.is_symlink(), f"{name} flat path must become a back-compat symlink"
        assert old.resolve() == dst.resolve(), f"{name} symlink must point at the migrated file"


def test_migration_is_idempotent_and_heals_partial_runs(store, monkeypatch):
    """Re-running --apply must not duplicate or corrupt, AND must finish a crashed run: a file that
    was copied to the schema but never got its back-compat symlink (interrupted mid-apply) is healed
    into a symlink on the next run — so 'migration incomplete' can't persist."""
    m = _migrator(store, monkeypatch)

    # simulate a crash AFTER copy, BEFORE symlink: dst exists, flat src is still a real file.
    (store / "ep_a111111" / "css").mkdir(parents=True)
    (store / "ep_a111111" / "css" / "s0_card.png").write_bytes(b"card-bytes")
    flat = store / "ep_a111111_s0_card.png"
    flat.write_bytes(b"card-bytes")  # un-symlinked leftover from the interrupted run

    m.apply(m.plan(None))

    assert flat.is_symlink(), "a copied-but-unlinked flat file must be healed into a symlink"
    assert flat.resolve() == (store / "ep_a111111" / "css" / "s0_card.png").resolve()

    # second full re-run is a no-op: still exactly one symlink, still resolves, no .part/.link litter.
    m.apply(m.plan(None))
    assert flat.is_symlink()
    assert not list(store.glob("*.part")) and not list(store.glob("*.link"))


def test_gc_spares_symlinks_a_real_migration_leaves(store, monkeypatch):
    """End-to-end: migrate the flat dump, then run the disk GC. Every back-compat symlink the
    migration left must survive (deleting one orphans a live render reference) and every migrated
    schema file must survive — only an aged scratch intermediate is reaped. This is the invariant the
    ticket says was unproven until migration was actually run."""
    m = _migrator(store, monkeypatch)
    for name, body in {
        "ep_a111111_episode.mp4": b"render",
        "ep_a111111_s0_voice.wav": b"vo",
    }.items():
        (store / name).write_bytes(body)
    m.apply(m.plan(None))
    scratch_f = _scratch(store, "ep_a111111", "s0_raw.wav")

    usage = namedtuple("usage", ("total", "used", "free"))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(100, 0, 100 * 1e9))
    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    for name in ("ep_a111111_episode.mp4", "ep_a111111_s0_voice.wav"):
        assert (store / name).is_symlink(), f"GC deleted back-compat symlink {name}"
        assert (store / name).resolve().exists(), f"GC orphaned symlink {name}"
    assert (store / "ep_a111111" / "renders" / "episode.mp4").exists()
    assert (store / "ep_a111111" / "audio" / "s0_voice.wav").exists()
    assert not scratch_f.exists(), "GC should still reap the aged scratch intermediate"


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


def test_purge_disk_survives_poisoned_enumerator_returning_schema_dir(store, monkeypatch):
    """AUDIT (schema-dir deletions): even if reapable_scratch() were buggy and yielded a schema dir
    like ep_a12345/audio/ (the exact failure mode the enumeration guards exist to prevent), purge_disk
    must NOT reap it. Two independent layers catch it: the loop's `if f.is_file()` test is False for a
    directory so the tombstone call is never reached, and tombstone() itself RAISES on a non-scratch
    dir. We poison the enumerator at the factory call site and prove the schema dir is left fully
    intact while a legitimately-aged scratch file alongside it is still reaped."""
    audio_dir = store / "ep_a12345" / "audio"
    audio_dir.mkdir(parents=True)
    vo = audio_dir / "s0_voice.wav"
    vo.write_bytes(b"the real VO - must never be tombstoned")
    old = time.time() - 7200
    os.utime(audio_dir, (old, old))  # aged so a naive mtime check would select it

    legit = _scratch(store, "ep_a12345", "s0_raw.wav", body=b"reapable")

    # Simulate a regressed enumerator that leaks a schema dir into the reapable set.
    monkeypatch.setattr(abn_factory, "reapable_scratch", lambda: [audio_dir, legit])
    # keep disk healthy so the low-disk render trim path is irrelevant to this assertion
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 0, 100 * 1e9))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert audio_dir.is_dir(), "purge_disk reaped a schema dir handed to it by a poisoned enumerator"
    assert vo.exists() and vo.read_bytes() == b"the real VO - must never be tombstoned"
    assert not (store / "_trash" / "ep_a12345" / "audio").exists(), "schema dir must never be tombstoned"
    assert not legit.exists(), "the genuine aged scratch file should still have been reaped"
    assert (store / "_trash" / "ep_a12345" / "scratch" / "s0_raw.wav").exists()


def test_purge_disk_render_trim_survives_poisoned_enumerator_returning_schema_dir(store, monkeypatch):
    """AUDIT (schema-dir deletions, render-trim twin of the scratch-loop poisoned-enumerator test):
    even if _old_episode_renders() were buggy and leaked a schema dir (ep_a12345/audio/) or a symlink
    into the low-disk render-trim set, the trim must NOT reap it. Two layers catch it: the consume-site
    `is_file()/not is_symlink()` guard skips it before the destructive call, AND tombstone_render()
    itself RAISES on a non-render path. The real episode render IS still trimmed, so the guard doesn't
    over-block. Pins that the render-trim consume site checks is_file() BEFORE tombstone_render()."""
    audio_dir = store / "ep_a12345" / "audio"      # a schema dir masquerading as a render candidate
    audio_dir.mkdir(parents=True)
    vo = audio_dir / "s0_voice.wav"
    vo.write_bytes(b"the real VO - must never be tombstoned")

    def _render(store, ep_id, age_s):
        d = store / ep_id / "renders"
        d.mkdir(parents=True)
        f = d / "episode.mp4"
        f.write_bytes(b"render")
        t = time.time() - age_s
        os.utime(f, (t, t))
        return f

    real = _render(store, "ep_real00", age_s=9000)  # a genuine old render that SHOULD trim
    link_target = _render(store, "ep_tgt000", age_s=8000)
    bad_link = store / "ep_link00" / "renders"
    bad_link.mkdir(parents=True)
    poison_link = bad_link / "episode.mp4"
    poison_link.symlink_to(link_target)             # a symlinked render candidate

    # Regressed enumerator: leaks a dir and a symlink alongside the one real render.
    monkeypatch.setattr(
        abn_factory, "_old_episode_renders", lambda: [audio_dir, poison_link, real]
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 100, 0))

    # Record every path the destructive primitive is HANDED. The active consume-site is_file() guard
    # must keep the leaked dir/symlink from ever reaching tombstone_render() — proving the guard fires
    # BEFORE the call (not merely that tombstone_render()'s own RAISE, swallowed by except, backstops).
    real_tr = abn_factory.tombstone_render
    seen = []

    def spy(path):
        seen.append(Path(path))
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", spy)

    # keep_episodes=0 so the whole leaked list is in the trim slice
    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=0, low_disk_gb=999)

    assert audio_dir not in seen and poison_link not in seen, (
        "tombstone_render() was handed a leaked dir/symlink — the consume site must check is_file() FIRST"
    )
    assert seen == [real], "only the genuine regular-file render should reach tombstone_render()"
    assert audio_dir.is_dir(), "render trim reaped a schema dir handed in by a poisoned enumerator"
    assert vo.exists() and vo.read_bytes() == b"the real VO - must never be tombstoned"
    assert poison_link.is_symlink(), "render trim must skip a symlinked render candidate"
    assert link_target.exists(), "the symlink's target render must not be reaped via the symlink"
    assert not (store / "_trash" / "ep_a12345" / "audio").exists(), "schema dir must never be tombstoned"
    assert not real.exists(), "the genuine old render should still have been trimmed"
    assert (store / "_trash" / "ep_real00" / "renders" / "episode.mp4").exists(), "real render must tombstone"


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


# --- migration <-> gateway vocabulary parity (the silent "read as missing" class) ----------------


@pytest.fixture()
def migrate_store(tmp_path, monkeypatch):
    """Point the gateway AND the migration script at one throwaway store. The migrator binds
    ASSETS_DIR by value at import time, so patch its module global too (episode_dir reads the live
    abn_assets.ASSETS_DIR, so patching that redirects it)."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    monkeypatch.setattr(migrate_abn_assets, "ASSETS_DIR", assets)
    return assets


def _migratable_kinds():
    """Every real per-segment kind: skip the scratch bucket and the episode-root singletons
    (timeline/manifest) — those have no legacy `{ep}_s0_{kind}` flat form to migrate."""
    for kind, (subdir, ext) in abn_assets.KINDS.items():
        if subdir in ("scratch", "."):
            continue
        yield kind, subdir, ext


def test_migration_destinations_match_gateway_kind_subdirs(migrate_store):
    """Every file migrate_abn_assets.plan() moves must land in the SAME subdir the runtime
    gateway (abn_assets.asset_path) would write that kind to. If the migrator classified a file
    into a subdir the gateway doesn't use for that kind, a timeline URL built by the gateway would
    point at an empty dir and the read would treat the asset as missing — with no error. This test
    is the parity gate that test_resolve_asset_subpath_url_preserves_subdir (subpath contract) and
    the GC contract tests do not cover."""
    ep = "ep_a111111"
    # one flat legacy file per real kind: the exact `{ep}_s{N}_{kind}.{ext}` form the factory dumped
    flat_names = {}
    for kind, _subdir, ext in _migratable_kinds():
        flat = migrate_store / f"{ep}_s0_{kind}.{ext or 'bin'}"
        flat.write_bytes(b"x")
        flat_names[flat.name] = kind

    moves = migrate_abn_assets.plan(only_ep=ep)
    by_src = {src.name: dst for src, dst, _reason in moves}

    # nothing dropped on the floor: every legacy file got a planned destination
    assert set(by_src) == set(flat_names), "plan() must produce a move for every episode-scoped file"

    for fname, kind in flat_names.items():
        dst = by_src[fname]
        gateway_subdir = abn_assets.asset_path(ep, kind, "s0").parent.name
        assert dst.parent.name == gateway_subdir, (
            f"migration put {kind!r} in {dst.parent.name!r} but the gateway writes it to "
            f"{gateway_subdir!r} — a timeline read of the gateway URL would see this as missing"
        )
        # and the migrated path is actually UNDER this episode's dir (not a shared/scratch escape)
        assert dst.parent.parent.name == ep


def test_migrated_asset_url_roundtrips_back_to_disk(migrate_store, monkeypatch):
    """End-to-end: a legacy `{ep}_s0_card.png` migrates to .../{ep}/css/s0_card.png, and the URL
    the gateway builds for that same (ep, kind, slug) resolves (via _resolve_asset) back to the
    migrated file on disk. This is the concrete failure the parity gate guards against — a subdir
    mismatch would make this round-trip miss."""
    monkeypatch.setattr(abn_factory, "ASSETS", migrate_store)
    ep = "ep_a111111"
    flat = migrate_store / f"{ep}_s0_card.png"
    flat.write_bytes(b"card-bytes")

    (src, dst, _reason), = (
        m for m in migrate_abn_assets.plan(only_ep=ep) if m[0].name == flat.name
    )
    # simulate the migration copy landing the bytes at the planned destination
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(flat.read_bytes())

    url = abn_assets.asset_url(ep, "card", "s0")
    resolved = abn_factory._resolve_asset(url)
    assert resolved == dst, "gateway URL must resolve to the migrated destination"
    assert resolved.exists(), "migrated file is unreadable via the gateway URL (read-as-missing bug)"
    assert resolved.read_bytes() == b"card-bytes"
