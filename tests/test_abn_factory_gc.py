import asyncio
import importlib
import json
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


def _scratch_cross(assets, name, *, age_s=7200, body=b"intermediate"):
    """Write a CROSS-EPISODE _scratch/ file aged `age_s` seconds in the past."""
    d = assets / "_scratch"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(body)
    old = time.time() - age_s
    os.utime(f, (old, old))
    return f


# --- scratch_dirs(): the GC's reapable-root enumerator must never reach a non-scratch surface ----
# The whole GC safety invariant (abn_assets ~406-431) rests on scratch_dirs() returning ONLY
# per-episode {ep}/scratch/ + cross-episode _scratch/. These pin the enumerator directly (the reap
# loop / tombstone are covered elsewhere) so a regression in its iteration or symlink checks is
# caught at the source — before it can hand a keeper-bearing dir to reapable_scratch().


def test_scratch_dirs_returns_only_scratch_roots(store):
    """Happy path: a real per-episode scratch/ and the cross-episode _scratch/ are the ONLY roots
    returned — schema layer dirs (renders/audio/footage/css) alongside them are never offered."""
    (store / "ep_a111111" / "scratch").mkdir(parents=True)
    (store / "ep_a111111" / "renders").mkdir()
    (store / "ep_a111111" / "audio").mkdir()
    (store / "_scratch").mkdir()

    got = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert got == {
        (store / "ep_a111111" / "scratch").resolve(),
        (store / "_scratch").resolve(),
    }


def test_scratch_dirs_never_reaches_reserved_top_dirs(store):
    """A scratch/ subdir living UNDER a reserved top dir (_published/_shared/_trash) must NEVER be
    enumerated — those names don't match the episode-id regex, so even a literal 'scratch' child of
    _published/ (a shipped final's working dir) is not a reapable root."""
    for top in ("_published", "_shared", "_trash"):
        (store / top / "scratch").mkdir(parents=True)
        (store / top / "ep_a111111" / "scratch").mkdir(parents=True)  # nested ep dir under reserved top
    # one legitimate root so we know the enumerator is actually running
    (store / "ep_dead00" / "scratch").mkdir(parents=True)

    got = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert got == {(store / "ep_dead00" / "scratch").resolve()}, (
        "scratch_dirs leaked a scratch/ under a reserved top dir as a reapable root"
    )


def test_scratch_dirs_skips_symlinked_episode_dir(store):
    """A symlinked EPISODE dir (e.g. ep_beef00 -> ep_dead00) must be skipped: enumerating its
    scratch/ would reap through the link and orphan the real episode's keepers. Only the real
    episode's scratch/ is returned."""
    (store / "ep_dead00" / "scratch").mkdir(parents=True)
    (store / "ep_beef00").symlink_to(store / "ep_dead00")

    got = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert got == {(store / "ep_dead00" / "scratch").resolve()}
    # the symlinked episode dir's scratch/ (same resolved target) is not added a SECOND time either
    assert len(abn_assets.scratch_dirs()) == 1, "symlinked episode dir was enumerated as its own root"


def test_scratch_dirs_skips_symlinked_scratch_pointing_into_published(store):
    """THE STALE-SYMLINK ESCAPE: a real episode dir whose ``scratch`` is itself a SYMLINK pointing
    into _published/ (a stale/relocated link, or a malicious one) must NOT be returned. ``is_dir()``
    follows symlinks, so without an explicit ``not is_symlink()`` guard the GC would rglob into
    _published/ and tombstone shipped finals. The real episode's scratch/ alongside it still works."""
    (store / "_published" / "ep_a111111").mkdir(parents=True)
    final = store / "_published" / "ep_a111111" / "episode.mp4"
    final.write_bytes(b"shipped final - must never be reapable")
    # ep_bad00/scratch is a symlink INTO _published/ (resolves to a dir, but is a symlink)
    (store / "ep_bad000").mkdir()
    (store / "ep_bad000" / "scratch").symlink_to(store / "_published" / "ep_a111111")
    # a normal episode scratch alongside it
    (store / "ep_cab000" / "scratch").mkdir(parents=True)

    roots = abn_assets.scratch_dirs()
    resolved = {d.resolve() for d in roots}
    assert (store / "_published" / "ep_a111111").resolve() not in resolved, (
        "scratch_dirs followed a symlinked scratch/ into _published/ — a GC rglob would eat finals"
    )
    assert resolved == {(store / "ep_cab000" / "scratch").resolve()}
    # end-to-end: the shipped final is therefore NOT in the reapable set
    assert final.resolve() not in {f.resolve() for f in abn_assets.reapable_scratch()}


def test_scratch_dirs_ignores_episodelike_regular_file(store):
    """A malformed entry at the store root that LOOKS like an episode id but is a regular FILE
    (not a dir) must be ignored — iterdir() yields it, but the ``is_dir()`` gate drops it, so no
    AttributeError / stray root. A legitimate episode dir alongside it is still enumerated."""
    (store / "ep_a111111").write_bytes(b"not a dir, just a file named like an episode")
    (store / "ep_fad000" / "scratch").mkdir(parents=True)

    got = {d.resolve() for d in abn_assets.scratch_dirs()}
    assert got == {(store / "ep_fad000" / "scratch").resolve()}


def test_scratch_dirs_missing_store_returns_empty(store):
    """If ASSETS_DIR doesn't exist yet (fresh boot, pre-migration), scratch_dirs() returns [] rather
    than raising — the FileNotFoundError/OSError guard around iterdir() holds."""
    import shutil as _sh

    _sh.rmtree(store)
    assert abn_assets.scratch_dirs() == []


# --- reapable_scratch(): the enumeration layer of the GC SAFETY INVARIANT (abn_assets ~414-417) ---
# scratch_dirs() pins the reapable ROOTS; these pin the FILE enumerator that walks them. Its docstring
# (abn_assets ~455-457) promises directories and symlinks under scratch/ are excluded — a symlink under
# scratch/ could point INTO the schema, and following + tombstoning it would orphan the real keeper.
# That exact loss vector (a link INSIDE a real scratch dir, not a symlinked root) was not pinned at the
# enumeration layer, so a regression dropping the `not is_symlink()` check would pass every other test.


def test_reapable_scratch_excludes_symlink_inside_scratch_pointing_at_a_keeper(store):
    """THE IN-SCRATCH-SYMLINK ESCAPE: a real ``{ep}/scratch/`` that contains a SYMLINK pointing at a
    real keeper (a shipped render) must NOT yield that link from ``reapable_scratch()``. ``is_file()``
    follows symlinks, so without the ``not is_symlink()`` guard the GC would resolve the link to the
    render and hand it to tombstone() — orphaning the keeper. Real scratch files alongside it still reap.
    This is distinct from the symlinked-ROOT cases (scratch_dirs): here the root is a genuine scratch
    dir and the link is a FILE inside it."""
    render = store / "ep_keep00" / "renders" / "episode.mp4"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"shipped render - must never be reapable")
    sd = store / "ep_dead00" / "scratch"
    sd.mkdir(parents=True)
    real = sd / "intermediate.png"
    real.write_bytes(b"spent scratch")
    (sd / "looks_like_scratch.mp4").symlink_to(render)  # link INTO the schema

    reapable = {f.resolve() for f in abn_assets.reapable_scratch()}
    assert render.resolve() not in reapable, (
        "reapable_scratch followed a symlink inside scratch/ to a real render — a GC reap would orphan it"
    )
    assert real.resolve() in reapable, "the real scratch file alongside the link was not enumerated"


def test_reapable_scratch_excludes_directories_under_scratch(store):
    """rglob('*') yields DIRECTORIES too; only regular files may be reaped. A subdir under scratch/
    must not appear in the reapable set (tombstone() only moves regular files), while the regular file
    nested inside it does."""
    sd = store / "ep_decade" / "scratch"
    nested = sd / "frames"
    nested.mkdir(parents=True)
    inner = nested / "f001.png"
    inner.write_bytes(b"frame")

    reapable = {f.resolve() for f in abn_assets.reapable_scratch()}
    assert nested.resolve() not in reapable, "reapable_scratch yielded a directory"
    assert inner.resolve() in reapable, "the regular file nested under scratch/ was not enumerated"


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


def test_protection_scan_strips_cachebuster_query_on_agenticnews_url(store):
    """A /agenticnews-assets/ render URL persisted by the editor with a ?rev=N cache-buster
    (EditorBay uses these — see EditorBay.test.tsx) must still protect the real file. The static
    mount ignores the query, so the keeper on disk is 'episode.mp4', not 'episode.mp4?rev=3'.
    Without stripping the query the protection path never matches and the GC tombstones a render
    an Editor Bay timeline still references — the exact schema-rooted miss this audit hunts for."""
    d = store / "ep_q00000" / "renders"
    d.mkdir(parents=True)
    keeper = d / "episode.mp4"
    keeper.write_bytes(b"render")
    old = time.time() - 9000
    os.utime(keeper, (old, old))
    rel = keeper.relative_to(store)
    (store / "editor_timelines" / "ep_q00000.json").write_text(
        f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}?rev=3"}}}}}}'
    )

    protected = abn_factory._editor_timeline_asset_paths()
    assert abn_factory._is_editor_timeline_protected_asset(keeper, protected), (
        "cache-busted /agenticnews-assets/ URL must resolve to the real file and protect it"
    )

    usage = namedtuple("usage", ("total", "used", "free"))
    _orig = shutil.disk_usage
    shutil.disk_usage = lambda _: usage(100, 100, 0)  # critically low → render trim fires
    try:
        abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=0, low_disk_gb=999)
    finally:
        shutil.disk_usage = _orig

    assert keeper.exists(), "GC trimmed a render referenced by a ?rev= cache-busted timeline URL"


def test_cachebuster_url_protects_and_renders_the_same_file_end_to_end(store):
    """THE DRIFT GUARD the audit asks for: drive ONE cache-buster URL
    (/agenticnews-assets/.../episode.mp4?rev=3) through BOTH the GC protection scan AND the
    OpenShot render path in the SAME test, and prove they land on the IDENTICAL on-disk file.

    The two sides strip the cache-buster independently — the GC via
    `_editor_timeline_asset_paths` (abn_factory) and the compiler via
    `openshot_bridge._resolve_asset_src`. test_resolve_asset_src_strips_query_and_fragment_in_parity_with_gc_scan
    pins the resolver shape, and test_protection_scan_strips_cachebuster_query_on_agenticnews_url
    pins the scan — but nothing pinned that the path the GC PROTECTS equals the path the compiler
    OPENS for the same URL. If they ever drift (GC protects 'episode.mp4', compiler opens
    'episode.mp4?rev=3', or vice versa), the file is reaped under one name while the render tries
    to open another — silent loss at render time. This test fails the moment that divergence opens.
    """
    from services import openshot_bridge

    d = store / "ep_drift0" / "renders"
    d.mkdir(parents=True)
    keeper = d / "episode.mp4"
    keeper.write_bytes(b"render")
    old = time.time() - 9000
    os.utime(keeper, (old, old))
    rel = keeper.relative_to(store)
    busted = f"/agenticnews-assets/{rel}?rev=3"

    # 1) The compiler's resolver (the path libopenshot would actually open at render time).
    #    Go through reader_json — the public seam the compiler actually calls (openshot_bridge
    #    ~331), not just the inner _resolve_asset_src — so a drift introduced anywhere between the
    #    reader build and the strip is caught, not only in the helper. This is the exact entry the
    #    ticket names: protect 'renders/episode.mp4', ask reader_json to resolve the ?rev=3 URL,
    #    and assert both land on the SAME real file.
    reader = openshot_bridge.reader_json(
        {}, {"src": busted, "type": "video"}, duration=1.0, asset_root=store
    )
    rendered = Path(reader["path"])

    # 2) The GC protection scan (the path the GC promises never to reap), from a timeline that
    #    persists the exact same cache-buster URL.
    (store / "editor_timelines" / "ep_drift0.json").write_text(
        f'{{"renderCache": {{"video": {{"path": "{busted}"}}}}}}'
    )
    protected = abn_factory._editor_timeline_asset_paths()

    # The render path AND the protected path must both be the SINGLE real file on disk — locked
    # together so a one-sided change to either strip rule trips this immediately.
    assert rendered.resolve() == keeper.resolve(), "compiler opens a path that isn't the real render"
    assert abn_factory._is_editor_timeline_protected_asset(rendered, protected), (
        "the file the compiler opens is NOT the file the GC protects — protect/render drift"
    )

    # 3) End-to-end under a real low-disk trim: the protected render survives, and the compiler's
    #    resolved path is still openable afterwards (it's the surviving keeper, not a reaped name).
    usage = namedtuple("usage", ("total", "used", "free"))
    _orig = shutil.disk_usage
    shutil.disk_usage = lambda _: usage(100, 100, 0)  # critically low → render trim fires
    try:
        abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=0, low_disk_gb=999)
    finally:
        shutil.disk_usage = _orig

    assert keeper.exists(), "GC reaped the cache-busted render the compiler resolves to"
    assert rendered.exists(), "compiler's resolved render path is missing after the GC cycle"


def test_cachebuster_strip_is_a_single_shared_helper_not_three_copies(store):
    """STRUCTURAL drift guard: the GC protection scan, abn_factory's `_resolve_asset`, and the
    OpenShot compiler's `_resolve_asset_src` must all strip the cache-buster through the SAME
    `openshot_bridge.strip_cachebuster` — not three hand-copied `.split('?')…split('#')` chains.

    test_cachebuster_url_protects_and_renders_the_same_file_end_to_end catches drift behaviorally
    for one URL shape; this catches it structurally: if any site re-forks its own strip, the
    shared helper stops being the single source and these assertions break. We exercise a few
    awkward shapes (fragment-before-query, double query, no buster) so the helper, the resolver,
    and the protection scan are all proven to agree byte-for-byte on the on-disk name."""
    from services import openshot_bridge

    cases = {
        "ep/renders/episode.mp4?rev=3": "ep/renders/episode.mp4",
        "ep/renders/episode.mp4#frag?rev=3": "ep/renders/episode.mp4",
        "ep/renders/episode.mp4?a=1&b=2": "ep/renders/episode.mp4",
        "ep/renders/episode.mp4": "ep/renders/episode.mp4",
    }
    for rel, want in cases.items():
        # 1) the canonical helper
        assert openshot_bridge.strip_cachebuster(rel) == want, rel
        # 2) the compiler resolver lands on asset_root / <stripped>
        url = f"/agenticnews-assets/{rel}"
        assert openshot_bridge._resolve_asset_src(url, asset_root=store) == str(store / want), rel
        # 3) abn_factory's _resolve_asset agrees with both
        assert abn_factory._resolve_asset(url) == abn_factory.ASSETS / want, rel


def test_protection_scan_reaches_deeply_nested_clip_keyframe_refs(store):
    """The collect() traversal must reach asset refs buried arbitrarily deep — a real editor timeline
    nests them under clips[] → keyframes[] (and an effects[] sidecar), not at the top level. If the
    recursion stopped short of any of these, the GC would tombstone a still-referenced asset. Every
    leaf URL — even one inside a list-of-dicts inside a list-of-dicts — must end up protected."""
    d = store / "ep_aaabbb" / "scratch"
    d.mkdir(parents=True)
    clip_src = d / "s0_clip.mp4"
    keyframe_img = d / "s0_keyframe.png"
    effect_lut = d / "s0_effect.cube"
    for f in (clip_src, keyframe_img, effect_lut):
        f.write_bytes(b"asset")

    def rel(p):
        return "/agenticnews-assets/" + str(p.relative_to(store))

    timeline = {
        "tracks": [
            {
                "clips": [
                    {
                        "src": rel(clip_src),
                        "keyframes": [
                            {"poster": rel(keyframe_img)},
                        ],
                        "effects": [{"params": {"lut": rel(effect_lut)}}],
                    }
                ]
            }
        ]
    }
    (store / "editor_timelines" / "ep_aaabbb.json").write_text(json.dumps(timeline))

    protected = abn_factory._editor_timeline_asset_paths()
    for f in (clip_src, keyframe_img, effect_lut):
        assert abn_factory._is_editor_timeline_protected_asset(f, protected), (
            f"deeply nested ref {f.name} was not reached by the protection scan"
        )


def test_protection_scan_handles_rendercache_variants(store):
    """renderCache is more than the one ?rev= case the original test covered. A bare path (no query),
    a #fragment-only cache-buster, AND a list of cache entries must ALL protect their real files.
    Each variant strips to the same on-disk file the static mount serves."""
    d = store / "ep_cc0011" / "renders"
    d.mkdir(parents=True)
    bare = d / "bare.mp4"
    fragged = d / "fragged.mp4"
    listed = d / "listed.mp4"
    for f in (bare, fragged, listed):
        f.write_bytes(b"render")

    def rel(p):
        return "/agenticnews-assets/" + str(p.relative_to(store))

    timeline = {
        "renderCache": {
            "video": {"path": rel(bare)},                       # no cache-buster at all
            "preview": {"path": rel(fragged) + "#t=3"},         # fragment-only cache-buster
            "history": [                                         # a LIST of cache entries
                {"path": rel(listed) + "?rev=9"},
            ],
        }
    }
    (store / "editor_timelines" / "ep_cc0011.json").write_text(json.dumps(timeline))

    protected = abn_factory._editor_timeline_asset_paths()
    for f in (bare, fragged, listed):
        assert abn_factory._is_editor_timeline_protected_asset(f, protected), (
            f"renderCache variant pointing at {f.name} did not protect the real file"
        )


def test_deeply_nested_timeline_ref_survives_full_gc_cycle(store):
    """Integration: a timeline whose only ref is buried under tracks→clips→keyframes survives a full
    purge_disk() cycle, while an aged, UNREFERENCED sibling in the same episode is reaped. This proves
    the recursive collect() result is actually consulted by the reap loop end to end — not just by the
    standalone predicate — so a missing traversal pattern can't let the GC eat a referenced asset."""
    protected = _scratch(store, "ep_dd0022", "s0_clip.mp4")
    unreferenced = _scratch(store, "ep_dd0022", "s1_orphan.mp4")
    rel = "/agenticnews-assets/" + str(protected.relative_to(store))
    timeline = {"tracks": [{"clips": [{"keyframes": [{"src": rel}]}]}]}
    (store / "editor_timelines" / "ep_dd0022.json").write_text(json.dumps(timeline))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert protected.exists(), "GC reaped an asset referenced only by a deeply nested timeline path"
    assert not unreferenced.exists(), "GC should still reap the unreferenced sibling"


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


def test_old_episode_renders_excludes_reserved_top_dirs_and_symlinks(store):
    """The low-disk trim only ever tombstones what _old_episode_renders() ENUMERATES, so the
    enumerator itself must never offer a non-episode render as a GC candidate. A render under a
    reserved-top dir (_shared/_scratch/_published, and crucially _trash/ — where a prior tombstone
    landed one) or under a symlinked episode dir must NOT appear, even though each sits at a
    .../renders/episode.mp4 path. Only the two real episode renders are returned."""
    def render(parent_rel, body=b"render"):
        d = store / parent_rel / "renders"
        d.mkdir(parents=True)
        f = d / "episode.mp4"
        f.write_bytes(body)
        return f

    real_a = render("ep_a111111")
    real_b = render("ep_b222222")
    # reserved-top renders — must be invisible to the enumerator (the `_`-prefix skip)
    render("_shared")
    render("_published")
    render("_scratch")
    trashed = render("_trash/ep_dead00")  # a previously-tombstoned render — must not be re-enumerated
    # a SYMLINKED episode dir pointing at a real one — must be skipped (no double-trim of the target)
    (store / "ep_beef00").symlink_to(store / "ep_a111111")

    found = set(abn_factory._old_episode_renders())
    assert found == {real_a, real_b}, "enumerator leaked a reserved-top or symlinked render as a GC candidate"
    assert trashed not in found, "a tombstoned _trash/ render must never be re-enumerated for re-trim"


def test_old_episode_renders_skips_non_mp4_render_files(store):
    """Belt-and-suspenders: even if the render slot somehow held a non-.mp4 file (a stray
    'episode' with no extension, or 'episode.txt'), the enumerator must NOT offer it as a
    low-disk tombstone candidate — is_file()+not is_symlink() alone would pass it. We force the
    candidate basename to a non-mp4 name to prove the suffix guard rejects it, then restore the
    real 'episode.mp4' to confirm the same dir IS enumerated when the extension is correct.

    NOTE: we save/restore Path.__truediv__ in a finally (not monkeypatch.undo) on purpose —
    undo() would also tear down the `store` fixture's ASSETS redirect and send the enumerator at
    the real volume."""
    d = store / "ep_a111111" / "renders"
    d.mkdir(parents=True)
    (d / "episode.txt").write_bytes(b"not a render")

    # Point the enumerator's hardcoded candidate at the wrong-extension file.
    real_join = Path.__truediv__

    def fake_join(self, other):
        if other == "episode.mp4":
            return real_join(self, "episode.txt")
        return real_join(self, other)

    Path.__truediv__ = fake_join
    try:
        assert abn_factory._old_episode_renders() == [], "a non-.mp4 render file must never be a GC candidate"
    finally:
        Path.__truediv__ = real_join

    (d / "episode.mp4").write_bytes(b"the real render")
    assert (d / "episode.mp4") in abn_factory._old_episode_renders(), "a real .mp4 render must still enumerate"


# --- scratch_usage(): the per-episode growth-measurement hook (ticket deliverable #1) ----------


def test_scratch_usage_breaks_down_bytes_per_owner(store):
    """scratch_usage() reports reapable scratch bytes keyed by owner — ep_id for a per-episode
    scratch/ and '_scratch' for the cross-episode root — so prod can age per-episode growth."""
    _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * 100)
    _scratch(store, "ep_a111111", "s1_raw.wav", body=b"y" * 50)
    _scratch(store, "ep_b222222", "s0_raw.wav", body=b"z" * 30)
    cross = store / "_scratch"
    cross.mkdir()
    (cross / "probe.png").write_bytes(b"q" * 10)

    usage = abn_assets.scratch_usage()
    assert usage == {"ep_a111111": 150, "ep_b222222": 30, "_scratch": 10}


def test_scratch_usage_only_measures_reapable_scratch_never_renders_or_audio(store):
    """The measurement boundary IS the reap boundary: a render, a VO, and a footage capture must
    never count toward 'scratch growth' — only files under scratch/ do. This pins that an operator
    reading scratch_usage() can't be misled into thinking a render is reapable scratch."""
    epdir = store / "ep_a111111"
    (epdir / "renders").mkdir(parents=True)
    (epdir / "renders" / "episode.mp4").write_bytes(b"R" * 1000)
    (epdir / "audio").mkdir()
    (epdir / "audio" / "s0_voice.wav").write_bytes(b"V" * 1000)
    (epdir / "footage").mkdir()
    (epdir / "footage" / "s3_ui.mp4").write_bytes(b"F" * 1000)
    _scratch(store, "ep_a111111", "s0_raw.wav", body=b"S" * 7)

    usage = abn_assets.scratch_usage()
    assert usage == {"ep_a111111": 7}, "only the scratch file may be measured as scratch growth"


def test_scratch_usage_empty_store_is_empty(store):
    """No scratch anywhere → empty dict (purge_disk's emit guard skips the BUS line)."""
    assert abn_assets.scratch_usage() == {}


def test_purge_disk_emits_scratch_usage_breakdown(store, monkeypatch):
    """purge_disk emits the per-owner scratch breakdown so growth is observable in prod (it runs
    BEFORE the reap, so it measures what's there at GC time)."""
    _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * (3 * 1024 * 1024), age_s=10)

    emitted = []
    monkeypatch.setattr(abn_factory.BUS, "emit", lambda *a, **k: emitted.append(a))
    # don't actually reap (fresh file) — we only assert the measurement emit fired
    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=99, low_disk_gb=0)

    msgs = [a[2] for a in emitted if len(a) >= 3 and a[1] == "gc"]
    assert any("scratch usage" in m and "ep_a111111" in m for m in msgs), (
        f"purge_disk must emit a per-owner scratch usage line; got {msgs}"
    )


def test_purge_disk_reaps_even_when_scratch_usage_observability_raises(store, monkeypatch):
    """The observability emit must NEVER break the GC (abn_factory ~3221: 'never let measurement
    break the GC'). If scratch_usage() itself blows up, purge_disk must swallow it and still reap the
    aged scratch — a failed metric is not allowed to leave disk un-freed. Pins that the try/except
    around the usage emit is load-bearing, not decorative."""
    f = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * 100)

    def boom():
        raise RuntimeError("usage probe blew up")

    monkeypatch.setattr(abn_factory, "scratch_usage", boom)

    # Must not raise, and the reap must still happen despite the measurement failure.
    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert not f.exists(), "GC stopped reaping because the observability emit raised"
    assert (store / "_trash" / "ep_a111111" / "scratch" / "s0_raw.wav").exists(), (
        "aged scratch must still be tombstoned even when scratch_usage() fails"
    )


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


def test_gc_segments_emits_scratch_usage_breakdown(store, monkeypatch):
    """The AUTONOMOUS factory-loop GC (_gc_segments, run every few cycles by run_factory_loop) must
    emit the per-owner scratch usage breakdown too — not only purge_disk's manual /gc endpoint. This
    is the exact ticket gap: scratch_usage() observability was tested but never emitted in the prod
    path that actually runs under load. Like purge_disk it measures BEFORE the reap, and the emit is
    best-effort so a measurement failure can never break the GC."""
    _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * (3 * 1024 * 1024), age_s=10)

    async def list_videos(*a, **k):
        return []

    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)
    # healthy disk so the low-disk render trim path stays irrelevant to this assertion
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 0, 100 * 1e9))

    emitted = []
    monkeypatch.setattr(abn_factory.BUS, "emit", lambda *a, **k: emitted.append(a))
    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    msgs = [a[2] for a in emitted if len(a) >= 3 and a[1] == "gc"]
    assert any("scratch usage" in m and "ep_a111111" in m for m in msgs), (
        f"_gc_segments must emit a per-owner scratch usage line in prod; got {msgs}"
    )


def test_gc_segments_reaps_even_when_scratch_usage_observability_raises(store, monkeypatch):
    """Twin of test_purge_disk_reaps_even_when_scratch_usage_observability_raises for the prod
    _gc_segments path: if scratch_usage() blows up, the emit must be swallowed and the aged scratch
    must still be tombstoned — a failed metric is never allowed to leave disk un-freed."""
    f = _scratch(store, "ep_a111111", "s0_raw.wav", body=b"x" * 100)

    def boom():
        raise RuntimeError("usage probe blew up")

    monkeypatch.setattr(abn_factory, "scratch_usage", boom)

    async def list_videos(*a, **k):
        return []

    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 0, 100 * 1e9))

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert not f.exists(), "_gc_segments stopped reaping because the observability emit raised"
    assert (store / "_trash" / "ep_a111111" / "scratch" / "s0_raw.wav").exists(), (
        "aged scratch must still be tombstoned by _gc_segments even when scratch_usage() fails"
    )


def test_gc_segments_low_disk_trim_honors_cachebusted_timeline_ref(store, monkeypatch):
    """End-to-end on the OTHER GC path: the async _gc_segments low-disk render trim must also strip a
    ?rev= cache-buster off a /agenticnews-assets/ timeline URL and spare the real render. purge_disk()
    is covered by test_protection_scan_strips_cachebuster_query_on_agenticnews_url, but _gc_segments
    runs its own protected_paths gate (abn_factory ~3172/3189) and its render trim hardcodes keep-4 —
    so it needs its own proof that the cache-busted keeper survives even when it's among the oldest."""
    def render(ep_id, age_s):
        d = store / ep_id / "renders"
        d.mkdir(parents=True)
        f = d / "episode.mp4"
        f.write_bytes(b"render")
        t = time.time() - age_s
        os.utime(f, (t, t))
        return f

    # 6 renders so the keep-4 trim has 2 victims; the oldest is the cache-busted keeper.
    keeper = render("ep_beef00", age_s=9000)            # oldest -> first trim victim, but protected
    drop_old = render("ep_drop00", age_s=8500)          # second-oldest, unreferenced -> trimmed
    for i, age in enumerate((8000, 7000, 6000, 10)):
        render(f"ep_fill0{i}", age_s=age)
    rel = keeper.relative_to(store)
    (store / "editor_timelines" / "ep_beef00.json").write_text(
        f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}?rev=7"}}}}}}'
    )

    async def list_videos(*a, **k):
        return []

    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 100, 0))

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert keeper.exists(), "_gc_segments trimmed a render referenced by a ?rev= cache-busted timeline URL"
    assert not drop_old.exists(), "_gc_segments should still trim the oldest unreferenced render under low disk"
    assert (store / "_trash" / "ep_drop00" / "renders" / "episode.mp4").exists(), "trim must tombstone, not unlink"


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
    intact while a legitimately-aged scratch file alongside it is still reaped.

    EXTENDED (cross-episode _scratch poisoning): the per-episode scratch case above is only half the
    reapable surface — purge_disk enumerates {ep}/scratch/ AND the cross-episode _scratch/ together.
    The nastier leak is a SYMLINK under _scratch/ pointing at a schema keeper (e.g. _scratch/leak.mp4
    -> ep_a12345/renders/episode.mp4): unlike a bare dir, `is_file()` follows the link and is True, so
    the scratch-reap loop (which has no is_symlink() consume-site guard) WOULD reach the tombstone
    call. tombstone()'s own is_symlink() RAISE is then the sole layer standing between a poisoned
    _scratch enumerator and destroying the real render. This proves that link is left intact, the
    render it targets survives untouched, and a genuine aged _scratch file alongside it is still
    reaped."""
    audio_dir = store / "ep_a12345" / "audio"
    audio_dir.mkdir(parents=True)
    vo = audio_dir / "s0_voice.wav"
    vo.write_bytes(b"the real VO - must never be tombstoned")
    old = time.time() - 7200
    os.utime(audio_dir, (old, old))  # aged so a naive mtime check would select it

    legit = _scratch(store, "ep_a12345", "s0_raw.wav", body=b"reapable")

    # Cross-episode _scratch/ poisoning: a real aged scratch file (legitimately reapable) PLUS a
    # symlink under _scratch/ that points at a schema keeper (a render). is_file() follows the link,
    # so only tombstone()'s is_symlink() RAISE stops the render from being eaten.
    cross_legit = _scratch_cross(store, "throwaway.png", body=b"cross-reapable")
    render = _render(store, "ep_a12345", age_s=10)
    render_bytes = render.read_bytes()
    leak_link = store / "_scratch" / "leak.mp4"
    leak_link.symlink_to(render)

    # Simulate a regressed enumerator that leaks a schema dir AND a _scratch symlink-into-renders.
    monkeypatch.setattr(
        abn_factory, "reapable_scratch", lambda: [audio_dir, legit, leak_link, cross_legit]
    )
    # keep disk healthy so the low-disk render trim path is irrelevant to this assertion
    monkeypatch.setattr(shutil, "disk_usage", lambda _: namedtuple("u", "total used free")(100, 0, 100 * 1e9))

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)

    assert audio_dir.is_dir(), "purge_disk reaped a schema dir handed to it by a poisoned enumerator"
    assert vo.exists() and vo.read_bytes() == b"the real VO - must never be tombstoned"
    assert not (store / "_trash" / "ep_a12345" / "audio").exists(), "schema dir must never be tombstoned"
    assert not legit.exists(), "the genuine aged scratch file should still have been reaped"
    assert (store / "_trash" / "ep_a12345" / "scratch" / "s0_raw.wav").exists()

    # cross-episode _scratch/ assertions
    assert leak_link.is_symlink(), "purge_disk followed+tombstoned a _scratch symlink into renders/"
    assert render.exists() and render.read_bytes() == render_bytes, (
        "the render targeted by a poisoned _scratch symlink was destroyed"
    )
    assert not cross_legit.exists(), "the genuine aged _scratch file should still have been reaped"
    assert (store / "_trash" / "_scratch" / "throwaway.png").exists()


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

    real = _render(store, "ep_dead00", age_s=9000)  # a genuine old render that SHOULD trim
    link_target = _render(store, "ep_tgt000", age_s=8000)
    bad_link = store / "ep_beef00" / "renders"
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
    assert (store / "_trash" / "ep_dead00" / "renders" / "episode.mp4").exists(), "real render must tombstone"


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


# --- _cross_scratch_path: the gateway chokepoint for cross-episode _scratch/ writes -------------
#
# _codex_image / _wan_i2v_sync / _bg_is_clean used to hand-build `ASSETS / "_scratch" / name` +
# os.mkdir and write through it directly (shutil.copy / urllib download), bypassing the runtime
# write-path validation the asset gateway exists to enforce (abn_assets line 14). An off-schema
# name slipping through there would be unrecognised by the GC (disk-fill / corruption risk). These
# tests pin that all three now funnel through one validated chokepoint that the GC reaps.


def test_cross_scratch_path_lands_in_managed_reapable_scratch(store):
    """A cross-episode scratch name routes to _scratch/, the gateway recognises it as managed, and
    the GC's reapable set includes it — so nothing written here is an unreapable stray."""
    p = abn_factory._cross_scratch_path("_tmp_bg_0.png")
    assert p == store / "_scratch" / "_tmp_bg_0.png"
    assert p.parent.is_dir(), "chokepoint must create the _scratch/ dir"
    assert abn_assets.is_managed(p), "write path must be gateway-managed (the runtime guard)"
    p.write_bytes(b"x")
    assert p.resolve() in {f.resolve() for f in abn_assets.reapable_scratch()}, (
        "GC must recognise the cross-scratch write as reapable"
    )


def test_cross_scratch_path_url_roundtrips_to_disk(store):
    """The /agenticnews-assets/_scratch/<name> URL the helper feeds back to callers (and that
    _grow_bg_library strips with removeprefix) must resolve to the exact file on disk."""
    p = abn_factory._cross_scratch_path("libgen3_broll.mp4")
    url = abn_factory._asset_url(p)
    assert url == "/agenticnews-assets/_scratch/libgen3_broll.mp4"
    assert abn_factory._resolve_asset(url) == p


@pytest.mark.parametrize("bad", ["../evil.png", "a/b.png", "a\\b.png", ".hidden", "..", ""])
def test_cross_scratch_path_rejects_off_schema_names(store, bad):
    """Traversal, slashes, leading dot and empty all RAISE before any directory is created or any
    byte is written — the off-schema path is rejected at runtime, never reaching disk."""
    with pytest.raises(ValueError):
        abn_factory._cross_scratch_path(bad)
    # the reject path must not have created a _scratch/ dir as a side effect for a traversal name
    assert not (store / "_scratch" / bad).exists()


# --- GC FAIL-SAFE: a swallowed protection-scan error must NOT let the low-disk trim eat a render --

_LOW_DISK = namedtuple("usage", ("total", "used", "free"))(100, 100, 0)


def _force_low_disk(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _: _LOW_DISK)


def test_protection_scan_complete_flag_tracks_unreadable_timeline(store):
    """A timeline JSON that can't be parsed marks the scan INCOMPLETE — the scanner can't promise it
    saw every referenced render, so it must signal `complete=False` (the fail-safe input)."""
    (store / "editor_timelines" / "good.json").write_text('{"clips": []}')
    (store / "editor_timelines" / "broken.json").write_text("{ this is not json")

    _paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is False, "an unparseable timeline must mark the protection scan incomplete"


def test_protection_scan_complete_when_all_timelines_readable(store):
    """No editor_timelines dir or all-readable timelines → scan is COMPLETE (trim is allowed)."""
    (store / "editor_timelines" / "ok.json").write_text('{"clips": [{"path": "x"}]}')
    _paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is True


def test_protection_scan_incomplete_on_glob_failure(store, monkeypatch):
    """If even globbing the timeline dir errors (permission denied), the scan is incomplete and the
    set is empty — the dangerous combination the fail-safe exists for."""
    real_glob = Path.glob

    def boom(self, pattern):
        if self.name == "editor_timelines":
            raise PermissionError("denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", boom)
    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert paths == set()
    assert complete is False


def test_protection_scan_dir_is_where_editor_store_persists(store):
    """CRITICAL invariant: the GC's protection scan globs ASSETS/editor_timelines/*.json
    (abn_factory line ~201). If the editor-bay actually persisted timelines anywhere else
    (a different dir, a DB, an in-memory-only cache), the scan would return complete=True
    while missing a live reference and the disk-wall would tombstone a still-referenced render.

    This pins the two halves to the SAME dir by going through the REAL persistence path the
    router uses (services.editor_timeline.TimelineStore rooted at ASSETS/editor_timelines —
    the only place editor timelines are ever written; there is no in-memory-only path) and
    asserting the GC scan actually picks up a render that timeline references.
    """
    from services import editor_timeline

    # Save a timeline EXACTLY like routers/agenticnews.py _editor_timeline_store() does.
    timeline_store = editor_timeline.TimelineStore(store / "editor_timelines")
    referenced = "/agenticnews-assets/ep_storedir/renders/episode.mp4"
    timeline_store.save({
        "projectId": "ep_storedir",
        "clips": [{"path": referenced}],
    })

    # The store must have written into the very dir the GC scan reads — not elsewhere.
    on_disk = store / "editor_timelines" / "ep_storedir.json"
    assert on_disk.exists(), "TimelineStore must persist into ASSETS/editor_timelines/"

    paths, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete is True, "a cleanly-saved timeline must yield a COMPLETE scan"
    expected = abn_factory._normalize_asset_path(
        store / "ep_storedir" / "renders" / "episode.mp4"
    )
    assert expected in paths, (
        "render referenced by a real editor-bay timeline must be seen by the GC protection "
        "scan — if it isn't, the editor store and the scan dir have drifted apart"
    )


def _render(store, ep_id, age_s):
    d = store / ep_id / "renders"
    d.mkdir(parents=True)
    f = d / "episode.mp4"
    f.write_bytes(b"render")
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def test_purge_disk_skips_render_trim_when_protection_scan_incomplete(store, monkeypatch):
    """THE BUG THIS TICKET GUARDS: an unreadable timeline JSON (a swallowed parse error) must NOT let
    the low-disk render trim tombstone old renders. Because the scan couldn't confirm what's still
    referenced, the destructive trim is skipped entirely — the old render survives. Disk pressure is
    recoverable; deleting a render an active editor depends on 500s the next render."""
    old_render = _render(store, "ep_old000", age_s=9000)
    _render(store, "ep_5e0000", age_s=10)
    # A broken timeline → protection scan is incomplete (some refs unknown).
    (store / "editor_timelines" / "broken.json").write_text("{ not json at all")
    _force_low_disk(monkeypatch)

    freed = abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert old_render.exists(), (
        "low-disk trim ran despite an incomplete protection scan — it could have eaten a live render"
    )
    assert not (store / "_trash" / "ep_old000" / "renders" / "episode.mp4").exists()
    assert freed == 0


def test_purge_disk_skips_render_trim_when_timeline_glob_permission_denied(store, monkeypatch):
    """Companion to test_purge_disk_skips_render_trim_when_protection_scan_incomplete, but the scan
    fails for the OTHER reason the fail-safe exists for: globbing the timeline dir itself raises (a
    PermissionError / OSError mid-scan, not a per-file parse error). _editor_timeline_asset_paths_checked
    returns (empty_set, complete=False), and the downstream low-disk render trim must respect that and
    skip — an empty-but-incomplete protected set must NOT be read as 'nothing is referenced, trim freely'.
    Without this, a denied glob would look identical to a clean empty scan and the disk-wall would
    tombstone a render an active editor still depends on."""
    old_render = _render(store, "ep_old222", age_s=9000)
    _render(store, "ep_5e2222", age_s=10)

    real_glob = Path.glob

    def boom(self, pattern):
        if self.name == "editor_timelines":
            raise PermissionError("denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", boom)
    _force_low_disk(monkeypatch)

    freed = abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert old_render.exists(), (
        "low-disk trim ran despite a permission-denied timeline glob — an incomplete scan whose set "
        "happens to be empty must still skip the destructive render trim, not trim freely"
    )
    assert not (store / "_trash" / "ep_old222" / "renders" / "episode.mp4").exists()
    assert freed == 0


def test_purge_disk_incomplete_scan_still_reaps_scratch(store, monkeypatch):
    """THE FAIL-SAFE MUST STAY NARROW: an incomplete protection scan (malformed timeline.json) skips
    ONLY the destructive low-disk render trim — the non-destructive scratch reap must still run and
    free disk. The natural over-broad "fix" for this ticket is to bail out of purge_disk the moment
    the scan is incomplete; that would let a single half-flushed timeline.json silently stop ALL disk
    reclamation (the GC's whole job) and the suite would stay green. This pins that scratch is still
    reaped under an incomplete scan, so the guard can't be widened into a full-GC kill switch.

    Aged scratch is NOT timeline-referenceable in a way the broken JSON could protect (the broken
    file carries no parseable refs), so reaping it is always safe regardless of scan completeness."""
    old_render = _render(store, "ep_4a4444", age_s=9000)
    reapable = _scratch(store, "ep_4a4444", "s0_raw.wav", body=b"x" * 100)
    # A broken timeline → protection scan is incomplete (render trim must be skipped)...
    (store / "editor_timelines" / "broken.json").write_text("{ not json at all")
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=1, low_disk_gb=999)

    # ...but the scratch reap still happened: the aged intermediate was tombstoned (→ _trash/).
    assert not reapable.exists(), (
        "incomplete protection scan stopped the scratch reap — the fail-safe was widened from "
        "'skip the destructive render trim' into a full-GC kill switch; disk would never be reclaimed"
    )
    assert (store / "_trash" / "ep_4a4444" / "scratch" / "s0_raw.wav").exists(), (
        "aged scratch must still be tombstoned (→ _trash/) even when the protection scan is incomplete"
    )
    # the destructive render trim is STILL correctly skipped (the other half of the contract).
    assert old_render.exists(), "render trim ran despite the incomplete scan — must stay skipped"


def test_gc_segments_incomplete_scan_still_reaps_scratch(store, monkeypatch):
    """Twin of test_purge_disk_incomplete_scan_still_reaps_scratch for the _gc_segments disk phase:
    an incomplete protection scan must skip only the render trim, not the scratch reap. Same regression
    target — a guard widened into an early bail would silently stop _gc_segments from reclaiming disk."""
    old_render = _render(store, "ep_5a5555", age_s=9000)
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):       # 6 total renders, keep-4 -> 2 victims
        _render(store, f"ep_5f111{i}", age_s=age)
    reapable = _scratch(store, "ep_5a5555", "s0_raw.wav", body=b"x" * 100, age_s=7200)
    (store / "editor_timelines" / "broken.json").write_text("{ not json at all")
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert not reapable.exists(), (
        "_gc_segments stopped the scratch reap under an incomplete scan — the fail-safe was widened "
        "into a full-GC kill switch instead of skipping only the destructive render trim"
    )
    assert (store / "_trash" / "ep_5a5555" / "scratch" / "s0_raw.wav").exists(), (
        "aged scratch must still be tombstoned by _gc_segments even when the protection scan is incomplete"
    )
    assert old_render.exists(), "_gc_segments render trim ran despite the incomplete scan — must stay skipped"


def test_purge_disk_still_trims_when_protection_scan_is_complete(store, monkeypatch):
    """Control: with a clean (complete) protection scan and low disk, the oldest unreferenced render
    IS trimmed — the fail-safe only blocks the trim when the scan genuinely failed, not always."""
    old_render = _render(store, "ep_old111", age_s=9000)
    _render(store, "ep_new111", age_s=10)
    (store / "editor_timelines" / "clean.json").write_text('{"clips": []}')
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not old_render.exists(), "a complete scan + low disk should still trim the oldest render"
    assert (store / "_trash" / "ep_old111" / "renders" / "episode.mp4").exists(), (
        "trimmed render must be tombstoned (recoverable), not unlinked"
    )


def test_purge_disk_scratch_reap_survives_protection_check_raising(store, monkeypatch):
    """A protection check that RAISES mid-loop (e.g. stat/normalize blowing up on one path) must not
    crash purge_disk or cause it to delete the file it failed to classify — the file is left intact
    and the GC moves on. Fail-closed on a per-file protection error."""
    target = _scratch(store, "ep_raise0", "s0_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset

    def explode(path, protected):
        if path == target:
            raise OSError("simulated stat/permission failure")
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", explode)

    # Must not raise, and must not reap the file it couldn't classify.
    freed = abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=99, low_disk_gb=0)
    assert target.exists(), "a scratch file whose protection check raised must be left intact"
    assert not (store / "_trash" / "ep_raise0" / "scratch" / "s0_raw.wav").exists()
    assert freed == 0


def test_cross_scratch_chokepoint_self_asserts_reapability_not_just_managed(store, monkeypatch):
    """The chokepoint's own guard must prove REAPABILITY, not merely is_managed(). is_managed() is
    True for broll_library/, _shared/, _published/, editor_renders/ — all managed but the GC NEVER
    reaps them — so the old is_managed()-only guard would wave through a write into a dir the GC
    can't touch (the unreapable-stray / disk-bloat failure this chokepoint exists to prevent).

    Concretely: is_managed() is strictly broader than the GC's reapable-root set. This pins that the
    chokepoint consults scratch_dirs() (the GC's actual reapable roots), NOT is_managed(): when the
    destination's parent is absent from scratch_dirs(), the write must RAISE even though is_managed()
    would say yes. Patching scratch_dirs() to exclude _scratch/ proves the guard reads it."""
    # the gap the tightened guard closes: a managed dir the GC never reaps. is_managed() passes it;
    # the reapable-root set does not — so an is_managed()-only guard would have let it through.
    unreapable = store / "broll_library" / "cache.png"
    assert abn_assets.is_managed(unreapable), "precondition: broll_library/ is is_managed()"
    assert unreapable.parent.resolve() not in {d.resolve() for d in abn_assets.scratch_dirs()}, (
        "precondition: broll_library/ is NOT a GC reapable root"
    )

    # with _scratch/ NOT in the reapable roots, the chokepoint must refuse even its normal target —
    # proving the guard is gated on scratch_dirs() membership, not the broader is_managed() check.
    monkeypatch.setattr(abn_factory, "scratch_dirs", lambda: [])
    with pytest.raises(ValueError):
        abn_factory._cross_scratch_path("probe_still.png")


# --- _gc_segments low-disk trim: the OTHER destructive path's incomplete-scan fail-safe ---------
# purge_disk's incomplete-scan skip is pinned above; _gc_segments (abn_factory ~3214-3238) runs its
# OWN protected_paths/protection_complete gate before its hardcoded keep-4 render trim. This is the
# exact span this ticket cites — it needs its own proof that an incomplete scan blocks the trim.


def _gc_no_videos(monkeypatch):
    """_gc_segments queries db.list_videos for the board-prune phase; stub it so we isolate the
    disk-trim phase. (We don't want a real DB and we don't want board pruning to interfere.)"""
    async def list_videos(*a, **k):
        return []
    monkeypatch.setattr(abn_factory.db, "list_videos", list_videos)


def test_gc_segments_skips_render_trim_when_protection_scan_incomplete(store, monkeypatch):
    """THE BUG THIS TICKET GUARDS (second GC path): an unreadable timeline JSON makes the protection
    scan incomplete, so _gc_segments' low-disk render trim must be SKIPPED entirely — even the oldest
    unreferenced render survives, because the scan couldn't confirm it isn't referenced by a timeline
    we failed to read. Disk pressure is recoverable; tombstoning a live render 500s the next render."""
    # >4 renders so the hardcoded keep-4 slice (_old_episode_renders()[4:]) WOULD yield a victim if
    # the trim ran — otherwise the gate is untested (with the guard gone, the trim must eat the
    # oldest; the guard is what makes it survive).
    old_render = _render(store, "ep_old222", age_s=9000)        # oldest -> trim victim absent guard
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):      # 5 newer -> 6 total, keep 4
        _render(store, f"ep_fill2{i}", age_s=age)
    # A broken timeline → protection scan is incomplete (some refs unknown).
    (store / "editor_timelines" / "broken.json").write_text("{ not json at all")
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert old_render.exists(), (
        "_gc_segments low-disk trim ran despite an incomplete protection scan — it could have eaten "
        "a render an active Editor Bay timeline still references"
    )
    assert not (store / "_trash" / "ep_old222" / "renders" / "episode.mp4").exists(), (
        "no render should have been tombstoned while the protection scan was incomplete"
    )


# --- GC TOCTOU: a timeline saved DURING purge_disk (between the initial scan and the render trim)
# must still protect its render. The protected set is captured once at the top of the function; the
# scratch-reap loop takes wall-clock time, during which an editor can save a new timeline. The fix
# re-scans immediately before the destructive trim and ORs the result in.


def test_purge_disk_protects_render_referenced_by_timeline_saved_during_reap(store, monkeypatch):
    """THE TOCTOU THIS TICKET GUARDS: at function entry NO timeline references ep_1a7e00's render, so
    the initial protection scan does NOT protect it. While the scratch-reap loop runs, an Editor Bay
    timeline is saved referencing that render. A stale (entry-time) protected set would let the
    low-disk trim tombstone a now-live render. The re-scan before the trim must spare it."""
    late = _render(store, "ep_1a7e00", age_s=9000)   # oldest -> first trim victim absent the fix
    _render(store, "ep_beef00", age_s=10)
    rel = late.relative_to(store)

    # Drop a reapable scratch file so the reap loop has work to do — and use ITS protection check as
    # the hook to simulate "an editor saved a timeline mid-reap" (writes the timeline JSON exactly
    # once, after the entry-time scan already ran but before the render trim re-scan).
    _scratch(store, "ep_1a7e00", "s9_raw.wav")
    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"wrote": False}

    def hook(path, protected):
        if not state["wrote"]:
            state["wrote"] = True
            (store / "editor_timelines" / "ep_1a7e00.json").write_text(
                f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}"}}}}}}'
            )
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=1, low_disk_gb=999)

    assert state["wrote"], "test setup: the mid-reap timeline write never fired"
    assert late.exists(), (
        "render referenced by a timeline saved DURING the reap was trimmed — the protected set was "
        "stale; the destructive trim must re-scan protection before deleting"
    )
    assert not (store / "_trash" / "ep_1a7e00" / "renders" / "episode.mp4").exists()


def test_purge_disk_skips_trim_when_rescan_becomes_incomplete(store, monkeypatch):
    """If the protection scan was complete at entry but a timeline becomes UNREADABLE by the time of
    the pre-trim re-scan (an editor mid-write left a half-flushed JSON), the trim must be SKIPPED —
    the re-scan can no longer promise it saw every referenced render. Fail-safe on the fresh scan."""
    old_render = _render(store, "ep_1c0000", age_s=9000)
    _render(store, "ep_5e0000", age_s=10)
    (store / "editor_timelines" / "clean.json").write_text('{"clips": []}')  # entry scan: complete
    _scratch(store, "ep_1c0000", "s9_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"broke": False}

    def hook(path, protected):
        if not state["broke"]:
            state["broke"] = True
            (store / "editor_timelines" / "broken.json").write_text("{ half-flushed not json")
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=1, low_disk_gb=999)

    assert old_render.exists(), "trim ran even though the pre-trim re-scan became incomplete"
    assert not (store / "_trash" / "ep_1c0000" / "renders" / "episode.mp4").exists()


def test_gc_segments_protects_render_referenced_by_timeline_saved_during_reap(store, monkeypatch):
    """Same TOCTOU on the _gc_segments path: a timeline saved during the scratch reap must protect its
    render against the keep-4 low-disk trim via the pre-trim re-scan."""
    late = _render(store, "ep_91a7e0", age_s=9000)               # oldest -> trim victim absent the fix
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):       # 5 newer -> 6 total, keep 4
        _render(store, f"ep_9f111{i}", age_s=age)
    rel = late.relative_to(store)
    _scratch(store, "ep_91a7e0", "s9_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"wrote": False}

    def hook(path, protected):
        if not state["wrote"]:
            state["wrote"] = True
            (store / "editor_timelines" / "ep_91a7e0.json").write_text(
                f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}"}}}}}}'
            )
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert state["wrote"], "test setup: the mid-reap timeline write never fired"
    assert late.exists(), (
        "_gc_segments trimmed a render referenced by a timeline saved DURING the reap — the trim must "
        "re-scan protection before deleting"
    )
    assert not (store / "_trash" / "ep_91a7e0" / "renders" / "episode.mp4").exists()


def test_gc_segments_still_trims_when_protection_scan_is_complete(store, monkeypatch):
    """Control: with a clean (complete) protection scan and low disk, _gc_segments DOES trim the
    oldest unreferenced render (keep-4) — the fail-safe blocks the trim only when the scan genuinely
    failed, not always. Needs >4 renders so the keep-4 trim has a victim."""
    old_render = _render(store, "ep_old333", age_s=9000)        # oldest, unreferenced -> trimmed
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):       # 5 newer -> 6 total, keep 4
        _render(store, f"ep_fill3{i}", age_s=age)
    (store / "editor_timelines" / "clean.json").write_text('{"clips": []}')
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert not old_render.exists(), "a complete scan + low disk should still trim the oldest render"
    assert (store / "_trash" / "ep_old333" / "renders" / "episode.mp4").exists(), (
        "trimmed render must be tombstoned (recoverable), not unlinked"
    )


def test_purge_disk_union_protects_render_seen_only_by_entry_scan(store, monkeypatch):
    """THE OTHER HALF OF THE OR-UNION (the symmetric twin of the saved-during-reap test): a render
    referenced by a timeline that exists at function ENTRY but is DELETED during the scratch reap (an
    editor closes a project / clears a draft mid-GC) must STILL be protected. The fresh pre-trim
    re-scan no longer sees it, but the entry scan did — and the comment promises a render protected by
    EITHER scan survives. If the code did `protected_paths = fresh_paths` (replace) instead of
    `protected_paths | fresh_paths` (OR), this render would be tombstoned. This pins the union keeps
    the entry-only side, not just the fresh-only side."""
    entry_only = _render(store, "ep_e17000", age_s=9000)   # oldest -> first trim victim absent the fix
    _render(store, "ep_face00", age_s=10)
    rel = entry_only.relative_to(store)
    # Timeline referencing the render EXISTS at entry, so the entry scan protects it.
    timeline = store / "editor_timelines" / "ep_e17000.json"
    timeline.write_text(f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}"}}}}}}')
    _scratch(store, "ep_e17000", "s9_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"deleted": False}

    def hook(path, protected):
        # During the scratch-reap loop (after the entry scan, before the pre-trim re-scan) the editor
        # removes the timeline — the FRESH scan will be complete but no longer reference this render.
        if not state["deleted"]:
            state["deleted"] = True
            timeline.unlink()
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=1, low_disk_gb=999)

    assert state["deleted"], "test setup: the mid-reap timeline deletion never fired"
    # fresh scan is COMPLETE (all remaining timelines parse) but no longer references the render, so
    # only the entry-scan side of the OR keeps it alive — proving the union isn't a replace.
    _fresh, complete = abn_factory._editor_timeline_asset_paths_checked()
    assert complete, "precondition: the post-deletion fresh scan must be complete (not the skip path)"
    assert entry_only.exists(), (
        "render protected only by the ENTRY scan was trimmed — the pre-trim re-scan REPLACED the "
        "protected set instead of OR-ing it; a render protected by EITHER scan must survive"
    )
    assert not (store / "_trash" / "ep_e17000" / "renders" / "episode.mp4").exists()


def test_gc_segments_union_protects_render_seen_only_by_entry_scan(store, monkeypatch):
    """Same OR-union-keeps-the-entry-side guarantee on the _gc_segments path: a timeline present at
    entry but deleted during the scratch reap must still protect its render via the entry-scan side of
    the union, even though the (complete) fresh re-scan no longer references it. Needs >4 renders so
    the keep-4 trim has a victim."""
    entry_only = _render(store, "ep_a55e70", age_s=9000)        # oldest -> trim victim absent the fix
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):       # 5 newer -> 6 total, keep 4
        _render(store, f"ep_b011{i}", age_s=age)
    rel = entry_only.relative_to(store)
    timeline = store / "editor_timelines" / "ep_a55e70.json"
    timeline.write_text(f'{{"renderCache": {{"video": {{"path": "/agenticnews-assets/{rel}"}}}}}}')
    _scratch(store, "ep_a55e70", "s9_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"deleted": False}

    def hook(path, protected):
        if not state["deleted"]:
            state["deleted"] = True
            timeline.unlink()
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert state["deleted"], "test setup: the mid-reap timeline deletion never fired"
    assert entry_only.exists(), (
        "_gc_segments trimmed a render protected only by the ENTRY scan — the re-scan must OR the two "
        "protected sets, not replace the entry set with the fresh one"
    )
    assert not (store / "_trash" / "ep_a55e70" / "renders" / "episode.mp4").exists()


def test_gc_segments_skips_trim_when_rescan_becomes_incomplete(store, monkeypatch):
    """Twin of test_purge_disk_skips_trim_when_rescan_becomes_incomplete for the _gc_segments path:
    the ENTRY scan is complete, but a timeline becomes UNREADABLE by the pre-trim re-scan (an editor
    mid-write left a half-flushed JSON). The fresh scan can no longer promise it saw every referenced
    render, so the trim must be SKIPPED — this pins the `fresh_complete` branch the ticket clarified."""
    old_render = _render(store, "ep_2d0000", age_s=9000)        # oldest -> trim victim absent guard
    for i, age in enumerate((8000, 7000, 6000, 5000, 10)):      # 5 newer -> 6 total, keep 4
        _render(store, f"ep_fill4{i}", age_s=age)
    (store / "editor_timelines" / "clean.json").write_text('{"clips": []}')  # entry scan: complete
    _scratch(store, "ep_2d0000", "s9_raw.wav")

    real_predicate = abn_factory._is_editor_timeline_protected_asset
    state = {"broke": False}

    def hook(path, protected):
        # Fires during the scratch-reap loop, BEFORE the pre-trim re-scan: corrupt a timeline so the
        # fresh scan comes back incomplete even though the entry scan was clean.
        if not state["broke"]:
            state["broke"] = True
            (store / "editor_timelines" / "broken.json").write_text("{ half-flushed not json")
        return real_predicate(path, protected)

    monkeypatch.setattr(abn_factory, "_is_editor_timeline_protected_asset", hook)
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert state["broke"], "test setup: the mid-reap timeline corruption never fired"
    assert old_render.exists(), (
        "_gc_segments trim ran even though the pre-trim re-scan became incomplete — an editor mid-write "
        "must block the trim, not let it eat a render the unreadable timeline might reference"
    )
    assert not (store / "_trash" / "ep_2d0000" / "renders" / "episode.mp4").exists()


# --- SOURCE-LEVEL invariant: the GC protection scan only walks ASSETS/editor_timelines/*.json,
# so it is correct ONLY IF that dir is the SOLE place Editor Bay timelines are ever persisted.
# test_protection_scan_dir_is_where_editor_store_persists pins the ONE store the router builds, but
# it would still pass if a SECOND persistence path (a differently-rooted TimelineStore, a DB
# persister) were added — that second path would carry live render references the scan never sees,
# complete=True would be wrong, and the disk-wall would tombstone an in-use render. These two tests
# fail loudly the moment such a drift is introduced, so the fail-safe's core assumption stays true.

def _repo_root() -> Path:
    return Path(abn_factory.__file__).resolve().parent.parent


def _repo_py_sources() -> list[Path]:
    """First-party PRODUCTION .py sources only — the runtime that can persist a timeline.

    Tests intentionally build throwaway TimelineStore(tmp_path) roots, so they're excluded; the
    invariant under test is about where the SHIPPING code persists timelines, not test fixtures."""
    root = _repo_root()
    skip = {".git", ".venv", "venv", "node_modules", ".claude", "yt-pipeline", "ec-repos",
            "agentonline", ".codex", "__pycache__", "tests", "scripts"}
    out = []
    for p in root.rglob("*.py"):
        parts = set(p.relative_to(root).parts)
        if parts & skip:
            continue
        out.append(p)
    return out


def test_timeline_store_is_only_constructed_under_editor_timelines():
    """Every TimelineStore(...) in first-party source must be rooted at the SAME dir the GC scans
    (.../editor_timelines). A construction rooted anywhere else is a second persistence path the
    protection scan can't see — exactly the silent-render-deletion the fail-safe guards against."""
    import re

    # `TimelineStore(` followed (on the same logical line) by a root arg that mentions editor_timelines.
    ctor = re.compile(r"TimelineStore\(([^)]*)\)")
    offenders = []
    for src in _repo_py_sources():
        text = src.read_text(encoding="utf-8", errors="ignore")
        for m in ctor.finditer(text):
            arg = m.group(1)
            if "editor_timelines" not in arg:
                offenders.append(f"{src.relative_to(_repo_root())}: TimelineStore({arg.strip()})")
    assert not offenders, (
        "found TimelineStore construction(s) NOT rooted at editor_timelines/ — the GC protection "
        "scan only walks ASSETS/editor_timelines/*.json, so any other root persists live render "
        "references the scan can never see (silent render deletion under low disk):\n  "
        + "\n  ".join(offenders)
    )


def test_editor_timeline_store_persists_only_to_a_single_json_per_project(tmp_path):
    """The store's path_for() must keep timelines as flat {projectId}.json directly under its root
    (no nesting, no second sidecar file) — the protection scan globs editor_timelines/*.json one
    level deep, so a nested or alternately-named persistence file would be missed by the scan."""
    from services import editor_timeline

    root = tmp_path / "editor_timelines"
    st = editor_timeline.TimelineStore(root)
    saved = st.save({"projectId": "ep_single0", "clips": [{"path": "x"}]})

    on_disk = list(root.glob("*.json"))
    assert on_disk == [root / "ep_single0.json"], (
        "TimelineStore must persist exactly one flat {projectId}.json under its root; the GC "
        "protection scan only globs editor_timelines/*.json one level deep"
    )
    assert saved["projectId"] == "ep_single0"
    # No nested timeline JSONs hiding below the scanned glob depth.
    assert not list(root.glob("*/*.json"))


# --- TOCTOU hardening: a timeline saved DURING the destructive trim must still protect a render ---
# The fresh re-scan captured just before the trim loop is stale the instant the loop starts: while we
# tombstone render N, an Editor Bay save can land referencing render N+1 the loop is about to delete.
# The wider the trim, the wider that window. _LiveProtectionSet re-scans per-iteration (mtime-token
# gated) so a mid-trim save is caught before the next render is tombstoned.


def _agenticnews_ref(store, render_path):
    return "/agenticnews-assets/" + str(render_path.relative_to(store))


def test_purge_disk_render_trim_protects_render_referenced_by_timeline_saved_mid_trim(store, monkeypatch):
    """A timeline saved AFTER the pre-trim fresh re-scan but BEFORE the loop reaches its render must
    spare that render. We trip the save from inside the tombstone of an EARLIER victim — exactly the
    interleaving the ticket calls out — and assert the later, now-referenced render survives while the
    genuinely-unreferenced earlier one is still trimmed.

    Trim order is newest-first over the post-keep slice, so the victim must be NEWER than the render
    it later protects — that way the victim is tombstoned first and the save lands before the loop
    reaches the protected (older) render."""
    keep_fresh = _render(store, "ep_keepfr", age_s=10)      # newest -> kept (keep_episodes=1)
    victim = _render(store, "ep_vvvv00", age_s=8000)        # next-newest victim -> trims first
    late_protected = _render(store, "ep_llll00", age_s=9000)  # oldest; referenced mid-trim, reached last

    real_tr = abn_factory.tombstone_render

    def save_timeline_then_tombstone(path):
        # The instant the first victim is being tombstoned, an Editor Bay timeline lands referencing
        # the NEXT render in the trim list. This bumps the editor_timelines mtime token.
        if Path(path) == victim:
            (store / "editor_timelines" / "ep_llll00.json").write_text(
                '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, late_protected)
            )
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", save_timeline_then_tombstone)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not victim.exists(), "the genuinely-unreferenced oldest render should still be trimmed"
    assert late_protected.exists(), (
        "a render referenced by a timeline saved mid-trim was tombstoned — the per-iteration live "
        "re-scan failed to pick up the save before deleting the next render (the TOCTOU this guards)"
    )


def test_gc_segments_render_trim_protects_render_referenced_by_timeline_saved_mid_trim(store, monkeypatch):
    """Twin of the purge_disk test on the async _gc_segments path: a mid-trim timeline save protects a
    not-yet-deleted render. 6 renders so the keep-4 trim has 2 victims; the save lands while the first
    victim is tombstoned and references the oldest, not-yet-reached render.

    Trim order is newest-first, so of the two keep-4 victims the victim must be the NEWER one (reached
    first) and late_protected the oldest (reached last)."""
    late_protected = _render(store, "ep_bb0000", age_s=9000)  # oldest -> reached last in the trim slice
    victim = _render(store, "ep_aa0000", age_s=8500)         # second-oldest -> first victim
    for i, age in enumerate((8000, 7000, 6000, 10)):
        _render(store, f"ep_cc000{i}", age_s=age)

    real_tr = abn_factory.tombstone_render

    def save_timeline_then_tombstone(path):
        if Path(path) == victim:
            (store / "editor_timelines" / "ep_bb0000.json").write_text(
                '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, late_protected)
            )
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", save_timeline_then_tombstone)
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert not victim.exists(), "_gc_segments should still trim the genuinely-unreferenced oldest render"
    assert late_protected.exists(), (
        "_gc_segments tombstoned a render referenced by a timeline saved mid-trim — the per-iteration "
        "live re-scan missed the save"
    )


def test_live_protection_set_does_not_rescan_when_no_timeline_changes(store, monkeypatch):
    """Cost guard: when NO timeline changes during the trim, the live set must NOT re-parse the
    timelines on every render — it compares a cheap mtime token first. A stable token means zero extra
    full re-scans. Pins that the common case stays a stat-per-render, not a glob+parse-per-render."""
    live = abn_factory._LiveProtectionSet(set())

    calls = []
    real_checked = abn_factory._editor_timeline_asset_paths_checked

    def counting_checked():
        calls.append(1)
        return real_checked()

    monkeypatch.setattr(abn_factory, "_editor_timeline_asset_paths_checked", counting_checked)

    # No timeline activity between checks -> token stable -> never re-scans.
    for _ in range(5):
        assert live.contains(store / "ep_x" / "renders" / "episode.mp4") is False
    assert calls == [], "live set re-scanned timelines with no change — the mtime-token gate is broken"


def test_live_protection_set_stops_trim_when_mid_trim_save_is_unparseable(store, monkeypatch):
    """If the timeline that lands mid-trim can't be parsed, the live set can't promise it knows the
    full referenced set, so `.complete` flips False and the caller must STOP trimming (protect
    everything) — the same fail-safe as the entry/fresh dual-complete gate, now enforced per-iteration.

    Newest-first trim order: the victim must be NEWER than the survivor so it trims first, lands the
    broken save, and the bail-out then protects the older survivor still ahead in the loop."""
    keep_fresh = _render(store, "ep_keepf2", age_s=10)      # newest -> kept (keep_episodes=1)
    victim = _render(store, "ep_dd0000", age_s=8000)        # next-newest -> trims first
    survivor = _render(store, "ep_ee0000", age_s=9000)      # oldest; protected by the bail-out

    real_tr = abn_factory.tombstone_render

    def save_broken_timeline_then_tombstone(path):
        if Path(path) == victim:
            (store / "editor_timelines" / "broken_midtrim.json").write_text("{ not json at all")
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", save_broken_timeline_then_tombstone)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not victim.exists(), "the first victim (trimmed before the broken save) should be gone"
    assert survivor.exists(), (
        "an unparseable timeline landing mid-trim must stop further trimming (protect-everything); "
        "the next render was tombstoned despite the now-incomplete live scan"
    )


# ── SAME-SECOND TOCTOU: the token must flip even when st_mtime does not ────────────────────────────
# Editor saves go through atomic_save (write tmp + os.replace). A save that lands in the same second
# as the live set's last scan — or on a coarse-mtime FS, or with an editor that pins mtimes — leaves
# st_mtime unchanged. An mtime-only token would stay stale, skip the re-scan, and let the trim eat a
# render the just-saved timeline now references. os.replace changes inode + content changes size, so
# the token folds both in and still flips. These pin that.


def test_dir_token_changes_on_same_second_atomic_replace(store):
    """An atomic_save-style replace (write tmp + os.replace) that keeps the SAME mtime second must
    still bump the token, because the inode and size change. mtime alone would miss it."""
    tl = store / "editor_timelines"
    f = tl / "ep_same_sec.json"
    f.write_text('{"old": "ref"}')
    t = 1_700_000_000.0
    os.utime(f, (t, t))
    os.utime(tl, (t, t))

    before = abn_factory._editor_timeline_dir_token()

    tmp = tl / "ep_same_sec.json.tmp"
    tmp.write_text('{"renderCache": {"video": {"path": "/agenticnews-assets/ep_x/renders/episode.mp4"}}}')
    os.utime(tmp, (t, t))
    os.replace(tmp, f)
    # Pin BOTH the file and the dir back to the original second — the worst case the ticket names.
    os.utime(f, (t, t))
    os.utime(tl, (t, t))

    after = abn_factory._editor_timeline_dir_token()
    assert before != after, (
        "token did not change across a same-second atomic replace — an mtime-only token would skip "
        "the live re-scan and let the trim delete a freshly-referenced render (the same-second TOCTOU)"
    )


def test_live_protection_set_catches_same_second_mid_trim_save(store, monkeypatch):
    """End-to-end: a timeline saved mid-trim whose mtime collides with the live set's last scan must
    STILL protect the render it references. Identical to the mtime-bump mid-trim test, except we pin
    the saved timeline AND the dir mtime to a fixed second so st_mtime can't be what signals the
    change — only the inode/size folded into the token can. Proves the same-second window is closed."""
    pinned = 1_700_000_000.0
    # Pin the dir mtime so the token's dir component is stable across the save.
    os.utime(store / "editor_timelines", (pinned, pinned))

    keep_fresh = _render(store, "ep_keepss", age_s=10)        # newest -> kept (keep_episodes=1)
    victim = _render(store, "ep_vss000", age_s=8000)          # next-newest victim -> trims first
    late_protected = _render(store, "ep_lss000", age_s=9000)  # oldest; referenced mid-trim, reached last

    real_tr = abn_factory.tombstone_render

    def save_same_second_then_tombstone(path):
        if Path(path) == victim:
            tl = store / "editor_timelines" / "ep_lss000.json"
            tl.write_text(
                '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, late_protected)
            )
            # Force the save to look like it happened in the SAME second as the pre-trim scan, and
            # re-pin the dir mtime too — so neither the file nor the dir mtime second changes.
            os.utime(tl, (pinned, pinned))
            os.utime(store / "editor_timelines", (pinned, pinned))
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", save_same_second_then_tombstone)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not victim.exists(), "the genuinely-unreferenced render should still be trimmed"
    assert late_protected.exists(), (
        "a render referenced by a SAME-SECOND mid-trim save was tombstoned — the live re-scan token "
        "relied on st_mtime alone and missed the atomic replace (the same-second TOCTOU this hardens)"
    )


# ── HIGH-CONCURRENCY RESIDUAL TOCTOU: the save lands in the gap between THIS render's protection ───
# check and its OWN tombstone, not between two separate renders. The prior mid-trim tests trip the
# save from inside the tombstone of an EARLIER victim (caught at the NEXT iteration's check). But
# when multiple episodes render concurrently, a save can land referencing render N AFTER the loop
# body has already entered for render N (passed the is_file/symlink guard) but BEFORE render N is
# deleted. safe_to_delete() is the single final gate placed immediately before tombstone with
# nothing between, so it re-reads the token one last time and catches that save. These pin it by
# landing the concurrent save from inside is_symlink() — the last loop-body call before the gate.


def test_safe_to_delete_is_false_when_render_becomes_protected(store):
    """Unit gate: safe_to_delete() must return False the instant a live re-scan (triggered by a
    changed editor_timelines token) finds the render now protected — even if the protected set was
    EMPTY when the live set was constructed. This is the atomic final check the trim leans on."""
    render = _render(store, "ep_unit00", age_s=9000)
    live = abn_factory._LiveProtectionSet(set())
    assert live.safe_to_delete(render) is True, "an unreferenced render must be deletable"

    # A concurrent episode's editor saves a timeline referencing this render -> token flips.
    (store / "editor_timelines" / "ep_unit00.json").write_text(
        '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, render)
    )
    assert live.safe_to_delete(render) is False, (
        "safe_to_delete must re-scan on the token change and refuse to delete a now-protected render"
    )


def test_safe_to_delete_is_false_when_live_scan_goes_incomplete(store):
    """Unit gate: a concurrent save that lands an UNPARSEABLE timeline mid-trim flips the live set's
    completeness to False — safe_to_delete() must then return False for ANY render (protect-everything),
    so the caller stops trimming. Pins that completeness is folded into the single final gate."""
    render = _render(store, "ep_unit11", age_s=9000)
    live = abn_factory._LiveProtectionSet(set())
    assert live.safe_to_delete(render) is True

    (store / "editor_timelines" / "broken_concurrent.json").write_text("{ not json at all")
    assert live.safe_to_delete(render) is False, (
        "an incomplete live re-scan must make safe_to_delete refuse (protect-everything)"
    )


def test_purge_disk_protects_render_referenced_by_concurrent_save_in_final_gate_window(store, monkeypatch):
    """HIGH-CONCURRENCY TOCTOU: a concurrent episode's editor saves a timeline referencing the very
    render the trim loop is ABOUT to delete, landing AFTER the loop body entered for that render but
    BEFORE the destructive call. We inject the save from inside is_symlink() (the last loop-body call
    before the final gate) for the target render. safe_to_delete() — the single gate immediately
    before tombstone — must re-read the token and spare it. An unreferenced sibling still trims."""
    victim = _render(store, "ep_freedm", age_s=8000)          # next-newest -> trimmed (no save targets it)
    target = _render(store, "ep_concur", age_s=9000)          # oldest -> reached last; saved into mid-body
    _render(store, "ep_keepc0", age_s=10)                      # newest -> kept (keep_episodes=1)

    real_is_symlink = Path.is_symlink
    # target.is_symlink() is called once during _old_episode_renders() enumeration and again in the
    # trim loop body (right before the final gate). Inject ONLY on the SECOND call, so the save lands
    # AFTER the pre-trim fresh scan — proving it's the FINAL safe_to_delete() gate that catches it, not
    # the entry/fresh scan (which would make this pass for the wrong reason).
    calls = {"n": 0}

    def save_then_symlink(self):
        if self == target:
            calls["n"] += 1
            if calls["n"] == 2:
                (store / "editor_timelines" / "ep_concur.json").write_text(
                    '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, target)
                )
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", save_then_symlink)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not victim.exists(), "the genuinely-unreferenced render should still be trimmed"
    assert target.exists(), (
        "a render referenced by a concurrent save landing between the loop-body guard and the "
        "tombstone was deleted — the final safe_to_delete() gate didn't re-read the token in time"
    )


def test_gc_segments_protects_render_referenced_by_concurrent_save_in_final_gate_window(store, monkeypatch):
    """Twin of the purge_disk test on the async _gc_segments path. 6 renders so the keep-4 slice has
    2 victims; a concurrent save lands referencing the oldest (reached last) from inside its is_symlink
    inspection — the final safe_to_delete() gate must spare it while the other victim still trims."""
    target = _render(store, "ep_gconcr", age_s=9000)          # oldest -> reached last; saved mid-body
    victim = _render(store, "ep_gvictm", age_s=8500)          # second-oldest -> trimmed
    for i, age in enumerate((8000, 7000, 6000, 10)):
        _render(store, f"ep_gfill{i}", age_s=age)

    real_is_symlink = Path.is_symlink
    # Inject on the SECOND target.is_symlink() (enumeration is 1st, trim-loop body is 2nd) so the save
    # lands after the pre-trim fresh scan and only the final safe_to_delete() gate can catch it.
    calls = {"n": 0}

    def save_then_symlink(self):
        if self == target:
            calls["n"] += 1
            if calls["n"] == 2:
                (store / "editor_timelines" / "ep_gconcr.json").write_text(
                    '{"renderCache": {"video": {"path": "%s"}}}' % _agenticnews_ref(store, target)
                )
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", save_then_symlink)
    _gc_no_videos(monkeypatch)
    _force_low_disk(monkeypatch)

    asyncio.run(abn_factory._gc_segments(keep_recent=0))

    assert not victim.exists(), "_gc_segments should still trim the genuinely-unreferenced render"
    assert target.exists(), (
        "_gc_segments deleted a render referenced by a concurrent save landing in the final-gate "
        "window — the safe_to_delete() gate didn't re-read the token before tombstoning"
    )


# ── SAME-SIZE IN-PLACE EDIT TOCTOU: the token-stale hole this ticket explicitly closes ─────────────
# The prior same-second tests cover an atomic os.replace save (new inode, usually new size). The
# residual hole the ticket names is narrower and nastier: a timeline rewritten IN PLACE (same inode)
# whose new render ref happens to be the SAME byte length as the old one, with mtime pinned back to
# the prior second. Then (mtime, inode, size) is byte-for-byte identical and an mtime+inode+size token
# would NOT flip — the live re-scan is skipped and the trim eats the now-referenced render. st_ctime
# is bumped by any write to the inode and cannot be forged via os.utime, so folding it into the token
# catches this at zero extra syscall cost. These pin that the dir contents shifting (different ref)
# without an mtime/inode/size change still flips the token and protects the render.


def _equal_len_ref(store, ep_id):
    """A /agenticnews-assets/ render ref for an episode whose id is a fixed width, so two such refs
    for two same-width episode ids are byte-for-byte the SAME length — letting an in-place edit swap
    one for the other without changing the file size."""
    return "/agenticnews-assets/%s/renders/episode.mp4" % ep_id


def test_dir_token_changes_on_same_size_inplace_edit_with_pinned_mtime(store):
    """An IN-PLACE rewrite (same inode, no os.replace) that swaps the render ref for a different one
    of the SAME byte length, with mtime pinned back, leaves (mtime, inode, size) identical. The token
    must STILL flip — only st_ctime distinguishes it, and st_ctime can't be set by os.utime. An
    mtime+inode+size-only token would miss this and let the trim delete a freshly-referenced render."""
    tl = store / "editor_timelines"
    f = tl / "ep_inplace.json"
    old_ref = _equal_len_ref(store, "ep_oldddd")
    new_ref = _equal_len_ref(store, "ep_newwww")
    assert len(old_ref) == len(new_ref)
    body_old = '{"renderCache": {"video": {"path": "%s"}}}' % old_ref
    body_new = '{"renderCache": {"video": {"path": "%s"}}}' % new_ref
    assert len(body_old) == len(body_new), "test bug: bodies must be equal length to exercise the hole"

    f.write_text(body_old)
    t = 1_700_000_000.0
    os.utime(f, (t, t))
    os.utime(tl, (t, t))
    ino_before = f.stat().st_ino

    before = abn_factory._editor_timeline_dir_token()

    with open(f, "r+") as fh:  # IN PLACE — same inode, same length
        fh.seek(0)
        fh.write(body_new)
        fh.truncate()
    os.utime(f, (t, t))
    os.utime(tl, (t, t))

    st = f.stat()
    assert st.st_ino == ino_before, "test bug: in-place edit must keep the same inode"
    assert st.st_size == len(body_new.encode()), "test bug: rewrite must keep the same size"
    assert st.st_mtime == t, "test bug: mtime must be pinned back to the prior second"

    after = abn_factory._editor_timeline_dir_token()
    assert before != after, (
        "token did not change across a same-size in-place edit with pinned mtime — an "
        "mtime+inode+size token would skip the live re-scan and let the trim delete a render the "
        "rewritten timeline now references (the token-stale TOCTOU this ticket closes)"
    )


def test_safe_to_delete_catches_same_size_inplace_edit(store):
    """Unit gate, the directly-named ticket scenario: a live set built while a timeline referenced
    render A must refuse to delete render B once an IN-PLACE same-size, pinned-mtime rewrite of that
    timeline swaps the ref to B. Only the ctime-folded token can flip here; with an mtime+inode+size
    token safe_to_delete() would still return True and the trim would eat B."""
    render_b = _render(store, "ep_newwww", age_s=9000)  # the render the edit will newly reference
    tl = store / "editor_timelines"
    f = tl / "ep_tl0000.json"
    old_ref = _equal_len_ref(store, "ep_oldddd")        # initially points elsewhere (same width)
    new_ref = _agenticnews_ref(store, render_b)
    body_old = '{"renderCache": {"video": {"path": "%s"}}}' % old_ref
    body_new = '{"renderCache": {"video": {"path": "%s"}}}' % new_ref
    assert len(body_old) == len(body_new), "test bug: bodies must match length to exercise the hole"

    t = 1_700_000_000.0
    f.write_text(body_old)
    os.utime(f, (t, t))
    os.utime(tl, (t, t))

    live = abn_factory._LiveProtectionSet(abn_factory._editor_timeline_asset_paths())
    assert live.safe_to_delete(render_b) is True, "render_b is unreferenced before the edit"

    with open(f, "r+") as fh:  # IN PLACE — same inode, same length
        fh.seek(0)
        fh.write(body_new)
        fh.truncate()
    os.utime(f, (t, t))
    os.utime(tl, (t, t))

    assert live.safe_to_delete(render_b) is False, (
        "safe_to_delete must re-scan and refuse to delete a render that an in-place, same-size, "
        "pinned-mtime timeline edit now references — the token-stale TOCTOU this ticket hardens"
    )


def test_purge_disk_protects_render_referenced_by_same_size_inplace_midtrim_edit(store, monkeypatch):
    """End-to-end: a timeline rewritten IN PLACE mid-trim (same inode, same byte length, mtime pinned
    to the prior second) to reference the render the loop is about to reach must spare that render —
    while the genuinely-unreferenced victim still trims. This is the same-second test's nastier
    cousin: nothing but st_ctime distinguishes the edited timeline, so only the ctime-folded token
    closes it."""
    pinned = 1_700_000_000.0
    os.utime(store / "editor_timelines", (pinned, pinned))

    _render(store, "ep_keepip", age_s=10)                     # newest -> kept (keep_episodes=1)
    victim = _render(store, "ep_vicip0", age_s=8000)          # next-newest victim -> trims first
    late_protected = _render(store, "ep_lateip", age_s=9000)  # oldest; referenced mid-trim, reached last

    tl = store / "editor_timelines" / "ep_tl00ip.json"
    placeholder_ref = _equal_len_ref(store, "ep_nonexs")  # same width as the real ref
    real_ref = _agenticnews_ref(store, late_protected)
    body_old = '{"renderCache": {"video": {"path": "%s"}}}' % placeholder_ref
    body_new = '{"renderCache": {"video": {"path": "%s"}}}' % real_ref
    assert len(body_old) == len(body_new), "test bug: bodies must match length to exercise the hole"
    tl.write_text(body_old)
    os.utime(tl, (pinned, pinned))
    os.utime(store / "editor_timelines", (pinned, pinned))

    real_tr = abn_factory.tombstone_render

    def inplace_edit_then_tombstone(path):
        if Path(path) == victim:
            with open(tl, "r+") as fh:  # IN PLACE — same inode, same length
                fh.seek(0)
                fh.write(body_new)
                fh.truncate()
            os.utime(tl, (pinned, pinned))                 # pin mtime back to the prior second
            os.utime(store / "editor_timelines", (pinned, pinned))
        return real_tr(path)

    monkeypatch.setattr(abn_factory, "tombstone_render", inplace_edit_then_tombstone)
    _force_low_disk(monkeypatch)

    abn_factory.purge_disk(intermediate_age_s=99999, keep_episodes=1, low_disk_gb=999)

    assert not victim.exists(), "the genuinely-unreferenced render should still be trimmed"
    assert late_protected.exists(), (
        "a render referenced by a SAME-SIZE in-place mid-trim edit (mtime/inode/size all pinned) was "
        "tombstoned — the live token relied on mtime+inode+size and missed the ctime-only change "
        "(the token-stale TOCTOU this ticket closes)"
    )


# --- editor-bay write surface: routers/agenticnews.py writes render results / title assets / -----
# timelines / review notes DIRECTLY under ASSETS_DIR/<dir> (NOT via abn_assets.asset_path()), e.g.
#   editor_renders/      _editor_render_dir() / _editor_render_output()  (editor_render.render output)
#   editor_title_assets/ _materialize_editor_title_assets()
#   editor_timelines/    _editor_timeline_store()
#   review_notes.json    _save_review_notes()
# That is BY DESIGN — these are whitelisted in abn_assets.MANAGED_TOP as "legacy/system dirs that
# stay where they are". The hard gate is that the GC must NEVER eat them. These tests pin that the
# render/cache write surface agenticnews.py uses is outside the reapable set, so a future GC change
# that broadened scratch_dirs() would fail here instead of silently reaping live editor renders.


def _editor_surface(store):
    """Reproduce the exact files routers/agenticnews.py writes outside the asset gateway."""
    paths = []
    rd = store / "editor_renders"
    rd.mkdir(parents=True, exist_ok=True)
    p = rd / "ep_cafe01.mp4"  # _editor_render_output(project_id, ".mp4")
    p.write_bytes(b"rendered episode - a cached render result, never reapable")
    paths.append(p)

    td = store / "editor_title_assets"
    td.mkdir(parents=True, exist_ok=True)
    p = td / "editor_title.png"  # _materialize_editor_title_assets()
    p.write_bytes(b"title card png")
    paths.append(p)

    tl = store / "editor_timelines"
    tl.mkdir(parents=True, exist_ok=True)
    p = tl / "ep_cafe01.json"  # _editor_timeline_store() project save
    p.write_bytes(b"{}")
    paths.append(p)

    p = store / "review_notes.json"  # _save_review_notes()
    p.write_bytes(b"{}")
    paths.append(p)
    return paths


def test_editor_bay_write_surface_is_never_reapable(store):
    """The render/cache/timeline files agenticnews.py writes outside the gateway are top-level
    non-episode dirs, so scratch_dirs()/reapable_scratch() never reach them."""
    surface = _editor_surface(store)
    reapable = {f.resolve() for f in abn_assets.reapable_scratch()}
    for p in surface:
        assert p.resolve() not in reapable, (
            f"GC would reap an editor-bay write {p} — agenticnews.py bypasses the gateway for these "
            f"by design, and the GC must never enumerate them as scratch"
        )
    # the three editor SUBDIR writes are recognized as schema-managed top dirs (MANAGED_TOP
    # whitelist); review_notes.json is a bare top-level file (not a managed dir) but is still
    # non-reapable (above) and refused by tombstone() (next test).
    for p in surface:
        if p.parent.resolve() != store.resolve():
            assert abn_assets.is_managed(p), f"{p} should be is_managed() (a MANAGED_TOP dir)"


def test_tombstone_refuses_editor_bay_writes(store):
    """Even if a bad GC path handed tombstone() an editor render/title/timeline, it RAISES — the
    only safe-deletable surface is scratch/ (per-episode) and _scratch/."""
    for p in _editor_surface(store):
        with pytest.raises(abn_assets.AssetPathError):
            abn_assets.tombstone(p)


def test_purge_disk_leaves_editor_bay_writes_intact(store):
    """End-to-end: an aggressive purge_disk run (reap everything, keep 1 episode) must not touch the
    editor render/cache surface, since agenticnews.py persists render results there off-gateway."""
    surface = _editor_surface(store)
    old = time.time() - 99999
    for p in surface:
        os.utime(p, (old, old))
    abn_factory.purge_disk(intermediate_age_s=1, keep_episodes=1, low_disk_gb=0)
    for p in surface:
        assert p.exists(), f"purge_disk reaped an off-gateway editor write {p}"
