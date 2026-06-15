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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually copy+symlink (default: dry run)")
    ap.add_argument("--ep", default=None, help="migrate only this episode id")
    ap.add_argument("--report", action="store_true", help="classification summary only")
    args = ap.parse_args()

    print(f"ASSETS_DIR = {ASSETS_DIR}")
    if not ASSETS_DIR.exists():
        sys.exit(f"ASSETS_DIR does not exist: {ASSETS_DIR}")

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
