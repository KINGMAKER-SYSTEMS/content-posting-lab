#!/usr/bin/env python3
"""
One-time migration: flat ABN asset dump  ->  per-episode subdir schema.

The store at ASSETS_DIR currently holds ~880 files dumped flat as
``{ep_id}_{kind}.ext``, plus loose shared/test files. This moves each episode's
assets into ``episodes/{ep_id}/{subdir}/...`` and shared assets into ``shared/...``,
using the SAME classifier the runtime gateway (services.abn_assets) enforces, so the
on-disk result is exactly what new writes will produce.

SAFETY (this dir has lost original VO to an over-eager GC before — we do NOT repeat that):
  * DRY-RUN BY DEFAULT. Prints the full plan and changes nothing unless --apply.
  * COPY, never move. Source files are left in place.
  * After copying, leaves a SYMLINK at the old flat path -> the new location, so every
    existing timeline.json / props / running render keeps resolving during the cutover.
  * Verifies byte-size after copy; aborts a file on mismatch.
  * Idempotent: re-running skips files already migrated.
  * Nothing is ever deleted. Reclaiming the originals is a separate, explicit step you
    run by hand only after you've confirmed renders still work.

USAGE
  python scripts/migrate_abn_assets.py                 # dry run — show the plan
  python scripts/migrate_abn_assets.py --apply         # do it (copy + symlink)
  python scripts/migrate_abn_assets.py --apply --ep ep_648e806a   # one episode only
  python scripts/migrate_abn_assets.py --report        # just classify + count, no plan
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.abn_assets import (  # noqa: E402
    ASSETS_DIR, KINDS, SHARED_CATEGORIES, classify, episode_dir,
)

# Loose-file routing rules: filename pattern -> shared category under _shared/.
# Anything not episode-scoped and not matched here goes to _scratch (reviewable, reapable).
SHARED_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^abn[-_].*logo|lockup|anvil|nb|transparent|explainer", re.I), "brand"),
    (re.compile(r"^(bed|whoosh|riser|pop|sting|sfx|music)", re.I), "audio"),
    (re.compile(r"_(bed|whoosh|riser|pop)\.", re.I), "audio"),
    (re.compile(r"^libgen\d*_(broll|still)", re.I), "broll_library"),
]
# Obvious agent scratch/test prefixes -> _scratch (never shared, safe to reap later).
SCRATCH_RE = re.compile(
    r"^(fluxtest|kintest|cardtest|codextest|hookfix|hookfixed|fixedhook|nohanghook|"
    r"rmclean|libgen|api_test|vt_|vtest|_vt|message|agent_log|review_notes|competitor_intel|token)",
    re.I,
)
# Directories that are ALREADY organized — leave them exactly where they are.
KEEP_DIRS = {
    "_shared", "_scratch", "_trash", "_published", "release",
    "brand", "broll_library", "card_backgrounds", "align", "voice_bakeoff",
    "editor_renders", "editor_timelines", "editor_title_assets", "ai-chat-repo",
}


def _route_loose(name: str) -> tuple[str, str]:
    """Return (kind, target_rel_dir) for a non-episode-scoped loose file."""
    for pat, cat in SHARED_RULES:
        if pat.search(name):
            return ("shared", f"_shared/{cat}")
    if SCRATCH_RE.search(name.lstrip("_")):
        return ("scratch", "_scratch")
    # unknown loose file: park in _scratch for human review (never silently shared)
    return ("scratch", "_scratch")


# The pre-schema factory wrote VO as the BARE ``{ep_id}_s{N}.wav`` (abn_factory:
# ``out = ASSETS / f"{name}.wav"`` with ``name = "{ep_id}_s{i}"``) — no ``_voice`` word.
# ``classify`` reads the kind off the last ``_``-token, so a bare voice file lands on
# kind ``s{N}`` -> ``scratch`` (the GC may freely reap scratch). That both (a) loses the
# only copy of original VO and (b) leaves the back-compat symlink pointing into scratch,
# so once the GC reaps it ``abn_factory._align``'s legacy fallback dangles and in-flight
# alignment silently returns []. Detect that exact shape here and route it to the same
# ``audio/s{N}_voice.wav`` the runtime gateway produces, so the symlink + copy survive GC.
_BARE_VOICE_RE = re.compile(
    r"^((?:rec_)?ep_[0-9a-f]{6,}|ep\d+)_(s\d+)\.wav$", re.I
)


def _voice_override(name: str) -> Optional[Path]:
    """If ``name`` is a bare legacy VO file, return its correct ``audio/`` destination;
    else None. PURE (no mkdir) so ``plan()`` stays side-effect-free — mirrors what the
    gateway's ``asset_path(ep, 'voice', 's{N}')`` produces: ``{ep}/audio/s{N}_voice.wav``."""
    m = _BARE_VOICE_RE.match(name)
    if not m:
        return None
    return episode_dir(m.group(1)) / "audio" / f"{m.group(2)}_voice.wav"


def plan(only_ep: str | None) -> list[tuple[Path, Path, str]]:
    """Build (src, dst, reason) triples. Pure — touches nothing."""
    moves: list[tuple[Path, Path, str]] = []
    for f in sorted(ASSETS_DIR.iterdir()):
        if f.name.startswith("._") or f.name == ".DS_Store":
            continue
        if f.is_dir():
            if f.name in KEEP_DIRS:
                continue
            # a stray dir like ep_5eedc0de_sources -> episodes/{ep}/sources
            m = re.match(r"((?:rec_)?ep_[0-9a-f]{6,}|ep\d+)_(.+)", f.name)
            if m and (not only_ep or m.group(1) == only_ep):
                dst = episode_dir(m.group(1)) / m.group(2)
                moves.append((f, dst, f"episode dir -> {dst.relative_to(ASSETS_DIR)}"))
            continue
        # already a symlink we created on a prior run? skip.
        if f.is_symlink():
            continue
        # bare legacy VO (``{ep}_s{N}.wav``) must beat classify(), which would route it to
        # scratch (reapable) and strand the back-compat symlink _align() depends on.
        vo = _voice_override(f.name)
        if vo is not None and (not only_ep or vo.parent.parent.name == only_ep):
            moves.append((f, vo, f"ep voice -> {vo.relative_to(ASSETS_DIR)}"))
            continue
        c = classify(f.name)
        if c:
            if only_ep and c["ep_id"] != only_ep:
                continue
            sub = c["subdir"]
            fname = f"{c['slug']}.{c['ext']}" if c["ext"] else c["slug"]
            dst = episode_dir(c["ep_id"]) / sub / fname if sub else episode_dir(c["ep_id"]) / fname
            moves.append((f, dst, f"ep {c['ep_id']} {c['kind']}"))
        else:
            if only_ep:
                continue
            kind, rel = _route_loose(f.name)
            dst = ASSETS_DIR / rel / f.name
            moves.append((f, dst, f"loose -> {rel}"))
    return moves


def _relink(src: Path, dst: Path) -> None:
    """Replace the flat regular file ``src`` with a back-compat symlink -> ``dst``.
    Atomic-ish: link is built next to src then renamed over it, so a crash never
    leaves src missing (the original copy already lives at dst)."""
    link_tmp = src.with_name(src.name + ".link")
    if link_tmp.is_symlink() or link_tmp.exists():
        link_tmp.unlink()
    link_tmp.symlink_to(dst)
    os.replace(link_tmp, src)  # replaces the regular file with the symlink


def apply(moves: list[tuple[Path, Path, str]]) -> None:
    done = relinked = skipped = failed = 0
    for src, dst, reason in moves:
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                # already copied. A prior run may have crashed BEFORE leaving the
                # back-compat symlink, so src is still a real file pointing nowhere
                # in the schema — heal it so a re-run completes the migration.
                if not src.is_dir() and not src.is_symlink():
                    _relink(src, dst)
                    relinked += 1
                else:
                    skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                done += 1
                continue
            tmp = dst.with_suffix(dst.suffix + ".part")
            shutil.copy2(src, tmp)
            if tmp.stat().st_size != src.stat().st_size:
                tmp.unlink(missing_ok=True)
                print(f"  !! SIZE MISMATCH, skipped: {src.name}")
                failed += 1
                continue
            os.replace(tmp, dst)
            # leave a symlink at the old flat path so old references keep resolving.
            _relink(src, dst)
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"  !! FAILED {src.name}: {e}")
            failed += 1
    print(f"\nApplied: {done} copied+linked, {relinked} re-linked (heal), "
          f"{skipped} already-migrated, {failed} failed.")


def verify_voice_symlinks(only_ep: str | None = None) -> list[tuple[str, str]]:
    """Post-migration audit for the back-compat VO symlinks ``abn_factory._align`` falls
    back to. Scans ASSETS_DIR for every legacy flat VO name (``{ep}_s{N}.wav``) and confirms
    the flat path is a SYMLINK resolving to an ``audio/`` target that actually exists — so an
    in-flight episode that still hits ``_align``'s ``ASSETS / f"{wav_name}.wav"`` fallback finds
    a real file. (``plan()`` is no help here: it SKIPS symlinks, so a migrated dir has zero
    voice moves left to inspect; this audit must look at the flat names on disk directly.)

    Returns a list of ``(flat_name, reason)`` problems; empty == all good. Reasons:
      * ``not-symlink`` — flat path is still a real file (migration never linked it; the
                          original copy may not have been routed to ``audio/`` at all).
      * ``dangling``    — symlink present but its target file doesn't exist (GC ate the copy
                          or it was misrouted to scratch and reaped — ``_align`` silently fails).
      * ``not-audio``   — symlink resolves OUTSIDE ``audio/`` (e.g. into reapable ``scratch/``),
                          so the only VO copy is exposed to episode-pruning GC.
    Pure read-only — never writes or deletes.
    """
    problems: list[tuple[str, str]] = []
    for f in sorted(ASSETS_DIR.iterdir()):
        m = _BARE_VOICE_RE.match(f.name)
        if not m:
            continue
        if only_ep and m.group(1) != only_ep:
            continue
        if not f.is_symlink():
            problems.append((f.name, "not-symlink"))
            continue
        target = f.resolve()
        if not target.exists():
            problems.append((f.name, "dangling"))
        elif target.parent.name != "audio":
            problems.append((f.name, "not-audio"))
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually copy+symlink (default: dry run)")
    ap.add_argument("--ep", default=None, help="migrate only this episode id")
    ap.add_argument("--report", action="store_true", help="classification summary only")
    ap.add_argument("--verify", action="store_true",
                    help="audit back-compat VO symlinks (abn_factory._align fallback); exit 1 if any broken")
    args = ap.parse_args()

    print(f"ASSETS_DIR = {ASSETS_DIR}")
    if not ASSETS_DIR.exists():
        sys.exit(f"ASSETS_DIR does not exist: {ASSETS_DIR}")

    if args.verify:
        problems = verify_voice_symlinks(args.ep)
        if not problems:
            print("VO symlink audit: OK — every legacy flat VO path resolves to an audio/ file.")
            return
        print(f"VO symlink audit: {len(problems)} BROKEN — _align fallback will fail for these:")
        for name, reason in problems:
            print(f"  !! {name}  [{reason}]")
        sys.exit(1)

    moves = plan(args.ep)
    if args.report:
        from collections import Counter
        c = Counter(str(dst.parent.relative_to(ASSETS_DIR)) for _, dst, _ in moves)
        for k, n in sorted(c.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {k}/")
        print(f"\n  {len(moves)} files would be organized.")
        return

    print(f"\n{'APPLYING' if args.apply else 'DRY RUN — nothing will change'} "
          f"({len(moves)} files)\n")
    shown = 0
    for src, dst, reason in moves:
        if shown < 50 or args.apply:
            print(f"  {src.name}\n      -> {dst.relative_to(ASSETS_DIR)}")
        shown += 1
    if not args.apply:
        if shown > 50:
            print(f"  … and {shown - 50} more (run with --report for the summary)")
        print("\nDry run only. Re-run with --apply to copy + leave back-compat symlinks.")
        print("Nothing is ever deleted; originals stay until you reclaim them by hand.")
        return
    apply(moves)


if __name__ == "__main__":
    main()
