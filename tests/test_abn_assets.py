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


# --- episode_singleton_path: the READ resolver that retires the flat episode paths ----
# The factory WRITES the timeline/render through asset_path (schema {ep}/timeline.json,
# {ep}/renders/episode.mp4). The editor-bay READS used to rebuild flat {ep}_timeline.json /
# {ep}_episode.mp4 strings, resolving only via the migration's back-compat symlinks. This
# resolver returns the canonical schema path so reads no longer depend on the symlinks.

def test_episode_singleton_path_matches_factory_write_path(gw):
    # the resolver must point at EXACTLY where asset_path() (the factory's writer) lands
    assert gw.episode_singleton_path("ep_648e806a", "timeline") == gw.asset_path("ep_648e806a", "timeline")
    assert gw.episode_singleton_path("ep_648e806a", "episode") == gw.asset_path("ep_648e806a", "episode")


def test_episode_singleton_path_is_read_safe_no_mkdir(gw):
    # a GET must not create the episode dir as a side effect (asset_path does; this must not)
    p = gw.episode_singleton_path("ep_deadbeef", "timeline")
    assert p == gw.ASSETS_DIR / "ep_deadbeef" / "timeline.json"
    assert not p.parent.exists(), "read resolver must not mkdir the episode dir"


def test_episode_singleton_path_returns_none_for_non_episode_id(gw):
    # a bare/ad-hoc id (no ep_ prefix) -> None, so the caller keeps its flat fallback
    assert gw.episode_singleton_path("clip", "timeline") is None
    assert gw.episode_singleton_path("vid_abc123", "episode") is None


def test_episode_singleton_path_rejects_non_singleton_kind(gw):
    with pytest.raises(gw.AssetPathError):
        gw.episode_singleton_path("ep_648e806a", "card")


# --- scratch_path: the sanctioned replacement for `.with_name()` on scratch ------
# abn_factory writes throwaway .tape / snippet.py / intermediate .html files. These
# used to be built as `asset_path_from_slug(slug,"scratch").with_name(name)`, which
# rebuilds the basename OUTSIDE the gateway and skips slug validation. scratch_path
# re-validates the full filename so an off-schema name is rejected at the write call.

def test_scratch_path_layout(gw):
    p = gw.scratch_path("ep_648e806a_s0", "ep_648e806a_s0.tape")
    assert p.name == "ep_648e806a_s0.tape"
    assert p.parent.name == "scratch"
    assert p.parent.parent.name == "ep_648e806a"      # episode dir directly under ASSETS_DIR
    assert p.parent.parent.parent == gw.ASSETS_DIR
    assert p.parent.is_dir()                            # gateway creates the dir


@pytest.mark.parametrize("flat,fname", [
    ("ep_648e806a_s0", "ep_648e806a_s0_kinetic.html"),
    ("ep_648e806a_s3", "ep_648e806a_s3_snippet.py"),
    ("ep_648e806a_s3", "ep_648e806a_s3_real.tape"),
])
def test_scratch_path_real_factory_names(gw, flat, fname):
    # the exact names abn_factory builds at its four scratch call-sites all validate
    p = gw.scratch_path(flat, fname)
    assert p.name == fname and p.parent.name == "scratch"


@pytest.mark.parametrize("payload", [
    "../../etc/passwd",          # path traversal
    "..",                        # escape scratch/ via parent dir
    "foo/bar.tape",              # embedded slash
    "foo\\bar.tape",             # windows-style separator
    ".hidden.tape",             # leading dot
    "a\x00b.tape",               # null byte
    "",                          # empty
])
def test_scratch_path_rejects_off_schema_name(gw, payload):
    # this is the bug the ticket closes: a caller-controlled name that .with_name()
    # would have silently accepted MUST raise at the gateway instead.
    with pytest.raises(gw.AssetPathError):
        gw.scratch_path("ep_648e806a_s0", payload)


def test_scratch_path_requires_episode_prefix(gw):
    with pytest.raises(gw.AssetPathError):
        gw.scratch_path("just_a_name", "x.tape")   # no ep_id on the front


# --- GC reapable surface: scratch_dirs / reapable_scratch ------------------------
# These are the ONLY paths the garbage collector may delete. The gateway-level
# contract (independent of abn_factory's GC loop) is that the reapable set is exactly
# the regular files under per-episode {ep}/scratch/ + cross-episode _scratch/, with
# directories and (critically) symlinks excluded — a symlink under scratch/ can point
# INTO the schema, and following it would orphan a real keeper.

def _scratch(gw, ep_id, name, body=b"intermediate"):
    d = gw.episode_dir(ep_id) / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(body)
    return f


def test_scratch_dirs_collects_per_episode_and_cross_episode(gw):
    a = _scratch(gw, "ep_a111111", "s0.bin")
    b = _scratch(gw, "ep_b222222", "s1.bin")
    cross = gw.ASSETS_DIR / "_scratch"
    cross.mkdir()
    (cross / "fluxtest.png").write_bytes(b"probe")
    dirs = {d.resolve() for d in gw.scratch_dirs()}
    assert a.parent.resolve() in dirs
    assert b.parent.resolve() in dirs
    assert cross.resolve() in dirs
    # a non-episode top dir with a scratch/ child is NOT a reapable root
    bogus = gw.ASSETS_DIR / "_shared" / "scratch"
    bogus.mkdir(parents=True)
    assert bogus.resolve() not in {d.resolve() for d in gw.scratch_dirs()}


def test_reapable_scratch_recurses_and_includes_only_regular_files(gw):
    top = _scratch(gw, "ep_a111111", "top.bin")
    nested = gw.episode_dir("ep_a111111") / "scratch" / "sub"
    nested.mkdir()
    deep = nested / "deep.bin"
    deep.write_bytes(b"y")
    cross = gw.ASSETS_DIR / "_scratch"
    cross.mkdir()
    cf = cross / "fluxtest.png"
    cf.write_bytes(b"probe")
    got = {p.resolve() for p in gw.reapable_scratch()}
    assert got == {top.resolve(), deep.resolve(), cf.resolve()}
    # the recursed-into subdir itself is never a reap candidate (only its files)
    assert nested.resolve() not in got


def test_reapable_scratch_excludes_symlinks_even_dangling(gw):
    """A symlink under scratch/ is excluded whether it resolves or dangles. A live symlink
    can point INTO the schema (renders/audio); reaping it would orphan the real keeper. A
    broken symlink makes is_file() raise on some platforms — that OSError must be swallowed,
    not crash the whole GC enumeration."""
    real = _scratch(gw, "ep_a111111", "real.bin")
    keeper_dir = gw.episode_dir("ep_a111111") / "renders"
    keeper_dir.mkdir(parents=True)
    keeper = keeper_dir / "episode.mp4"
    keeper.write_bytes(b"the real render")
    sd = real.parent
    (sd / "live.mp4").symlink_to(keeper)        # symlink INTO the schema
    (sd / "dead.mp4").symlink_to(sd / "gone.bin")  # dangling symlink
    got = {p.resolve() for p in gw.reapable_scratch()}
    assert got == {real.resolve()}, "only the regular file is reapable; both symlinks excluded"
    assert keeper.exists(), "enumeration must never follow a symlink to a keeper"


def test_reapable_scratch_empty_when_no_scratch_roots(gw):
    # episode exists but has no scratch/ dir, and there's no _scratch/ — nothing reapable
    gw.asset_path("ep_a111111", "card", "s0")  # creates css/ only
    assert gw.reapable_scratch() == []


# --- tombstone(): the safe-delete primitive (move to _trash/, never unlink) ------

def test_tombstone_moves_scratch_file_to_trash_and_returns_size(gw):
    f = _scratch(gw, "ep_a111111", "s0_raw.wav", body=b"data!!")
    size = gw.tombstone(f)
    assert size == 6
    assert not f.exists()
    dest = gw.ASSETS_DIR / "_trash" / "ep_a111111" / "scratch" / "s0_raw.wav"
    assert dest.exists() and dest.read_bytes() == b"data!!"


def test_tombstone_reaps_cross_episode_scratch(gw):
    cross = gw.ASSETS_DIR / "_scratch"
    cross.mkdir()
    f = cross / "fluxtest_probe.png"
    f.write_bytes(b"probe")
    gw.tombstone(f)
    assert not f.exists()
    assert (gw.ASSETS_DIR / "_trash" / "_scratch" / "fluxtest_probe.png").exists()


def test_tombstone_collision_suffixes_instead_of_clobbering(gw):
    f1 = _scratch(gw, "ep_a111111", "s0_raw.wav", body=b"first")
    gw.tombstone(f1)
    f2 = _scratch(gw, "ep_a111111", "s0_raw.wav", body=b"second")
    gw.tombstone(f2)
    trashed = list((gw.ASSETS_DIR / "_trash" / "ep_a111111" / "scratch").glob("s0_raw*"))
    assert len(trashed) == 2, "second reap of same rel path must suffix, not overwrite"
    bodies = {t.read_bytes() for t in trashed}
    assert bodies == {b"first", b"second"}


def test_tombstone_refuses_symlink_under_scratch(gw):
    """The exact origaudio-loss class hazard: a symlink under scratch/ pointing at a real
    keeper must RAISE, not be tombstoned (which would move/orphan the keeper reference)."""
    keeper_dir = gw.episode_dir("ep_a111111") / "renders"
    keeper_dir.mkdir(parents=True)
    keeper = keeper_dir / "episode.mp4"
    keeper.write_bytes(b"render")
    sd = gw.episode_dir("ep_a111111") / "scratch"
    sd.mkdir(parents=True)
    link = sd / "link.mp4"
    link.symlink_to(keeper)
    with pytest.raises(gw.AssetPathError):
        gw.tombstone(link)
    assert link.is_symlink() and keeper.exists()


def test_tombstone_refuses_directory(gw):
    sd = gw.episode_dir("ep_a111111") / "scratch"
    sd.mkdir(parents=True)
    with pytest.raises(gw.AssetPathError):
        gw.tombstone(sd)
    assert sd.is_dir()


def test_tombstone_refuses_non_scratch_schema_files(gw):
    """A render, an audio VO, and a footage capture all live under the schema but OUTSIDE a
    reapable scratch root — tombstone() must RAISE so a buggy GC call can't turn into
    whole-episode loss."""
    render = gw.asset_path("ep_a111111", "episode")
    render.write_bytes(b"render")
    vo = gw.asset_path("ep_a111111", "voice", "s0")
    vo.write_bytes(b"vo")
    ui = gw.asset_path("ep_a111111", "ui", "s3")
    ui.write_bytes(b"capture")
    for keeper in (render, vo, ui):
        with pytest.raises(gw.AssetPathError):
            gw.tombstone(keeper)
        assert keeper.exists()


def test_tombstone_refuses_off_store_path(gw, tmp_path):
    outside = tmp_path.parent / "elsewhere.bin"
    outside.write_bytes(b"x")
    try:
        with pytest.raises(gw.AssetPathError):
            gw.tombstone(outside)
        assert outside.exists()
    finally:
        outside.unlink()


def test_tombstone_refuses_missing_path(gw):
    """Race window: a scratch file the caller already enumerated can vanish before the reap
    (another GC pass, a crash, a manual rm). A path that is no longer a regular file must
    RAISE AssetPathError, not silently 'succeed' or corrupt _trash/."""
    sd = gw.episode_dir("ep_a111111") / "scratch"
    sd.mkdir(parents=True)
    with pytest.raises(gw.AssetPathError):
        gw.tombstone(sd / "never_existed.bin")


# --- migration <-> gateway <-> read-resolver: the flat-path retirement contract -----
# The migration moves flat {ep}_timeline.json / {ep}_episode.mp4 into the schema and leaves
# a back-compat symlink. The point of this ticket: those flat reads can now be retired because
# episode_singleton_path() resolves to EXACTLY where the migration put the file. This pins that
# the producer (migration) and the consumer (read-resolver) agree on the on-disk location.

def test_migration_lands_episode_singletons_where_read_resolver_looks(gw):
    import scripts.migrate_abn_assets as M
    importlib.reload(M)  # rebind the script's captured ASSETS_DIR to gw's temp dir

    ep = "ep_648e806a"
    flat_tl = gw.ASSETS_DIR / f"{ep}_timeline.json"
    flat_ep = gw.ASSETS_DIR / f"{ep}_episode.mp4"
    flat_tl.write_text('{"segments": [1]}')
    flat_ep.write_bytes(b"mp4data")

    M.apply(M.plan(None))

    # the migration's destination is EXACTLY what the read-resolver returns
    assert gw.episode_singleton_path(ep, "timeline") == gw.ASSETS_DIR / ep / "timeline.json"
    assert gw.episode_singleton_path(ep, "episode") == gw.ASSETS_DIR / ep / "renders" / "episode.mp4"
    assert gw.episode_singleton_path(ep, "timeline").read_text() == '{"segments": [1]}'
    assert gw.episode_singleton_path(ep, "episode").exists()
    # the flat name survives only as a back-compat symlink into the schema (on its way out)
    assert flat_tl.is_symlink() and flat_ep.is_symlink()
    assert flat_tl.resolve() == gw.episode_singleton_path(ep, "timeline").resolve()
