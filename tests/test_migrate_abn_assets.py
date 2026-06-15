"""Tests for the one-time ABN asset migration (scripts/migrate_abn_assets.py).

Focus: the back-compat VO symlinks that ``abn_factory._align`` falls back to. The
pre-schema factory wrote VO as the BARE ``{ep_id}_s{N}.wav`` (no ``_voice`` word), which
``services.abn_assets.classify`` would route to ``scratch`` (reapable by the episode GC).
These tests pin two things:

  1. ``plan()`` routes a bare legacy VO file to ``{ep}/audio/s{N}_voice.wav`` (the same path
     the runtime gateway produces), NOT to scratch — so the only copy of original VO and the
     symlink ``_align`` depends on both survive GC.
  2. ``verify_voice_symlinks()`` flags every way the back-compat symlink can be broken
     (missing/real-file/dangling/misrouted-to-scratch), so a completed migration is auditable.
"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def mig(tmp_path, monkeypatch):
    """Re-import the migration script pointed at a throwaway ASSETS_DIR (never touch T9)."""
    monkeypatch.setenv("ABN_ASSETS_DIR", str(tmp_path))
    import services.agenticnews as an
    importlib.reload(an)
    import services.abn_assets as A
    importlib.reload(A)
    import scripts.migrate_abn_assets as M
    importlib.reload(M)
    return M


def _touch(p: Path, data: bytes = b"vo") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_bare_legacy_vo_routes_to_audio_not_scratch(mig):
    # the genuine pre-schema VO filename: bare {ep}_s{N}.wav, no '_voice' token
    flat = _touch(mig.ASSETS_DIR / "ep_648e806a_s0.wav")
    moves = {src.name: dst for src, dst, _ in mig.plan(None)}
    dst = moves[flat.name]
    rel = dst.relative_to(mig.ASSETS_DIR).parts
    assert rel == ("ep_648e806a", "audio", "s0_voice.wav"), rel
    # explicitly NOT scratch (where classify() would have sent it -> GC bait)
    assert "scratch" not in rel


def test_voice_override_pure_no_mkdir(mig):
    # plan() advertises itself as side-effect free; _voice_override must not create dirs
    dst = mig._voice_override("ep_648e806a_s3.wav")
    assert dst is not None and dst.name == "s3_voice.wav" and dst.parent.name == "audio"
    assert not dst.parent.exists()  # no mkdir happened
    assert mig._voice_override("ep_648e806a_s0_card.png") is None  # non-voice -> no override


def test_apply_leaves_audio_copy_and_symlink(mig):
    _touch(mig.ASSETS_DIR / "ep_648e806a_s0.wav", b"narration-bytes")
    mig.apply(mig.plan(None))
    flat = mig.ASSETS_DIR / "ep_648e806a_s0.wav"
    target = mig.ASSETS_DIR / "ep_648e806a" / "audio" / "s0_voice.wav"
    assert target.exists() and target.read_bytes() == b"narration-bytes"
    assert flat.is_symlink() and flat.resolve() == target
    # the audit is clean on a completed migration
    assert mig.verify_voice_symlinks() == []


def test_verify_flags_dangling_symlink(mig):
    # symlink present but its audio target was reaped -> _align fallback silently fails
    flat = mig.ASSETS_DIR / "ep_648e806a_s0.wav"
    gone = mig.ASSETS_DIR / "ep_648e806a" / "audio" / "s0_voice.wav"
    flat.symlink_to(gone)  # target never created
    assert mig.verify_voice_symlinks() == [("ep_648e806a_s0.wav", "dangling")]


def test_verify_flags_real_file_never_linked(mig):
    # migration never ran (or crashed before linking): flat path is still a real file
    _touch(mig.ASSETS_DIR / "ep_648e806a_s0.wav")
    assert mig.verify_voice_symlinks() == [("ep_648e806a_s0.wav", "not-symlink")]


def test_verify_flags_symlink_into_scratch(mig):
    # the pre-fix migration outcome: copy + symlink landed in reapable scratch/, not audio/
    flat = mig.ASSETS_DIR / "ep_648e806a_s0.wav"
    scratch_copy = _touch(mig.ASSETS_DIR / "ep_648e806a" / "scratch" / "s0.wav")
    flat.symlink_to(scratch_copy)
    assert mig.verify_voice_symlinks() == [("ep_648e806a_s0.wav", "not-audio")]


def test_verify_respects_ep_filter(mig):
    # a broken symlink for a DIFFERENT episode is ignored when auditing one ep
    bad = mig.ASSETS_DIR / "ep_aaaaaaaa_s0.wav"
    bad.symlink_to(mig.ASSETS_DIR / "ep_aaaaaaaa" / "audio" / "s0_voice.wav")  # dangling
    _touch(mig.ASSETS_DIR / "ep_648e806a_s0.wav")
    mig.apply(mig.plan("ep_648e806a"))
    assert mig.verify_voice_symlinks("ep_648e806a") == []
    assert mig.verify_voice_symlinks("ep_aaaaaaaa") == [("ep_aaaaaaaa_s0.wav", "dangling")]
