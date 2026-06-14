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


def _rel_url(p: Path) -> str:
    return URL_PREFIX + str(p.relative_to(ASSETS_DIR))


def asset_url(ep_id: str, kind: str, slug: Optional[str] = None, *, ext: Optional[str] = None) -> str:
    """The /agenticnews-assets/ URL for an episode asset — what goes into timelines/props.
    Side-effect-creates the dir (same as asset_path) so the URL is always backed by a real dir."""
    return _rel_url(asset_path(ep_id, kind, slug, ext=ext))


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
