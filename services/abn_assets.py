"""
ABN asset-path GATEWAY — the one sanctioned way to get a write path under the
episode asset store.

WHY THIS EXISTS
---------------
Every ABN asset used to be written FLAT as ``ASSETS / f"{ep_id}_{kind}.ext"`` into a
single 879-file directory, with episode identity carried only by the filename prefix.
That made the dir unbrowsable, made the GC prune-by-glob destructive (it has deleted
original VO — ``*_raw.wav`` matched an intermediate pattern), and gave subagents no
schema to follow. This module replaces hand-built paths with a validated, per-episode
layout that ANY agent (claude / pi / codex / ocean) is forced through — because the
*write path itself* is rejected at runtime when it's off-schema. System prompts only
go so far; the filesystem call is the real enforcement point.

THE SCHEMA (confirmed by John 2026-06-13) — grouped by LAYER SOURCE, matching
``openshot_bridge.SOURCE_TYPES`` (remotion / webscroll / css / broll / still / vo / bed),
because an ABN episode is heterogeneous source layers composited by OpenShot:

    {ep_id}/                      e.g. ep_648e806a   (directly under ASSETS_DIR)
        footage/   webscroll Playwright captures (ui.mp4)            -> source webscroll
        css/       rasterized seekable-HTML cards + kinetic clips    -> source css/still
        remotion/  terminal / code-graphics shots                   -> source remotion
        broll/     ambient plates, code demos, source stills        -> source broll/still
        audio/     vo wavs, alignment json, ducked beds             -> source vo/bed
        renders/   episode.mp4, thumb, assembled cuts               -> output
        scratch/   per-episode intermediates the GC may freely reap
        timeline.json     render props (GROUND TRUTH for QA)
        manifest.json     asset index (written by callers as needed)
    _shared/       cross-episode: logo lockups, bed tracks, broll_library, card_backgrounds
    _published/    shipped finals, per-episode subdir (already in use)
    _scratch/      cross-episode test/scratch (fluxtest_*, kintest_* — reapable)
    _trash/        tombstone for safe-deletes (GC moves here instead of unlink)

PUBLIC API
----------
    asset_path(ep_id, kind, slug=None, ext=...) -> Path   # validated, dir created
    asset_url(ep_id, kind, slug=None, ext=...)  -> str     # the /agenticnews-assets/ URL
    shared_path(category, name) -> Path
    shared_url(category, name)  -> str
    published_path(ep_id, name) -> Path
    episode_dir(ep_id) -> Path
    is_managed(path) -> bool                                # path lives under the schema
    classify(path) -> dict|None                             # reverse-map a flat legacy name
    KINDS                                                    # the closed kind vocabulary

The URL form is what goes into timelines/props. ``openshot_bridge._resolve_asset_src``
already maps ``/agenticnews-assets/<rel>`` -> ``asset_root / <rel>`` for ANY subpath,
so ``/agenticnews-assets/ep_x/css/s0.png`` resolves with zero bridge changes (verified).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from services.agenticnews import ASSETS_DIR

# URL prefix the static mount + openshot_bridge already understand.
URL_PREFIX = "/agenticnews-assets/"

# ---- closed vocabularies -------------------------------------------------------

# kind -> (subdir, default extension). This is the WHOLE sanctioned asset vocabulary,
# grouped by LAYER SOURCE (footage/css/remotion/broll/audio/renders) — NOT media type.
# Adding a new asset type = add it here (and nowhere else). An unknown kind RAISES.
KINDS: dict[str, tuple[str, str]] = {
    # --- webscroll layer (real browser footage, Playwright) ---
    "ui":        ("footage", "mp4"),   # live UI / page capture (the moat)
    "footage":   ("footage", "mp4"),
    "webscroll": ("footage", "mp4"),
    # --- css layer (rasterized seekable-HTML cards + kinetic type) ---
    "card":      ("css", "png"),
    "hook":      ("css", "png"),
    "number":    ("css", "png"),
    "quote":     ("css", "png"),
    "vs":        ("css", "png"),
    "diagram":   ("css", "png"),
    "kinetic":   ("css", "mp4"),
    "css":       ("css", "mp4"),
    # --- remotion layer (terminal / code-graphics shots) ---
    "remotion":  ("remotion", "mp4"),
    "terminal":  ("remotion", "mp4"),
    # --- broll layer (ambient plates, code demos, source stills) ---
    "broll":     ("broll", "mp4"),
    "demo":      ("broll", "mp4"),
    "still":     ("broll", "png"),
    "src":       ("broll", "png"),     # source screenshot still
    # --- audio (vo + bed + alignment) ---
    "voice":     ("audio", "wav"),
    "vo":        ("audio", "wav"),
    "align":     ("audio", "json"),
    "bed":       ("audio", "mp3"),     # episode-local ducked bed; library beds live in _shared/audio
    # --- renders (output) ---
    "episode":   ("renders", "mp4"),
    "assembled": ("renders", "mp4"),
    "thumb":     ("renders", "png"),
    "thumb_bg":  ("renders", "png"),
    # --- episode-root singleton ---
    "timeline":  (".", "json"),        # -> {ep_id}/timeline.json
    "manifest":  (".", "json"),        # -> {ep_id}/manifest.json
    # --- intermediates the GC may freely reap ---
    "scratch":   ("scratch", ""),
}

# Shared (cross-episode) categories. NEVER GC'd by episode pruning. Live under _shared/.
SHARED_CATEGORIES = {"brand", "audio", "broll_library", "card_backgrounds"}

# An ep_id is `ep_<hex>` (factory uuid prefix) or `rec_ep_<hex>` (recreate). We also
# tolerate the legacy short forms `ep01`, `ep2`… so old call-sites can migrate.
_EP_RE = re.compile(r"^(?:rec_)?ep_[0-9a-f]{6,}$|^ep\d+$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Top-level dirs that are part of the schema (used by is_managed + the hook guard).
MANAGED_TOP = {
    "_shared", "_published", "_scratch", "_trash",
    # legacy/system dirs that are already organized and stay where they are
    "editor_renders", "editor_timelines", "editor_title_assets", "align",
    "card_backgrounds", "broll_library", "release", "ai-chat-repo", "voice_bakeoff",
}


class AssetPathError(ValueError):
    """Raised when an off-schema asset path is requested. The message tells the
    caller exactly how to fix it — agents read this and self-correct."""


def _validate_ep(ep_id: str) -> str:
    ep_id = str(ep_id).strip()
    if not _EP_RE.match(ep_id):
        raise AssetPathError(
            f"bad episode id {ep_id!r}. Expected 'ep_<hex>' / 'rec_ep_<hex>' "
            f"(or legacy 'ep<N>'). Episode assets MUST be scoped to a real episode id."
        )
    return ep_id


def episode_dir(ep_id: str) -> Path:
    """Absolute dir for one episode: ASSETS_DIR/{ep_id}."""
    return ASSETS_DIR / _validate_ep(ep_id)


def asset_path(
    ep_id: str,
    kind: str,
    slug: Optional[str] = None,
    *,
    ext: Optional[str] = None,
) -> Path:
    """The ONE sanctioned way to get a write path for an episode asset.

    Validates ep_id + kind against the closed schema, creates the target subdir,
    and returns the absolute Path. RAISES ``AssetPathError`` on anything off-schema
    so no agent — regardless of model — can write a stray file through this gateway.

        asset_path("ep_648e806a", "card", "s0")    -> .../ep_648e806a/css/s0_card.png
        asset_path("ep_648e806a", "ui", "s3")      -> .../ep_648e806a/footage/s3_ui.mp4
        asset_path("ep_648e806a", "voice", "s3")   -> .../ep_648e806a/audio/s3_voice.wav
        asset_path("ep_648e806a", "timeline")      -> .../ep_648e806a/timeline.json
        asset_path("ep_648e806a", "episode")       -> .../ep_648e806a/renders/episode.mp4
    """
    ep_id = _validate_ep(ep_id)
    kind = str(kind).strip().lower()
    if kind not in KINDS:
        raise AssetPathError(
            f"unknown asset kind {kind!r}. Allowed: {', '.join(sorted(KINDS))}. "
            f"To add a new asset type, register it in services/abn_assets.KINDS — "
            f"do NOT hand-build a path."
        )
    subdir, default_ext = KINDS[kind]
    ext = (ext if ext is not None else default_ext).lstrip(".")
    # ext is caller-controlled and gets concatenated straight into the basename, so an
    # ext like 'png/../../evil' would carry a path separator + traversal and land the asset
    # OUTSIDE the episode dir (or the whole asset store) — the exact off-schema escape this
    # gateway exists to close. Allow only a plain alphanumeric extension (or empty for scratch).
    if ext and not re.fullmatch(r"[A-Za-z0-9]+", ext):
        raise AssetPathError(
            f"bad extension {ext!r}. Use a plain extension like 'png'/'mp4'/'json' "
            f"(alphanumerics only; no dots, slashes, or path traversal)."
        )

    # singleton episode-root files (timeline.json, manifest.json): name is the kind itself
    if subdir == ".":
        target = episode_dir(ep_id) / f"{kind}.{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    # everything else lives in a typed subdir. slug names the specific shot/segment.
    # 'episode' is special: a renders/ singleton named episode.mp4.
    if kind in ("episode",) and slug is None:
        d = episode_dir(ep_id) / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"episode.{ext}"

    if slug is None:
        slug = kind
    slug = str(slug).strip()
    if not _SLUG_RE.match(slug):
        raise AssetPathError(
            f"bad slug {slug!r}. Use segment/shot ids like 's0', 's3_hook', 'thumb1' "
            f"(alphanumerics, dot, dash, underscore; no slashes, no leading dot)."
        )
    # don't double-stamp the kind into the name unless the slug already implies it
    name = slug if (slug == kind or slug.endswith(f"_{kind}")) else f"{slug}_{kind}"
    fname = f"{name}.{ext}" if ext else name
    d = episode_dir(ep_id) / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d / fname


# Pull the episode id off the FRONT of a flat factory slug. The factory builds
# per-segment slugs as ``{ep_id}_s{N}`` (abn_factory: ``sid = f"{ep_id}_s{i}"``) and
# episode-level slugs as the bare ``{ep_id}``. This recovers (ep_id, remainder) so a
# call site that only has the flat slug can still route through the gateway WITHOUT
# threading ep_id through every signature. Remainder is the per-segment tail ('s0',
# 's3') or '' for an episode-level asset.
_EP_PREFIX_RE = re.compile(r"^((?:rec_)?ep_[0-9a-f]{6,}|ep\d+)(?:_(.+))?$")


def split_slug(flat_slug: str) -> tuple[str, str]:
    """('ep_648e806a_s0') -> ('ep_648e806a', 's0');  ('ep_648e806a') -> ('ep_648e806a', '').
    Raises AssetPathError if no episode id is on the front (so a stray name can't sneak
    through as if it were episode-scoped)."""
    m = _EP_PREFIX_RE.match(str(flat_slug).strip())
    if not m:
        raise AssetPathError(
            f"slug {flat_slug!r} has no episode id prefix. Factory slugs must start with "
            f"'ep_<hex>' (e.g. 'ep_648e806a_s0'). Pass an explicit ep_id to asset_path() "
            f"instead if this isn't episode-scoped."
        )
    return m.group(1), (m.group(2) or "")


def asset_path_from_slug(flat_slug: str, kind: str, *, ext: Optional[str] = None) -> Path:
    """Gateway entry for call sites that only hold a flat ``{ep_id}_s{N}`` slug.
    Splits the ep_id off the front and routes through asset_path(). The per-segment
    remainder ('s0') becomes the asset slug; an episode-level slug (bare ep_id) yields
    the kind's singleton/default name.

        asset_path_from_slug('ep_648e806a_s0', 'card')   -> .../ep_648e806a/css/s0_card.png
        asset_path_from_slug('ep_648e806a',    'thumb')  -> .../ep_648e806a/renders/thumb.png
    """
    ep_id, rest = split_slug(flat_slug)
    return asset_path(ep_id, kind, rest or None, ext=ext)


def asset_url_from_slug(flat_slug: str, kind: str, *, ext: Optional[str] = None) -> str:
    """URL twin of asset_path_from_slug — what goes into timelines/props."""
    return _rel_url(asset_path_from_slug(flat_slug, kind, ext=ext))


def scratch_path(flat_slug: str, filename: str) -> Path:
    """Gateway path for a SPECIFICALLY-NAMED throwaway intermediate under
    ``{ep_id}/scratch/`` (a .tape, a snippet .py, an intermediate .html).

    Use this instead of ``asset_path_from_slug(slug, "scratch").with_name(...)``:
    ``Path.with_name`` rebuilds the filename OUTSIDE the gateway, re-introducing the
    exact unchecked-name hole this module exists to close (line 14: the write path
    itself must be rejected at runtime when off-schema). ``filename`` is the full
    basename and is validated through the same ``_SLUG_RE`` as every other slug, so
    ``'..'``, ``'../evil'``, leading dots, slashes and null bytes all RAISE here.

        scratch_path('ep_648e806a_s0', 'ep_648e806a_s0.tape')
            -> .../ep_648e806a/scratch/ep_648e806a_s0.tape
    """
    ep_id, _rest = split_slug(flat_slug)
    name = str(filename).strip()
    if not _SLUG_RE.match(name) or "/" in name or "\\" in name:
        raise AssetPathError(
            f"bad scratch filename {name!r}. Use a flat basename like "
            f"'{flat_slug}.tape' (alphanumerics, dot, dash, underscore; no slashes, "
            f"no leading dot, no traversal). Do NOT hand-build a path with .with_name()."
        )
    d = episode_dir(ep_id) / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def adhoc_scratch_path(name: str, ext: str) -> Path:
    """Gateway path for a genuinely UNSCOPED ad-hoc/test render — one with no episode to
    scope it to (a bare default like ``vo``/``card``/``clip``, or a video id with no ``ep_``
    prefix). Lands it under the cross-episode ``_scratch/`` dir instead of the flat
    ASSETS_DIR ROOT.

    This closes the three fallback holes in routers/agenticnews.py (the ``except
    AssetPathError`` clauses that built ``ASSETS_DIR / f"{name}.wav"`` etc.): a flat name at
    the store root collides across calls AND is indistinguishable to the GC from a keeper —
    the exact glob-GC hazard the schema exists to kill. ``_scratch/`` is already the
    cross-episode reapable surface (``scratch_dirs()`` walks it, ``tombstone()`` accepts it),
    so these intermediates land where the GC can safely reap them by location, not by guessing
    from the filename.

    ``name`` is validated through the same ``_SLUG_RE`` as every other slug, so traversal,
    slashes, leading dots and null bytes RAISE here rather than escaping the store.

        adhoc_scratch_path("vo", "wav")    -> .../_scratch/vo.wav
        adhoc_scratch_path("card", "png")  -> .../_scratch/card.png
    """
    name = str(name).strip()
    if not _SLUG_RE.match(name) or "/" in name or "\\" in name:
        raise AssetPathError(
            f"bad ad-hoc name {name!r}. Use a flat basename (alphanumerics, dot, dash, "
            f"underscore; no slashes, no leading dot, no traversal)."
        )
    ext = str(ext).lstrip(".")
    if ext and not re.fullmatch(r"[A-Za-z0-9]+", ext):
        raise AssetPathError(
            f"bad extension {ext!r}. Use a plain extension like 'png'/'mp4'/'wav'."
        )
    d = ASSETS_DIR / "_scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d / (f"{name}.{ext}" if ext else name)


def _rel_url(p: Path) -> str:
    return URL_PREFIX + str(p.relative_to(ASSETS_DIR))


def asset_url(ep_id: str, kind: str, slug: Optional[str] = None, *, ext: Optional[str] = None) -> str:
    """The /agenticnews-assets/ URL for an episode asset — what goes into timelines/props.
    Side-effect-creates the dir (same as asset_path) so the URL is always backed by a real dir."""
    return _rel_url(asset_path(ep_id, kind, slug, ext=ext))


def episode_singleton_path(ep_id: str, kind: str) -> Optional[Path]:
    """READ resolver for an episode-level singleton (``timeline`` / ``episode`` / ``manifest``).

    Returns the canonical SCHEMA path the factory writes to — ``{ep_id}/timeline.json`` or
    ``{ep_id}/renders/episode.mp4`` — so a GET reads the same file the producer wrote, NOT
    the flat ``{ep_id}_timeline.json`` legacy name (which now only exists as a back-compat
    symlink the migration left, on its way out).

    Unlike :func:`asset_path` this NEVER creates a directory (it's a read) and NEVER raises:
    a non-episode id (a bare ``clip`` / a video id with no ``ep_`` prefix) returns ``None`` so
    the caller can keep its own flat fallback for genuinely-unscoped ad-hoc renders. This is
    the last call-site needed to retire the flat episode-read paths in routers/agenticnews.py.
    """
    if kind not in ("timeline", "episode", "manifest"):
        raise AssetPathError(
            f"episode_singleton_path only resolves episode-level singletons "
            f"(timeline/episode/manifest), not {kind!r}."
        )
    try:
        ep_id = _validate_ep(ep_id)
    except AssetPathError:
        return None
    subdir, default_ext = KINDS[kind]
    if subdir == ".":
        return ASSETS_DIR / ep_id / f"{kind}.{default_ext}"
    return ASSETS_DIR / ep_id / subdir / f"{kind}.{default_ext}"


def shared_path(category: str, name: str) -> Path:
    """Path for a cross-episode shared asset (logos, beds, broll library, card bgs).
    Lives under _shared/<category>/."""
    category = str(category).strip().lower()
    if category not in SHARED_CATEGORIES:
        raise AssetPathError(
            f"unknown shared category {category!r}. Allowed: {', '.join(sorted(SHARED_CATEGORIES))}."
        )
    name = str(name).strip()
    if not _SLUG_RE.match(name):
        raise AssetPathError(f"bad shared asset name {name!r}.")
    d = ASSETS_DIR / "_shared" / category
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def shared_url(category: str, name: str) -> str:
    return _rel_url(shared_path(category, name))


def published_path(ep_id: str, name: str) -> Path:
    """Path for a shipped final under _published/{ep_id}/ (the cleanup pattern John likes:
    finals to _published/<ep>/, intermediates deleted)."""
    ep_id = _validate_ep(ep_id)
    name = str(name).strip()
    if not _SLUG_RE.match(name):
        raise AssetPathError(f"bad published asset name {name!r}.")
    d = ASSETS_DIR / "_published" / ep_id
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def is_managed(path: Path | str) -> bool:
    """True if ``path`` lives under the schema: a real episode dir, or a managed top dir."""
    try:
        rel = Path(path).resolve().relative_to(ASSETS_DIR.resolve())
    except (ValueError, OSError):
        return False
    if not rel.parts:
        return False
    head = rel.parts[0]
    return head in MANAGED_TOP or bool(_EP_RE.match(head))


# ---- GC support: the ONLY things the garbage collector may reap -----------------
#
# The schema makes GC trivially safe: the reapable surface is the union of every
# per-episode ``{ep_id}/scratch/`` dir plus the cross-episode ``_scratch/`` (spec lines
# 27, 32). Nothing else is ever a GC candidate — not footage/css/remotion/broll/audio/
# renders, not timeline.json/manifest.json, not _shared/_published, and never a dir or a
# symlink. So the GC no longer has to flat-glob the root and then defensively skip schema
# dirs/symlinks (the old ``_gc_unsafe`` band-aid): it just walks these two roots.
#
# ============================ GC SAFETY INVARIANT (RUNBOOK) =======================
# Reapable-scratch-only guarantee — the single rule operating this GC depends on:
#
#   The GC may ONLY reap regular files under a scratch root: per-episode
#   ``{ep_id}/scratch/`` and cross-episode ``_scratch/``. It may NEVER delete a render,
#   audio/VO, footage, css/remotion/broll asset, timeline/manifest JSON, a schema dir,
#   a back-compat symlink, or anything under ``_shared`` / ``_published`` / ``_trash``.
#
# Enforced in TWO layers so a buggy caller cannot cause loss:
#   1. ENUMERATION — ``reapable_scratch()`` only yields regular files under ``scratch_dirs()``;
#      ``_old_episode_renders()`` (in abn_factory) only yields ``<ep_dir>/renders/episode.mp4``,
#      skipping ``_``-prefixed top dirs and symlinked episode dirs.
#   2. PHYSICAL DELETE — ``tombstone()`` RAISES on any non-scratch path; ``tombstone_render()``
#      RAISES on anything that isn't ``<ep_dir>/renders/*``. Both MOVE to ``_trash/`` (never
#      ``unlink``), so even a mistaken reap is recoverable, not data loss.
#
# Low-disk render trim is the one path that touches a real render. It is gated by:
#   - an Editor Bay timeline-reference scan (never trim a referenced render),
#   - a fail-safe that SKIPS the trim entirely if that scan was incomplete (unreadable JSON),
#   - a pre-trim RE-SCAN (closes the TOCTOU where a timeline is saved mid-reap),
#   - and ``tombstone_render()`` (recoverable, not unlink).
# Proven by tests/test_abn_factory_gc.py. The old flat-glob GC that ate original VO
# (CLAUDE.md "origaudio loss") is gone — never reintroduce a root-glob reap.
#
# OBSERVABILITY: ``scratch_usage()`` (below) reports per-episode scratch bytes over exactly
# this reapable surface; ``abn_factory.purge_disk`` emits it so growth can be aged in prod.


def scratch_dirs() -> list[Path]:
    """The reapable scratch roots: every ``{ep_id}/scratch/`` + the cross-episode ``_scratch/``."""
    dirs: list[Path] = []
    cross = ASSETS_DIR / "_scratch"
    if cross.is_dir():
        dirs.append(cross)
    try:
        for child in ASSETS_DIR.iterdir():
            if child.is_dir() and not child.is_symlink() and _EP_RE.match(child.name):
                sd = child / "scratch"
                if sd.is_dir():
                    dirs.append(sd)
    except (FileNotFoundError, OSError):
        pass
    return dirs


def reapable_scratch() -> list[Path]:
    """Every regular FILE under a reapable scratch root — the closed set the GC may delete.
    Directories and symlinks are excluded (a symlink under scratch/ could point INTO the
    schema; following + tombstoning it would orphan the real keeper)."""
    files: list[Path] = []
    for d in scratch_dirs():
        for f in d.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    files.append(f)
            except OSError:
                pass
    return files


def scratch_usage() -> dict[str, int]:
    """OBSERVABILITY: bytes of reapable scratch, broken down by reap root, for measuring
    per-episode scratch growth in prod (the GC's blast-radius ticket asks to age this under load).

    Keyed by the scratch-root owner — ``ep_id`` for a per-episode ``{ep_id}/scratch/`` and
    ``"_scratch"`` for the cross-episode root — to the summed byte size of the regular files under it.
    Walks EXACTLY ``reapable_scratch()`` (same closed set the GC may delete), so what this reports as
    growth is by construction only ever reapable scratch — never a render, audio, footage, or schema
    dir. A non-reapable surface can therefore never silently show up here as 'scratch growth'."""
    by_owner: dict[str, int] = {}
    base = ASSETS_DIR.resolve()
    for f in reapable_scratch():
        try:
            owner = f.resolve().relative_to(base).parts[0]  # ep_id, or "_scratch"
            by_owner[owner] = by_owner.get(owner, 0) + f.stat().st_size
        except (OSError, ValueError, IndexError):
            pass
    return by_owner


def tombstone(path: Path | str) -> int:
    """SAFE-DELETE: move ``path`` into ``_trash/`` instead of unlinking it (spec line 33), so a
    mistaken reap is recoverable rather than data loss. Returns the byte size moved (0 on no-op).

    Refuses to tombstone anything that is NOT a regular file under a reapable scratch root —
    schema dirs, renders, audio, symlinks, _shared/_published all RAISE, so the GC physically
    cannot turn a tombstone call into whole-episode loss even if handed a bad path."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            raise AssetPathError(f"refusing to tombstone non-regular-file {p}")
        rel = p.resolve().relative_to(ASSETS_DIR.resolve())
    except (ValueError, OSError) as e:
        raise AssetPathError(f"refusing to tombstone off-store path {p}: {e}")
    parts = rel.parts
    in_scratch = (parts[0] == "_scratch") or (
        len(parts) >= 2 and bool(_EP_RE.match(parts[0])) and parts[1] == "scratch"
    )
    if not in_scratch:
        raise AssetPathError(
            f"refusing to tombstone {rel} — GC may only reap scratch/ (per-episode) and _scratch/."
        )
    return _move_to_trash(p, rel)


def tombstone_render(path: Path | str) -> int:
    """SAFE-DELETE for a real episode RENDER under low disk (spec line 33): move ``path`` into
    ``_trash/`` instead of unlinking it, so a mistaken low-disk trim is recoverable rather than
    permanent data loss. Returns the byte size moved.

    Mirror of ``tombstone()`` but for the renders/ surface — exactly what ``_old_episode_renders()``
    enumerates (``<ep_dir>/renders/episode.mp4`` under any episode dir, including legacy short ids).
    Refuses anything that is NOT a regular file at ``<ep_dir>/renders/*``: audio, footage, scratch,
    symlinks, a bare top-level file, and any reserved top dir (``_shared``/``_published``/``_trash``/
    ``_scratch``) all RAISE — so a buggy disk-trim caller physically cannot turn this into loss
    outside an episode's renders/."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            raise AssetPathError(f"refusing to tombstone non-regular-file {p}")
        rel = p.resolve().relative_to(ASSETS_DIR.resolve())
    except (ValueError, OSError) as e:
        raise AssetPathError(f"refusing to tombstone off-store render {p}: {e}")
    parts = rel.parts
    if not (len(parts) >= 2 and parts[1] == "renders" and not parts[0].startswith("_")):
        raise AssetPathError(
            f"refusing to tombstone {rel} — render safe-delete may only reap <ep_dir>/renders/."
        )
    return _move_to_trash(p, rel)


def _move_to_trash(p: Path, rel: Path) -> int:
    """Move ``p`` to ``_trash/<rel>``, mirroring the relative path so collisions across episodes
    can't clobber and a human can see where a tombstoned file came from. Suffix on basename
    collision. Returns the byte size moved. (Shared by tombstone + tombstone_render.)"""
    size = p.stat().st_size
    dest = ASSETS_DIR / "_trash" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}.{int(p.stat().st_mtime)}{dest.suffix}")
    p.replace(dest)
    return size


# Reverse map: classify a LEGACY flat filename (`ep_648e806a_s0_card.png`) into the
# schema so the migration script knows where it belongs. Returns None for files that
# aren't episode-scoped (shared/scratch are handled separately by the migrator).
_LEGACY_RE = re.compile(
    r"^(?P<ep>(?:rec_)?ep_[0-9a-f]{6,}|ep\d+)_"      # episode id
    r"(?:(?P<seg>s\d+)_)?"                             # optional segment
    r"(?P<rest>.+?)"                                   # the descriptive middle
    r"\.(?P<ext>[A-Za-z0-9]+)$"                        # extension
)


def classify(path: Path | str) -> Optional[dict]:
    """Reverse-engineer a flat legacy filename into {ep_id, kind, slug, subdir, ext}.
    Used only by the one-time migration. Falls back to 'scratch' for episode-scoped
    files whose kind isn't in the vocabulary (so nothing is dropped on the floor)."""
    name = Path(path).name
    m = _LEGACY_RE.match(name)
    if not m:
        return None
    ep = m.group("ep")
    seg = m.group("seg")
    rest = m.group("rest")
    ext = m.group("ext").lower()
    lowered = rest.lower()
    # two-word kinds first
    if lowered.startswith("thumb_bg") or lowered == "thumb_bg":
        kind = "thumb_bg"
    elif lowered == "thumb" or lowered.endswith("_thumb"):
        kind = "thumb"
    else:
        kind = rest.split("_")[-1].lower()
    subdir, _ = KINDS.get(kind, ("scratch", ext))
    base = rest
    if base.lower().endswith("_" + kind):
        base = base[: -(len(kind) + 1)]
    slug_parts = [p for p in (seg, base) if p]
    slug = "_".join(slug_parts) or (seg or kind)
    if subdir == ".":
        subdir = ""
    return {"ep_id": ep, "kind": kind, "slug": slug, "subdir": subdir, "ext": ext}
