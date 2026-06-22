"""
AgenticBuilderNews — the autonomous episode FACTORY.

This is the real pipeline. It chains every local tool into a full multi-segment
episode and emits a live event for every action so the /factory page can show
the human the machine working. The human does NOT operate it.

Chain (per the measured competitor recipe — AI Explained / TheAIGRID format):
  scrape -> score -> bundle ~7 segments (retention-ordered)
  -> per segment: script (~110 words) -> Pocket-TTS VO -> whisper word-timestamps
     -> capture source screenshot -> build timeline (Ken-Burns + highlight + karaoke caption)
  -> ffmpeg assemble full episode -> approval gate

Events stream over SSE (services emit; routers/agenticnews.py exposes /stream).
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import shlex
import sqlite3
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from collections import deque

import services.agenticnews as db

_log = logging.getLogger(__name__)


async def _best_effort_update(vid: str, patch: dict) -> None:
    """Best-effort board-state update. Tolerates DB contention (sqlite3.Error,
    e.g. 'database is locked') silently — these state writes are non-essential.
    Any OTHER exception (AttributeError, NameError, TypeError…) is a real bug
    that must surface, so we log it instead of swallowing it in a bare except.
    """
    try:
        await db.update_video(vid, patch)
    except sqlite3.Error:
        pass
    except Exception:
        _log.exception("unexpected error updating video %s with %r", vid, patch)

# v2 anti-slop visual system: deconstruct VO into scenes → designed cards instead of blog
# screenshots. Imported defensively so a v2 issue can never break the running v1 producer.
try:
    from factory.formats.scenes import tag_scenes, direct_visuals, hero_number, hero_number_label
    from factory.formats.types import SceneRole
    from factory.formats import cards as _v2cards
    _V2_VISUALS = True
except Exception as _v2e:  # pragma: no cover
    _V2_VISUALS = False


def _ensure_card_backgrounds(want: int = 6):
    """Keep a pool of cinematic GPT-image backgrounds (via Codex, PRO plan) in card_backgrounds/ so the
    designed cards composite text over REAL imagery instead of a flat blue gradient. Cheap: generated
    once and reused across episodes; tops up the pool toward `want`. Sets cards._ASSETS_DIR so _base()
    can find them. Best-effort — if codex is unavailable, cards fall back to the gradient."""
    if not _V2_VISUALS:
        return
    try:
        # Read AND write the bg pool through the abn_assets gateway. shared_path()
        # validates the name (_SLUG_RE) and lands files in _shared/card_backgrounds/;
        # point the cards reader at that same dir so its <assets>/card_backgrounds
        # resolves to exactly where we write — no off-schema flat dump under ASSETS root.
        _v2cards._ASSETS_DIR = _cards_assets_dir()
        bgdir = shared_path("card_backgrounds", "bg_00.png").parent
        have = [p for p in bgdir.glob("bg_*.png") if not p.name.startswith("._")]
        prompts = [
            "Cinematic dark tech background: deep navy and cyan, abstract circuit-board light traces and glowing nodes, depth of field, atmospheric.",
            "Cinematic dark background: flowing neural-network nodes and data streams, navy to crimson, particles, depth, moody.",
            "Cinematic abstract server-room / data-center bokeh, deep blue, soft glowing lights, shallow depth of field, dark and premium.",
            "Cinematic dark gradient with subtle floating geometric tech shapes and faint grid, navy and teal, atmospheric, minimal.",
            "Cinematic dark AI cityscape from above at night, blue and magenta light trails, depth, futuristic, no text.",
            "Cinematic abstract holographic data visualization, dark background, cyan and red glowing lines, depth, premium tech mood.",
        ]
        import shutil as _sh
        idx = len(have)
        while idx < want and idx < len(prompts):
            rel = _codex_image(prompts[idx], f"_tmp_bg_{idx}")
            if rel:
                # rel is a /agenticnews-assets/<subpath> URL — resolve through ASSETS (it now
                # lands in _scratch/, not the root) before promoting the keeper into the bg pool.
                src = ASSETS / rel.removeprefix("/agenticnews-assets/")
                if src.exists():
                    src.replace(shared_path("card_backgrounds", f"bg_{idx:02d}.png"))
            idx += 1
    except Exception:
        pass

ASSETS = db.ASSETS_DIR

# The asset-path GATEWAY (services/abn_assets) is the ONLY sanctioned way to build a
# write path under the asset store — it enforces the per-episode folder schema so every
# generated layer lands in {ep_id}/{footage,css,remotion,broll,audio,renders}/ instead
# of the old flat dump. Factory slugs are `{ep_id}_s{N}`, so *_from_slug splits the
# ep_id off the front; _asset_url turns a managed path back into its /agenticnews-assets/ URL.
from services.abn_assets import (  # noqa: E402
    asset_path, asset_url, asset_path_from_slug, asset_url_from_slug,
    scratch_path, shared_path, published_path, split_slug, URL_PREFIX,
    reapable_scratch, scratch_dirs, scratch_usage, tombstone, tombstone_render,
)
from services.json_store import atomic_save  # noqa: E402
from services.fsutil import safe_unlink  # noqa: E402


def _cards_assets_dir() -> str:
    """Base dir the v2 cards reader (cards._bg_pool: <assets>/card_backgrounds) must point at so
    its reads resolve to the SAME dir the gateway writes to. shared_path() lands the bg pool in
    _shared/card_backgrounds/, so the reader's base is that dir's parent (ASSETS/_shared) — derived
    from the gateway, not hardcoded, so read+write can never drift apart."""
    return str(shared_path("card_backgrounds", "bg_00.png").parent.parent)


def _asset_url(path: Path) -> str:
    """Absolute managed asset path -> its /agenticnews-assets/ URL (what goes in timelines)."""
    return URL_PREFIX + str(Path(path).resolve().relative_to(ASSETS.resolve()))


# Validation close to the gateway's _SLUG_RE: a flat basename — alphanumerics, dot, dash,
# underscore; no leading dot, no slashes, no traversal. (A leading underscore is allowed here,
# unlike the per-episode slug rule, because real cross-scratch names like '_tmp_bg_0' carry one.)
# Same closed shape abn_assets enforces, so a bad name can't slip a stray file into the store.
_SCRATCH_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _cross_scratch_path(name: str) -> Path:
    """Gateway-routed write path for a CROSS-EPISODE throwaway intermediate under ``_scratch/``
    (a Codex/Flux generation copy, a wan-i2v library clip, an OCR probe still — none episode-scoped).

    The asset gateway only hands out PER-EPISODE scratch (``scratch_path`` requires an ``ep_id``
    prefix), so these library/probe intermediates have no episode to scope to. Rather than hand-build
    ``ASSETS / "_scratch" / name`` + ``mkdir`` at each call site (bypassing the runtime write-path
    check the gateway exists to enforce — abn_assets line 14), funnel all of them through this ONE
    chokepoint: validate the basename against the same closed shape the gateway uses, build the path,
    and assert the gateway itself recognises it as managed (``_scratch`` is a MANAGED_TOP dir + a
    reapable GC root). An off-schema name RAISES before any bytes are written, so the GC can never be
    handed an unreapable stray. Returns the absolute Path with its parent created."""
    name = str(name).strip()
    if not _SCRATCH_NAME_RE.match(name) or "/" in name or "\\" in name:
        raise ValueError(
            f"bad cross-scratch filename {name!r}. Use a flat basename "
            f"(alphanumerics, dot, dash, underscore; no slashes, no leading dot, no traversal)."
        )
    dest = ASSETS / "_scratch" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Assert the write lands in a GC-REAPABLE root, not merely a MANAGED one. is_managed() is too
    # weak here: it's True for broll_library/, _shared/, _published/, editor_renders/ — all managed
    # but NEVER reaped — so the old is_managed() guard would wave a write into a dir the GC can't
    # touch, the exact unreapable-stray / disk-bloat failure this chokepoint exists to prevent. The
    # GC's reapable roots are scratch_dirs() by definition, so check membership against that set.
    if dest.parent.resolve() not in {d.resolve() for d in scratch_dirs()}:
        raise ValueError(f"refusing non-reapable cross-scratch write {dest}")
    return dest


def _resolve_asset(url_or_path) -> Path:
    """Inverse of _asset_url: a /agenticnews-assets/<subpath> URL (or bare name / abs path) -> the
    on-disk Path under ASSETS. PRESERVES SUBDIRS — flattening to basename was silently dropping the
    per-episode subdir, so a migrated asset read back as missing. Legacy flat names still resolve via
    the back-compat symlinks the migration left at the old paths."""
    s = str(url_or_path or "")
    if s.startswith(URL_PREFIX):
        return ASSETS / s[len(URL_PREFIX):]
    p = Path(s)
    if p.is_absolute():
        return p
    return ASSETS / s  # bare relative name → under the store root (legacy symlink covers old flat names)


def _normalize_asset_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except Exception:
        return path


def _editor_timeline_asset_paths_checked() -> tuple[set[Path], bool]:
    """Like _editor_timeline_asset_paths but also return whether the scan was COMPLETE.

    `complete` is False if any part of the protection scan failed — the timeline dir couldn't be
    globbed, or any individual timeline JSON couldn't be read/parsed. When the scan is incomplete
    the returned set may be MISSING references an active Editor Bay timeline still depends on, so a
    destructive GC must treat an incomplete scan as "protect everything" and skip render trimming
    (a swallowed stat/permission/parse error must never let the disk-wall trim eat a live render)."""

    paths: set[Path] = set()
    complete = True
    timeline_dir = ASSETS / "editor_timelines"
    asset_root = _normalize_asset_path(ASSETS)
    try:
        timeline_paths = list(timeline_dir.glob("*.json"))
    except FileNotFoundError:
        # No editor_timelines dir at all → nothing to protect, and that's a COMPLETE answer.
        return paths, True
    except Exception:
        # Permission denied / IO error globbing → we genuinely don't know what's referenced.
        return paths, False

    def collect(value):
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
            return
        if isinstance(value, list):
            for child in value:
                collect(child)
            return
        if not isinstance(value, str):
            return
        if value.startswith("/agenticnews-assets/"):
            # Strip a ?query / #fragment cache-buster (the editor persists render-cache
            # URLs like "…/episode.mp4?rev=3"); the static mount ignores it, so the real
            # file on disk is "episode.mp4". Without this strip the protected path becomes
            # "episode.mp4?rev=3", never matches the keeper, and the GC tombstones a render
            # an Editor Bay timeline still references.
            rel = value.removeprefix("/agenticnews-assets/").split("?", 1)[0].split("#", 1)[0]
            paths.add(_normalize_asset_path(ASSETS / rel))
            return
        path = Path(value)
        if path.is_absolute():
            normalized = _normalize_asset_path(path)
            try:
                normalized.relative_to(asset_root)
            except ValueError:
                return
            paths.add(normalized)

    for timeline_path in timeline_paths:
        try:
            collect(json.loads(timeline_path.read_text()))
        except Exception:
            # A timeline we can't read/parse may reference renders we now can't see → scan incomplete.
            complete = False
            continue
    return paths, complete


def _editor_timeline_asset_paths() -> set[Path]:
    """Return asset paths that are still referenced by Editor Bay timelines."""

    return _editor_timeline_asset_paths_checked()[0]


def _editor_timeline_asset_names() -> set[str]:
    """Return asset basenames that are still referenced by Editor Bay timelines."""

    return {path.name for path in _editor_timeline_asset_paths()}


def _is_editor_timeline_protected_asset(path: Path, protected_paths: set[Path]) -> bool:
    return _normalize_asset_path(path) in protected_paths


# Point the card generator at the cinematic-background pool at MODULE LOAD — so EVERY path (the loop AND
# a direct produce_one_episode from force_ep.py, which bypasses start_factory) composites cards over real
# backgrounds, not the flat gradient. (Was only set in start_factory → forced episodes missed it.)
if _V2_VISUALS:
    try:
        _v2cards._ASSETS_DIR = _cards_assets_dir()
    except Exception:
        pass
WPM = 195
_FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
# v2 visuals on by default; ABN_V2_VISUALS=0 falls back to the legacy screenshot chain.
_USE_V2_VISUALS = os.getenv("ABN_V2_VISUALS", "1") == "1"
SEG_WORDS = 200           # script-length hint to the LLM. Timing everywhere uses measured VO duration,
                          # because actual TTS pace varies by engine/voice.
N_SEGMENTS = 11           # target episode size. 11 stories × ~86s actual + sting ≈ 15-16min. Longer than
                          # the old "11-12min" comment claimed, but VALID + RPM-positive (more mid-roll
                          # inventory) and clears every gate. Keep high — do NOT trim for "shorter".

# ─────────────────────── HARD VALUATION GATES (non-negotiable) ───────────────────────
# These are ENFORCED parameters, not aspirations. An episode that violates a gate does NOT
# reach 'review' — it is rejected/looped. This is the contract that defines "satisfactory work".
MIN_EPISODE_SEC = 600     # 10:00 HARD FLOOR. Render shorter than this is auto-rejected (RPM/mid-roll).
MIN_SEGMENTS    = 8       # floor on segment count so we START long enough to clear MIN_EPISODE_SEC.
                          # 8 × ~86s actual ≈ 11.5min of VO + cold-open + sting → comfortably clears 10min.

# Rotating subjects for the autonomous LORE format (every 6th episode) — origin stories builders care about
_LORE_SUBJECTS = [
    "The Rise of Anthropic", "How Cursor Ate the IDE", "The ggml and llama.cpp Gambit",
    "The Rise of Hugging Face", "How Ollama Made Local LLMs Easy", "The Story of LangChain",
    "The Mistral Open-Weight Bet", "How Replit Built an Agent", "The vLLM Inference Revolution",
    "The Rise of Perplexity",
]

# ---------------- EVENT BUS ----------------
class EventBus:
    def __init__(self):
        self._seq = 0
        self._ring = deque(maxlen=600)
        self._subs: set[asyncio.Queue] = set()

    def emit(self, actor, action, detail="", episode_id=None, segment_id=None,
             artifact_url=None, data=None, stage=None):
        self._seq += 1
        ev = {"id": self._seq, "ts": time.time(), "actor": actor, "action": action,
              "detail": detail, "episode_id": episode_id, "segment_id": segment_id,
              "artifact_url": artifact_url, "data": data or {}, "stage": stage}
        self._ring.append(ev)
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        return ev

    def replay(self, since=0):
        return [e for e in self._ring if e["id"] > since]

    async def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)


BUS = EventBus()

# factory live state (what's happening right now, for the pulse panel)
STATE = {"running": False, "stage": "idle", "actor": "-", "episode_id": None,
         "detail": "factory idle", "started": None}
_PAUSE = asyncio.Event(); _PAUSE.set()  # set = running, clear = paused


def _download(url: str, dest, timeout: int = 60) -> bool:
    """Download a URL to dest WITH a timeout. urllib.request.urlretrieve takes no timeout, so a hung
    CDN download blocks an episode FOREVER (a real freeze risk on voice/b-roll/image fetches). This
    wraps urlopen with a socket timeout and streams to disk. Returns True on success."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as fh:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        return Path(dest).exists() and Path(dest).stat().st_size > 0
    except Exception:
        try:
            if Path(dest).exists() and Path(dest).stat().st_size == 0:
                Path(dest).unlink()
        except Exception:
            pass
        return False


def _set(stage, actor, detail, episode_id=None):
    STATE.update(stage=stage, actor=actor, detail=detail)
    if episode_id:
        STATE["episode_id"] = episode_id
    BUS.emit(actor, f"stage.{stage}", detail, episode_id=episode_id, stage=stage)


# Bound concurrent subprocess spawns. Wave 3 gathers 7 segments, each spawning VHS/ffmpeg/etc;
# spawning them all at once races asyncio's child-watcher → 'Racing with another loop to spawn a
# process' → the whole episode aborts. Serializing the SPAWN (not the run) fixes it without killing
# parallelism — the slow part (process execution) still overlaps; only the brief spawn is gated.
_SPAWN_LOCK = asyncio.Semaphore(2)


async def _sh(cmd, timeout=600):
    async with _SPAWN_LOCK:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill(); return 124, "timeout"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


# ---------------- SCRAPE + SCORE ----------------
# hard off-niche gate — finance/business/politics/gadget noise that isn't agentic-builder content.
# Word-boundary anchored so it won't false-positive on substrings.
_OFFNICHE = re.compile(
    r'\b(stock|stocks|s&p|s and p|ipo|s-1|valuation|valuations|most valuable|wall street|nasdaq|shares|'
    r'lawsuit|sues|sued|antitrust|tariff|tariffs|election|senate|congress|sec filing|'
    r'burry|short seller|bubble|market cap|earnings|revenue beat|layoffs?|fundraise|raises \$|'
    r'iphone|android phone|smartphone|gadget|wearable|smartwatch)\b', re.I)


def _scrape_sync():
    """Lab-focused scrape: what OpenAI/Anthropic/frontier labs are SHIPPING. Recent only."""
    items = []
    # bias hard toward the frontier labs + shipping news
    queries = ["OpenAI", "Anthropic", "Claude", "GPT", "Gemini", "AI model release",
               "new AI model", "AI agent", "coding agent"]
    # only stories from the last ~7 days (current events, not evergreen)
    cutoff = int(time.time()) - 7 * 86400
    for q in queries:
        try:
            qs = urllib.parse.quote(q)
            url = (f"https://hn.algolia.com/api/v1/search?query={qs}&tags=story"
                   f"&numericFilters=points%3E60,created_at_i%3E{cutoff}&hitsPerPage=6")
            with urllib.request.urlopen(url, timeout=12) as r:
                for h in json.load(r).get("hits", []):
                    t = (h.get("title") or "").strip()
                    if not t:
                        continue
                    items.append({
                        "title": t, "pts": h.get("points", 0), "comments": h.get("num_comments", 0),
                        "created": h.get("created_at_i", 0),
                        "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    })
        except Exception:
            pass
    # GitHub trending agentic repos for variety
    try:
        gh = ("https://api.github.com/search/repositories?q=agent+OR+agentic+pushed:>2026-06-01"
              "&sort=stars&order=desc&per_page=5")
        req = urllib.request.Request(gh, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            for repo in json.load(r).get("items", [])[:5]:
                items.append({
                    "title": f"{repo['name']}: {_clip(repo.get('description') or '', 56)}",
                    "pts": repo.get("stargazers_count", 0) // 1000, "comments": 0,
                    "url": repo.get("html_url", ""),
                })
    except Exception:
        pass
    # Reddit — agentic-builder subreddits (deeper, faster-churning niche pool)
    for sub in ("LocalLLaMA", "AI_Agents", "LLMDevs"):
        try:
            req = urllib.request.Request(f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=8",
                                         headers={"User-Agent": "AgenticBuilderNews/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                for c in json.load(r).get("data", {}).get("children", []):
                    p = c.get("data", {})
                    t = (p.get("title") or "").strip()
                    if t and p.get("ups", 0) > 80:
                        items.append({"title": t, "pts": p.get("ups", 0), "comments": p.get("num_comments", 0),
                                      "url": p.get("url") or f"https://reddit.com{p.get('permalink','')}"})
        except Exception:
            pass
    # (HuggingFace daily-papers removed — it surfaces CV/robotics academic papers, not
    #  agentic-builder dev content. Wrong niche, adds noise. Reddit + HN + GitHub are on-target.)
    # Lobsters — high signal-to-noise dev community, on-niche
    try:
        req = urllib.request.Request("https://lobste.rs/t/ai,programming.json",
                                     headers={"User-Agent": "AgenticBuilderNews/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            for p in json.load(r)[:10]:
                t = (p.get("title") or "").strip()
                if t and p.get("score", 0) > 8:
                    items.append({"title": t, "pts": p.get("score", 0) * 10, "comments": p.get("comment_count", 0),
                                  "url": p.get("url") or p.get("short_id_url", "")})
    except Exception:
        pass
    # HARD off-niche filter — drop finance/business/politics/gadget stories before scoring.
    # Defense in depth: this deterministic gate runs BEFORE the scout's judgment, so finance
    # news never leaks even when the LLM scout is tempted by a high-point story.
    items = [it for it in items if not _OFFNICHE.search(it.get("title", ""))]
    # dedup by normalized title stem (drop near-dupes like "...– more things")
    seen, uniq = set(), []
    for it in items:
        stem = re.sub(r'[^a-z0-9 ]', '', it["title"].lower())[:40]
        if stem in seen:
            continue
        seen.add(stem); uniq.append(it)
    return uniq


# stale / evergreen topics to EXCLUDE — we want current AI-builder events, not explainers
STALE_PAT = re.compile(r'\b(explained|visualization|inevitabilism|introduction|guide|tutorial|'
                       r'what is|how to|learning to|fundamentals|101|deep dive into|history of|'
                       r'ask hn|show hn|launch hn)\b', re.I)
# RETRO / OLD-TECH drift — this is an AI-builder NEWS channel; a 1997/retro topic is off-brand stale
# (the titler produced 'making a game in visual studio 1997' — caught on a real episode). Filter
# vintage years + retro hardware/era keywords. Years 2018+ are fine (recent AI era).
_RETRO_PAT = re.compile(r'\b(19\d{2}|200\d|201[0-7])\b|'                       # pre-2018 years
                        r'\b(commodore|amiga|ms-?dos|windows 9[58]|windows xp|floppy|dial-?up|'
                        r'visual studio 199\d|turbo pascal|geocities|netscape|vintage|retro|'
                        r'nostalgia|back in the day|old-?school)\b', re.I)


def _is_stale(it):
    t = it.get("title", "")
    return bool(STALE_PAT.search(t)) or bool(_RETRO_PAT.search(t))


LABS = ("openai", "anthropic", "claude", "gpt", "chatgpt", "gemini", "google deepmind",
        "deepmind", "mistral", "meta ai", "llama", "deepseek", "qwen", "grok", "xai")


def _clip(text, n):
    """Truncate on a word boundary with an ellipsis — never cut mid-word (looks broken on cards)."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",.;:- ")
    return (cut or text[:n]) + "…"


def mem_recent(title, window):
    """Windowed freshness check (shorter window for evergreen so the channel never goes fully dark)."""
    try:
        import services.abn_memory as mem
        return mem.is_recently_used(title, window)
    except Exception:
        return False


def _is_recently_used_safe(title):
    try:
        import services.abn_memory as mem
        return mem.is_recently_used(title)
    except Exception:
        return False


def _evergreen_topics(n):
    """Evergreen deep-dive candidates — notable agentic tools, NOT news-gated. Pulls actively-maintained
    high-star agentic repos so a thin-news day still ships a real episode (the 60%-evergreen lane)."""
    out = []
    # Wide topic pool — the agentic long tail, not just the same 12 mega-repos. Rotate the slice each
    # call (by episode count) so thin-news days surface DIFFERENT tools, never the same langchain/gemini.
    topics = ("ai-agents", "llm", "mcp", "agentic", "rag", "llm-agent", "autonomous-agents",
              "ai-tools", "agent-framework", "local-llm", "llmops", "vector-database",
              "prompt-engineering", "rust", "cli-tool", "tui")
    try:
        import services.abn_memory as _m
        rot = int(_m.stats().get("episodes", 0))
    except Exception:
        rot = 0
    # pick 4 topics this round + vary the star band so we dip into the mid-tail, not just the top
    picks = [topics[(rot + i) % len(topics)] for i in range(4)]
    star_bands = ("stars:1000..8000", "stars:500..4000", "stars:%3E2000", "stars:800..6000")
    band = star_bands[rot % len(star_bands)]
    for topic in picks:
        try:
            gh = f"https://api.github.com/search/repositories?q=topic:{topic}+{band}&sort=updated&order=desc&per_page=6"
            req = urllib.request.Request(gh, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                for repo in json.load(r).get("items", []):
                    desc = (repo.get("description") or "").strip()
                    if not desc:
                        continue
                    out.append({"title": f"{repo['name']} — {_clip(desc, 64)}", "pts": repo.get("stargazers_count", 0) // 1000,
                                "comments": 0, "url": repo.get("html_url", ""), "evergreen": True})
        except Exception:
            pass
    out = [it for it in out if not _OFFNICHE.search(it["title"])]
    return out[:max(n + 4, 6)]


def _score(it):
    if _is_stale(it):
        return -1  # evergreen/explainer, not current news
    t = it["title"].lower()
    # frontier-lab shipping news is the clutch beat — weight it heaviest
    lab = 5 if any(k in t for k in LABS) else 0
    shipping = 3 if any(k in t for k in ("release", "launches", "ships", "announces", "drops",
                                          "introduc", "unveils", "rolls out", "new model", "available",
                                          "open-source", "open source", "api", "agent", "model")) else 0
    drama = 2 if any(k in t for k in ("rogue", "deleted", "hit piece", "silently", "secretly", "without asking", "scam")) else 0
    recency = 2 if any(k in t for k in ("just", "now", "today", "this week")) else 0
    # SEARCHABILITY (the 100k-subs lever): boost household-name tools/products people actually search,
    # PENALIZE obscure fringe repos (rllm, ntsc-rs, awesome-* lists) that nobody searches — the niche
    # drift that was filling episodes. The scout can only pick from what scoring floats up.
    SEARCHABLE = ("gpt", "chatgpt", "claude", "gemini", "llama", "cursor", "copilot", "openai",
                  "anthropic", "perplexity", "midjourney", "sora", "grok", "deepseek", "mistral",
                  "hugging face", "huggingface", "ollama", "langchain", "notion", "github", "vscode")
    searchable = 4 if any(k in t for k in SEARCHABLE) else 0
    # obscure-repo markers: a lowercase-hyphenated repo name ('open-multi-agent', 'ntsc-rs'),
    # an 'awesome-' list, or a tiny star count → niche, hard to discover.
    obscure = 0
    title0 = it.get("title", "")
    head = title0.split("—")[0].split(":")[0].strip()
    if re.match(r'^[a-z][a-z0-9]*(-[a-z0-9]+){1,}$', head):     # lowercase-hyphenated repo name
        obscure += 3
    if "awesome-" in t or t.startswith("awesome "):
        obscure += 4
    if it.get("evergreen") and it.get("pts", 0) < 3 and not searchable:  # low-star evergreen repo
        obscure += 2
    # FLYWHEEL negative signal: deprioritize stories similar to ones the operator rejected
    penalty = 0
    try:
        import services.abn_memory as mem
        penalty = min(4, mem.rejection_penalty(it["title"]))  # cap so one bad topic doesn't nuke a whole vein
    except Exception:
        pass
    return it["pts"] / 100 + lab + shipping + drama + recency + searchable - obscure - penalty


# ---------------- SCRIPT (ollama, fallback template) ----------------
SCRIPT_RULES = (
    "You write voiceover for AgenticBuilderNews, a faceless daily channel covering what the FRONTIER "
    "LABS are shipping — OpenAI, Anthropic, Google, Meta: new models, new apps, new API features, new "
    "policies. Voice: a senior engineer — dry, anti-hype, skeptical until the code runs.\n"
    "HARD RULES (enforced — breaking any is a failure):\n"
    "1. FIRST SENTENCE = the concrete fact. Never open with framing, a greeting, or a recap.\n"
    "2. BANNED phrases — never use any of these or anything like them: 'this week in AI', 'welcome', "
    "'let me break it down', 'in the world of AI', 'no, this isn't', 'it's real', 'sounds neat', "
    "'why care', 'why does this matter', 'but why care', 'here's the kicker', 'sci-fi', 'imagine', "
    "'game-changer', 'revolutionary', 'mind-blowing', 'insane', 'crazy', 'stay tuned', 'we'll dig into', "
    "'we'll get into', 'buckle up'. Do NOT ask the audience rhetorical questions like 'why does this matter?' "
    "— just state why it matters.\n"
    "3. SUBSTANCE over headline. Name the actual model/feature/number/mechanism. If you don't know a specific "
    "fact, do NOT invent one (no fake dates, no 'just shipped a paper' unless the brief confirms it's new).\n"
    "4. TIGHT. Every sentence carries information. No transitions that say nothing.\n"
    "5. Short punchy spoken sentences. No markdown, stage directions, emojis, or headers — only spoken words.\n"
    "6. End on a concrete builder's takeaway, not a tease."
)


def _llm_script_sync(title, is_hook, research="", words=SEG_WORDS, deep=False):
    """Script via the SCRIPTWRITER EXPERT. `words` sets length; `deep`=single-topic deep-dive facet
    (more technical depth, a concrete mechanism + tradeoff, since it's the RPM-driving long format)."""
    try:
        import services.abn_experts as experts
        ctx = f"\nResearch brief (use the core concept, not the headline):\n{research}" if research else ""
        if deep:
            guide = ("This is one facet of a SINGLE-TOPIC DEEP DIVE — go deeper than a news beat: explain the "
                     "actual mechanism, a concrete tradeoff, and what a builder would DO with it. Technical, specific.")
        elif is_hook:
            guide = "This is the COLD OPEN — lead with the single most striking concrete fact."
        else:
            guide = "Lead with the core technical substance, then why it matters to builders, then a one-line builder's take."
        user_p = f"Story: \"{title}\".{ctx}\n\nWrite a {words}-word spoken beat. {guide}"
        return experts.ask("scriptwriter", user_p)
    except Exception:
        return None


def _fetch_source_text(url: str, limit: int = 2500) -> str:
    """Fetch the REAL source text so the researcher works from facts, not training-data guesses (the
    root of vague scripts + missing numbers). For a GitHub repo, pull the raw README (where the real
    specs/numbers live); for an article, pull the page and strip tags. Best-effort, short timeout."""
    if not url:
        return ""
    try:
        import urllib.request as _u
        # GitHub repo → raw README (real specs, version, benchmarks)
        m = re.search(r'github\.com/([^/]+)/([^/#?]+)', url)
        if m:
            owner, repo = m.group(1), m.group(2).replace(".git", "")
            for branch in ("main", "master"):
                for fn in ("README.md", "readme.md", "README.rst"):
                    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fn}"
                    try:
                        with _u.urlopen(raw, timeout=8) as r:
                            txt = r.read(40000).decode("utf-8", "ignore")
                        if txt.strip():
                            txt = re.sub(r'<[^>]+>', '', txt)
                            txt = re.sub(r'\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)', '', txt)  # badges
                            txt = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', txt)               # images
                            return re.sub(r'\n{3,}', '\n\n', txt).strip()[:limit]
                    except Exception:
                        continue
        # generic page → text
        req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0 (AgenticBuilderNews research)"})
        with _u.urlopen(req, timeout=8) as r:
            html = r.read(120000).decode("utf-8", "ignore")
        html = re.sub(r'(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>', ' ', html)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:limit]
    except Exception:
        return ""


def _research_sync(title, url, angle=None):
    """Deep-dive via the RESEARCHER EXPERT, GROUNDED in the real fetched source (README/page) so the
    brief carries REAL facts + numbers, not training-data guesses. For a deepdive FACET, pass `angle`
    so each of the 3 facets researches its OWN sub-topic → distinct briefs → non-repeating scripts."""
    try:
        import services.abn_experts as experts
        src = _fetch_source_text(url)
        src_block = f"\n\nREAL SOURCE (the actual README/page — extract facts + NUMBERS from THIS, don't guess):\n{src}" if src else ""
        if angle:
            q = (f"AI/dev tool: \"{title}\" (source: {url}). This is ONE facet of a deep-dive. Research "
                 f"ONLY this specific angle in depth — ignore the tool's general pitch: {angle}. Give the "
                 f"mechanism, numbers, and tradeoffs SPECIFIC to this angle.{src_block}")
        else:
            q = f"AI/dev item: \"{title}\" (source: {url}). What's the core concept + the story angle a builder needs?{src_block}"
        return experts.ask("researcher", q) or ""
    except Exception:
        return ""


async def _script_segment(title, url, idx, is_hook, research="", deep=False):
    """Research-first, then a tight LLM script. `deep`=single-topic deep-dive facet (longer + deeper)."""
    budget = 200 if deep else SEG_WORDS  # deep-dive facets run ~2x longer for substance (RPM format)
    txt = await asyncio.to_thread(_llm_script_sync, title, is_hook, research, budget, deep)
    if not txt or len(txt) < 40:
        txt = (f"{title}. {research[:200] if research else 'The core of it: this changes how builders ship with agents, and the detail everyone skips is the one that matters.'}")
    words = txt.split()
    if len(words) > budget + 40:
        txt = " ".join(words[:budget + 40])
    return txt


# ---------------- VO (local Pocket-TTS, built-in English voice — the ONLY narrator) ----------------
# The channel narrator is local Pocket-TTS with its built-in English voice. That is the only
# supported VO. There is no voice clone and no cloud TTS: the old john_voice.safetensors clone
# and the Replicate/Chatterbox path were both rejected narrators and have been removed. Do not
# reintroduce a custom voice file or a cloud TTS engine.
#
# Tunable via env (sane default baked in):
#   ABN_POCKET_LANGUAGE — Pocket-TTS language/model, default english_2026-04
# The env value is VALIDATED, never trusted verbatim: an operator (or a poisoned
# environment) could set ABN_POCKET_LANGUAGE=/path/to/evil.safetensors and turn the
# narrator command into `pocket-tts ... --language /path/to/evil.safetensors`, smuggling
# a clone/cloud model file past the locked-voice gate. We accept ONLY a built-in English
# language CODE; anything with a path separator, file extension, or other junk is rejected
# and falls back to the default (and is logged).
_POCKET_DEFAULT_LANGUAGE = "english_2026-04"
# A built-in language CODE — `english_` followed only by alphanumerics / dash / underscore.
# This deliberately matches real codes (english_2026-04, english_2025-12) and is permissive
# enough for test/dev codes (english_test), while rejecting ANY path: a `/`, `\`, `.`, or `~`
# can't appear, so `/path/to/evil.safetensors` or `model.safetensors` never reach --language.
_POCKET_LANG_RE = re.compile(r"^english_[A-Za-z0-9_-]+$")


def _pocket_language() -> str:
    """Resolve the validated Pocket-TTS language code. Rejects path-like / off-pattern env
    values (the locked-voice hard gate) and falls back to the built-in default."""
    raw = os.getenv("ABN_POCKET_LANGUAGE", _POCKET_DEFAULT_LANGUAGE).strip()
    if _POCKET_LANG_RE.match(raw):
        return raw
    if raw:
        _log.warning(
            "ignoring invalid ABN_POCKET_LANGUAGE=%r (not a built-in english_* code); "
            "using built-in %s", raw, _POCKET_DEFAULT_LANGUAGE,
        )
    return _POCKET_DEFAULT_LANGUAGE


def _pocket_tts_command(text: str, out: Path) -> list[str]:
    language = _pocket_language()
    cmd = ["pocket-tts", "generate", "--text", text, "--output-path", str(out), "--quiet"]
    if language:
        cmd += ["--language", language]
    return cmd


async def _voice(text, name):
    # Local Pocket-TTS, built-in English voice — the channel's only narrator (no clone, no cloud).
    out = asset_path_from_slug(name, "voice")
    cmd = shlex.join(_pocket_tts_command(text, out))
    code, log = await _sh(cmd, timeout=300)
    if code != 0 or not out.exists():
        raise RuntimeError(f"tts: {log[-200:]}")
    dur = await _dur(out)
    return _asset_url(out), dur


async def _dur(path):
    code, out = await _sh(f'ffprobe -v error -show_entries format=duration -of csv=p=0 {shlex.quote(str(path))}', timeout=30)
    try:
        return float(out.strip())
    except Exception:
        return 0.0


# ---------------- WHISPER word timestamps ----------------
# faster-whisper with a WARM cached model → ~13s/segment vs ~90s for the CLI cold-start.
_WHISPER_MODEL = None


def _get_whisper():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


# Brand names Whisper mis-splits or mis-cases in the karaoke captions (caught on real episodes:
# 'open ai' for 'OpenAI'). Two-word merges + single-word casing fixes, applied to the word list.
_BRAND_MERGE = {("open", "ai"): "OpenAI", ("lang", "chain"): "LangChain", ("git", "hub"): "GitHub",
                ("chat", "gpt"): "ChatGPT", ("hugging", "face"): "Hugging Face",
                ("anth", "ropic"): "Anthropic", ("co", "pilot"): "Copilot"}
_BRAND_CASE = {"openai": "OpenAI", "langchain": "LangChain", "github": "GitHub", "chatgpt": "ChatGPT",
               "anthropic": "Anthropic", "copilot": "Copilot", "llama": "Llama", "gpt": "GPT",
               "mcp": "MCP", "rag": "RAG", "ollama": "Ollama", "vllm": "vLLM", "ai": "AI"}
# ASR GARBLES — Whisper consistently mangles a few proper nouns (caught across 5 episodes: 'Anthropic'
# became Thropix/Thropic/Thropics/Anthropics 25x). Map a regex of the garble → the correct brand. These
# patterns are NOT real English words, so correcting them can't clobber legitimate text.
_BRAND_GARBLE = [
    (re.compile(r"^(an|a)?thropi[ckxq]s?$", re.I), "Anthropic"),  # thropic/Thropic/thropix/anthropics
                                                                  # FIX: 'an' prefix is fully optional —
                                                                  # bare 'Thropic' (no leading a) is the
                                                                  # actual garble Whisper emits, and the
                                                                  # old '^an?thropi' REQUIRED the 'a', so
                                                                  # 'Thropic' slipped through onto a real
                                                                  # caption ('And Thropic just open...').
    (re.compile(r"^o?pen-?ai$", re.I), "OpenAI"),               # penai / open-ai stragglers
    (re.compile(r"^(co-?pilot|copilots)$", re.I), "Copilot"),
    (re.compile(r"^(lang-?chain|langchains)$", re.I), "LangChain"),
    (re.compile(r"^(g-?p-?t|gpts)$", re.I), "GPT"),
]


def _fix_brand_words(words):
    """Correct brand-name splits/casing in Whisper word-timestamps so captions read 'OpenAI', not
    'open ai'. Merges a split pair into one word (keeping the combined timespan) + fixes casing."""
    if not words:
        return words
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        cur = re.sub(r'[^a-z]', '', (w.get("w") or "").lower())
        raw_nxt = (words[i + 1].get("w") or "") if i + 1 < len(words) else ""
        nxt = re.sub(r'[^a-z]', '', raw_nxt.lower())
        # tolerate a trailing possessive/plural on the 2nd word ('AIs'/'AI's' -> 'ais' -> 'ai')
        nxt_base = re.sub(r's$', '', nxt) if nxt.endswith("s") and (cur, re.sub(r's$', '', nxt)) in _BRAND_MERGE else nxt
        merged = _BRAND_MERGE.get((cur, nxt)) or _BRAND_MERGE.get((cur, nxt_base))
        if merged:
            # preserve a possessive 's (OpenAI's) if the original 2nd word had one
            if re.search(r"['’]s\b|s\b", raw_nxt) and nxt_base != nxt:
                merged += "'s" if "'" in raw_nxt or "’" in raw_nxt else "s"
            out.append({"w": merged, "s": w["s"], "e": words[i + 1]["e"]})
            i += 2
            continue
        raw = w.get("w") or ""
        # possessive/punct suffix to carry across a correction ("Thropic's" -> "Anthropic's")
        suffix = ""
        m_poss = re.search(r"(['’]s)\b", raw)
        if m_poss:
            suffix = "'s"
        trail = re.sub(r"[\w'’]", "", raw)                  # trailing , . etc.
        core = re.sub(r"['’]s\b", "", raw)                  # strip possessive for matching
        core_alpha = re.sub(r'[^a-zA-Z]', '', core)
        # ASR-garble fix FIRST (Thropix -> Anthropic), then plain casing fix
        garbled = next((fix for pat, fix in _BRAND_GARBLE if pat.match(core_alpha)), None)
        # SPECIAL CASE: capitalized 'Tropic's'/'Tropics' (no h) is the garbled-Anthropic Whisper emits
        # in AI-news context — caught on a real caption ('and Tropic's'). Match the RAW word (case-
        # sensitive) so lowercase 'tropics'/'tropical' (real words) are NEVER touched.
        if not garbled and re.match(r"^Tropic(['’]s|s)?$", core if core else raw):
            garbled = "Anthropic"
        base = core_alpha.lower()
        if garbled:
            out.append({"w": garbled + suffix + trail, "s": w["s"], "e": w["e"]})
        elif base in _BRAND_CASE:
            punct = re.sub(r'[\w]', '', raw)
            out.append({"w": _BRAND_CASE[base] + punct, "s": w["s"], "e": w["e"]})
        else:
            out.append(w)
        i += 1
    return out


# ROOT-CAUSE bias for the transcriber: feed Whisper the domain's proper nouns as an initial_prompt so
# it transcribes them CORRECTLY instead of mis-hearing them (Anthropic->Thropic/Tropic, OpenAI->open ai,
# GPT, Claude, Gemini...). This reduces brand garbles at the SOURCE; _fix_brand_words stays as a net.
_WHISPER_VOCAB = ("This is a tech news segment about AI tools and companies including OpenAI, Anthropic, "
                  "Claude, GPT, Gemini, Google, Microsoft, Meta, Llama, Mistral, Cursor, Copilot, GitHub, "
                  "LangChain, Ollama, Hugging Face, Perplexity, MCP, and Codex.")


def _align_sync(wav_path):
    try:
        m = _get_whisper()
        segs, _ = m.transcribe(str(wav_path), word_timestamps=True, language="en",
                               initial_prompt=_WHISPER_VOCAB)
        words = []
        for s in segs:
            for w in (s.words or []):
                words.append({"w": (w.word or "").strip(), "s": round(float(w.start), 2), "e": round(float(w.end), 2)})
        return _fix_brand_words(words)
    except Exception:
        return []


async def _align(wav_name):
    # wav_name is the segment slug ('{ep_id}_s{i}'); the voice wav now lives at the gateway
    # path {ep_id}/audio/s{i}_voice.wav. Resolve it there (fall back to the legacy flat path,
    # which the migration left as a back-compat symlink, so in-flight episodes still align).
    wav = asset_path_from_slug(wav_name, "voice")
    if not wav.exists():
        legacy = ASSETS / f"{wav_name}.wav"
        if legacy.exists():
            wav = legacy
    words = await asyncio.to_thread(_align_sync, wav)
    if words:
        return words
    # fallback: openai-whisper CLI (slow) if faster-whisper unavailable. Alignment json -> audio layer.
    out_json = asset_path_from_slug(wav_name, "align")
    outdir = out_json.parent
    cmd = (f'whisper {shlex.quote(str(wav))} --model tiny.en --word_timestamps True '
           f'--output_format json --output_dir {shlex.quote(str(outdir))} --language en 2>/dev/null')
    await _sh(cmd, timeout=300)
    jf = outdir / f"{wav.stem}.json"
    out = []
    if jf.exists():
        try:
            d = json.loads(jf.read_text())
            for seg in d.get("segments", []):
                for w in seg.get("words", []):
                    out.append({"w": w.get("word", "").strip(), "s": round(w.get("start", 0), 2), "e": round(w.get("end", 0), 2)})
        except Exception:
            pass
    return out


def _script_align(words, script):
    """Make captions SCRIPT-TRUE. Whisper re-transcribing our own TTS audio re-introduced typos
    and brand garbles into the on-screen captions ('rendering captions from the voiceover instead
    of the actual script' — John). We WROTE the words; whisper's only job is timing. Align whisper
    tokens to script tokens (difflib on normalized forms) and take the TEXT from the script while
    keeping whisper's timestamps. Unequal runs distribute script tokens across the whisper span."""
    if not words or not script:
        return words
    import difflib, re
    script_toks = script.split()
    norm = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
    sm = difflib.SequenceMatcher(a=[norm(w["w"]) for w in words],
                                 b=[norm(t) for t in script_toks], autojunk=False)
    out = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            for k in range(a1 - a0):
                out.append({**words[a0 + k], "w": script_toks[b0 + k]})
        elif op == "replace":
            span = words[a0:a1]
            toks = script_toks[b0:b1]
            s, e = span[0]["s"], span[-1]["e"]
            step = (e - s) / max(1, len(toks))
            for k, t in enumerate(toks):
                out.append({"w": t, "s": round(s + k * step, 2), "e": round(s + (k + 1) * step, 2)})
        elif op == "delete":
            # whisper-only tokens (hallucinated/filler) — drop; the script didn't say them
            continue
        # 'insert' (script words whisper never heard): no timing to anchor — skip rather than flash
    return out or words


# ---------------- KINETIC HTML INSERT (seekable-html-video) ----------------
# "this is what i want to see more of in these videos" (John, on the 16:9 explainer). Per-segment
# kinetic typography inserts rendered by tools/seekable-html-video — deterministic browser motion
# graphics on the brand system, mixed in WITH screen recordings and cards, replacing static holds.
_REPO = Path(__file__).resolve().parent.parent
_KINETIC_TPL = _REPO / "tools/seekable-html-video/templates/builder-news-explainer/index.html"


_KINETIC_DIR = _REPO / "tools/seekable-html-video/templates"


def _kinetic_params(title, script, source_url):
    """Art-direct the insert: pick a template + fill its `const P` params from the segment script.
    LLM (scriptwriter expert) with a deterministic fallback. Returns (template_name, P_dict)."""
    dom = re.sub(r'^https?://(www\.)?', '', source_url or "").split("/")[0][:34] or "agenticbuildernews"
    src_line = f"SRC: {dom.upper()}"
    d = {}
    try:
        import services.abn_experts as experts
        raw = experts.ask("scriptwriter",
            "You are art-directing a 12s kinetic-typography insert for a tech-news video. From the "
            "script below return JSON ONLY:\n"
            "{\"template\": \"stat\"|\"zine\",   // stat IF the script's most striking fact is a NUMBER, else zine\n"
            " \"claim\": \"setup line, ≤6 words, ALL CAPS\",  \"statValue\": int, \"statPrefix\": \"\"|\"$\", "
            "\"statSuffix\": \"%\"|\"B\"|\"M\"|\"X\"|\"\", \"statKicker\": \"payoff line ≤6 words ALL CAPS\",\n"
            " \"w\": [exactly 5 SINGLE WORDS that read IN ORDER as one punchy verdict sentence — "
            "like [\"THE\",\"WRAPPER\",\"ERA\",\"IS\",\"DEAD.\"] — w5 is the one-word payoff ending in '.'],\n"
            " \"panelTitle\": \"≤11-char mono label e.g. OBITUARIES, THE OLD WAY\", "
            "\"items\": [3 things being replaced/killed, ≤14 chars each],\n"
            " \"payoffLead\": \"first words of final line ≤10 chars\", \"payoffKey\": \"circled word ≤7 chars\"}\n\n"
            f"Story: {title}\nScript:\n{(script or '')[:1200]}")
        d = json.loads(re.search(r'\{.*\}', raw or "", re.S).group(0))
    except Exception:
        d = {}
    # deterministic stat detection — the GATE for stat-shrine. The LLM alone once chose a shrine
    # to the number "3" (no unit, no meaning). A number is shrine-worthy only if the script
    # literally contains value+unit (%, x, $, B/M) — otherwise it's a zine story.
    m = re.search(r'(\$?)(\d+(?:\.\d+)?)\s*(%|x|billion|million|B\b|M\b)', script or "", re.I)
    tpl = "stat" if (m and d.get("template") != "zine") else "zine"
    if tpl == "stat":
        sfx = {"billion": "B", "million": "M", "x": "X"}.get(str(d.get("statSuffix", "")).lower(),
                                                            str(d.get("statSuffix", "%"))[:2])
        try:
            val = int(float(d.get("statValue")))
        except (TypeError, ValueError):
            val = int(float(m.group(2))) if m else 0
        if val <= 0:
            tpl = "zine"  # a shrine to zero is a zine story after all
        else:
            return "stat-shrine", {
                "claim": str(d.get("claim") or title)[:44].upper(),
                "statValue": val, "statSuffix": sfx,
                "statPrefix": str(d.get("statPrefix", ""))[:1],
                "kicker": str(d.get("statKicker") or "AND BUILDERS FEEL IT.")[:44].upper(),
                "edition": "ED.047 — SILKSCREEN PROOF", "source": src_line,
            }
    # one TOKEN per slot — the layout composes 5 single words ("THE WRAPPER / ERA IS / DEAD.");
    # a phrase in a slot ("PROMPT SITES") breaks the composition (caught on a real render)
    w = [str(x).upper().split()[0] for x in (d.get("w") or []) if str(x).strip()][:5]
    # word-salad guard: repeated tokens ("THIN PROMPT GPT THIN DEAD") mean the LLM filled slots
    # with phrase fragments — the title is a better sentence than a shuffled keyword bag
    if len(w) == 5 and len(set(w[:4])) < 4:
        w = []
    if len(w) < 5:
        tw = [x.upper() for x in re.sub(r'[^A-Za-z0-9 ]', '', title).split()][:4]
        while len(tw) < 4:
            tw.append("NOW")
        w = tw + ["LIVE."]
    if not w[4].endswith("."):
        w[4] += "."
    items = [str(x)[:14].upper() for x in (d.get("items") or [])][:3] or ["THE OLD WAY", "MANUAL OPS", "GUESSWORK"]
    while len(items) < 3:
        items.append("THE OLD WAY")

    def word_trim(s, limit):
        """Trim to limit WITHOUT chopping mid-word ('BUILDERS WHO'[:10] = 'BUILDERS W' shipped
        a real frame reading 'BUILDERS W OWN'). Drop trailing partial words instead."""
        s = str(s).strip()
        if len(s) <= limit:
            return s
        cut = s[:limit]
        return (cut.rsplit(" ", 1)[0] if " " in cut else cut).strip()

    return "zine-slam", {
        "kicker": "ABN ZINE <b>— BUILDER NEWS</b>",
        "w1": word_trim(w[0], 12), "w2": word_trim(w[1], 12), "w3": word_trim(w[2], 12),
        "w4": word_trim(w[3], 12), "w5": word_trim(w[4], 10) or "LIVE.",
        "panelTitle": word_trim((d.get("panelTitle") or "THE OLD WAY").upper(), 11),
        "items": items,
        "payoffLead": word_trim((d.get("payoffLead") or "OWN THE").upper(), 12),
        "payoffKey": word_trim((d.get("payoffKey") or "STACK").upper(), 7),
        "source": src_line, "ghost": word_trim(w[4], 10),
    }


async def _kinetic_insert(title, script, source_url, sid):
    """Render an art-directed seekable-HTML kinetic insert → {"src": served_mp4, "dur": seconds}.
    Templates: stat-shrine (number worship) / zine-slam (verdict typography). The corporate
    builder-news-explainer is dead as an insert — John: "looks like a training video"."""
    tpl_name, P = await asyncio.to_thread(_kinetic_params, title, script, source_url)
    tpl_file = _KINETIC_DIR / tpl_name / "index.html"
    if not tpl_file.exists():
        return None
    html = tpl_file.read_text()
    html = html.replace('url("fonts/', f'url("file://{tpl_file.parent}/fonts/')
    pjs = "const P = {" + ",".join(f"{k}: {json.dumps(v)}" for k, v in P.items()) + "};"
    html, nsub = re.subn(r'const P = \{.*?\};', lambda _: pjs, html, count=1, flags=re.S)
    if not nsub:
        return None
    # intermediate html -> per-episode scratch (reaped freely); final mp4 -> css layer
    src_html = scratch_path(sid, f"{sid}_kinetic.html")
    src_html.write_text(html)
    out = asset_path_from_slug(sid, "kinetic")
    code, log = await _sh(
        f'cd {shlex.quote(str(_REPO))} && NODE_PATH=frontend/node_modules node '
        f'tools/seekable-html-video/render_seekable.cjs --input {shlex.quote(str(src_html))} '
        f'--output {shlex.quote(str(out))} --fps 24 --cleanup-frames', timeout=360)
    src_html.unlink(missing_ok=True)
    if code != 0 or not out.exists() or out.stat().st_size < 50_000:
        return None
    dur = await _dur(out)
    return {"src": _asset_url(out), "dur": float(dur or 11.0), "template": tpl_name}


# ---------------- SOURCE SCREENSHOT ----------------
def _shot_blank_sync(png_path):
    """True if the image is near-blank (a flagship-killer: blank gradient or mostly-white CAPTCHA page).
    Real pages have text/UI → high pixel stddev + many distinct regions. PIL stddev is the reliable signal."""
    try:
        from PIL import Image, ImageStat
        im = Image.open(png_path).convert("L").resize((160, 90))  # downsample: fast + ignores tiny noise
        stat = ImageStat.Stat(im)
        stddev = stat.stddev[0]
        # a blank gradient / uniform CAPTCHA page has stddev < ~18; a real page of text/UI is 40-80+
        return stddev < 18
    except Exception:
        return False  # don't block on the validator failing


async def _shot_is_usable(png: Path):
    """Reject near-blank frames (one of the two flagship-killers found by watching)."""
    return not await asyncio.to_thread(_shot_blank_sync, str(png))


async def _screenshot(url, name):
    """Capture the source page as the on-screen artifact (the trust signal). Validates the result —
    rejects CAPTCHA/bot-walls and near-blank frames (caller falls back to animated b-roll)."""
    out = asset_path_from_slug(name, "src")
    # CAPTCHA/bot-wall guard: pull the page text first; bail if it's a verification wall
    bot_signals = ("verify you are human", "performing security verification", "checking your browser",
                   "enable javascript and cookies", "cloudflare", "are you a robot", "access denied",
                   "verifying...", "just a moment", "ddos protection", "ray id", "captcha",
                   "needs to review the security", "attention required")
    for binname in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chromium", "chrome"):
        b = binname if "/" in binname else binname
        code, _ = await _sh(
            f'{shlex.quote(b)} --headless=new --disable-gpu --hide-scrollbars '
            f'--window-size=1920,1080 --screenshot={shlex.quote(str(out))} {shlex.quote(url)} 2>/dev/null',
            timeout=40)
        if out.exists():
            # dump text to sniff for a bot-wall (best-effort; if dump fails we still have the blank check)
            txt_code, txt = await _sh(
                f'{shlex.quote(b)} --headless=new --disable-gpu --dump-dom {shlex.quote(url)} 2>/dev/null',
                timeout=30)
            low = (txt or "").lower()
            if any(s in low for s in bot_signals) and len(low) < 4000:
                BUS.emit("editor-agent", "shot.reject", "screenshot was a bot-wall/CAPTCHA — discarded")
                safe_unlink(out)
                return None
            if not await _shot_is_usable(out):
                BUS.emit("editor-agent", "shot.reject", "screenshot near-blank — discarded")
                safe_unlink(out)
                return None
            return _asset_url(out)
    return None


# ---------------- VHS CODE DEMO (the agentic-builder moat shot) ----------------
def _codegen_sync(title, brief):
    """Code demo via the CODER EXPERT (single source of truth)."""
    try:
        import services.abn_experts as experts
        return experts.ask("coder", f"Story: {title}\nContext: {brief[:300]}\n\nWrite a 3-6 line terminal demo that shows this in action.")
    except Exception:
        return None


async def _code_demo(title, brief, name):
    """Generate a real code snippet → VHS .tape → 1080p MP4. The 'watch it get built' shot."""
    code = await asyncio.to_thread(_codegen_sync, title, brief)
    if not code:
        return None
    lines = [l.strip() for l in code.splitlines() if l.strip() and not l.strip().startswith("```")][:6]
    if not lines:
        return None
    out = asset_path_from_slug(name, "demo")
    # tape + snippet are throwaway intermediates -> per-episode scratch/
    tape = scratch_path(name, f"{name}.tape")
    # write the snippet to a file and DISPLAY it with bat (syntax-highlighted) — no execution,
    # so we never get 'command not found' errors. This shows clean code, not a broken shell.
    snippet = scratch_path(name, f"{name}_snippet.py")
    snippet.write_text("\n".join(lines) + "\n")
    bat = "bat" if Path("/opt/homebrew/bin/bat").exists() else "cat"
    # VHS has a path-parse bug on absolute Output, so we cd to a working dir and use a relative
    # Output, then move the result to the gateway path. snippet is shown by absolute path (bat is fine).
    workdir = out.parent
    body = [f"Output {out.name}", "Set Width 1920", "Set Height 1080", "Set FontSize 34",
            "Set TypingSpeed 42ms", "Set Theme \"Dracula\"",
            'Type "# AgenticBuilderNews — live build"', "Enter", "Sleep 500ms",
            # type the bat command that renders the code, then run it ONCE (bat just prints, never errors)
            f'Type "{bat} --style=numbers --color=always {shlex.quote(str(snippet))}"', "Enter", "Sleep 2500ms"]
    tape.write_text("\n".join(body) + "\n")
    # cwd = the renders/footage subdir so the relative `Output {out.name}` lands at `out`.
    code_, log = await _sh(f'cd {shlex.quote(str(workdir))} && vhs {shlex.quote(str(tape))} 2>&1', timeout=120)
    if out.exists():
        return _asset_url(out)
    return None


# ---------------- REAL DEMO (genuinely run a tool, capture REAL terminal output) ----------------
# WHY: the credibility moat. Scripted "code demos" are fabricated commands an expert builder
# spots instantly. This clones the ACTUAL repo and runs a WHITELIST of read-only inspection
# commands inside VHS, recording the REAL output. It NEVER executes repo-authored code.
#
# THE SAFETY MODEL (read before extending):
#   VHS runs whatever you `Type ... Enter` in a shell whose cwd we control. The repo's own code
#   does NOT run unless WE invoke a build/run command. So safety == the command whitelist, not the
#   repo. We:
#     1. clone --depth 1 into a FRESH tmp dir (isolated; deleted after)
#     2. run only read-only inspection: ls, tree, cat README, wc, git log --oneline
#     3. NEVER: make/cargo build/npm i/pip install/docker run/./configure/eval of repo scripts,
#        no `npx <repo>`, no running an example, no executing any file the repo shipped.
#   Network is used ONLY by `git clone` (a trusted binary hitting a trusted host we pinned to
#   github.com). After clone we never touch the network again.
#
# WHAT IS *NOT* SAFE TO AUTO-RUN (deliberately excluded — would require sandboxing, see notes):
#   - building the project (arbitrary build.rs / postinstall scripts execute attacker code)
#   - running the tool's own binary / examples (that IS executing untrusted code)
#   - any `--help` that requires building first
#   To ever run those safely you need a real sandbox (gVisor / Firecracker / a throwaway Docker
#   container with --network=none, no host mounts, dropped caps). That is a separate hardening
#   project; this function stays in the provably-safe read-only lane.

_GH_REPO_RE = re.compile(r'https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)', re.I)


def _github_repo(url: str) -> str | None:
    """Return a normalized https github clone URL iff the input is a real github.com repo URL.
    Pinning to github.com is the network-trust boundary — we never clone arbitrary hosts."""
    if not url:
        return None
    m = _GH_REPO_RE.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r'\.git$', '', repo)
    # reject path-traversal / shell-meta in the slug (defense in depth; values are also quoted).
    # a segment must contain at least one alphanumeric char — blocks '.', '..', '...' owners/repos.
    slug = re.compile(r'(?=.*[A-Za-z0-9])[A-Za-z0-9_.-]+')
    if not slug.fullmatch(owner) or not slug.fullmatch(repo):
        return None
    if owner in (".", "..") or repo in (".", ".."):
        return None
    return f"https://github.com/{owner}/{repo}.git"


async def _real_demo(repo_url: str, name: str):
    """Clone a REAL github repo and record REAL read-only inspection output via VHS.
    Returns /agenticnews-assets/<name>_demo.mp4 (same slot as _code_demo) or None.

    Read-only by construction: no build, no run, no repo-authored code executes. The only
    network call is the pinned `git clone`. Falls back to None so the caller can use the
    scripted _code_demo — a fabricated-but-honest snippet is better than a broken shell."""
    clone_url = _github_repo(repo_url)
    if not clone_url:
        return None
    import tempfile, shutil
    workdir = Path(tempfile.mkdtemp(prefix=f"abn_demo_{name}_"))
    repo_dir = workdir / "repo"
    out = asset_path_from_slug(name, "demo")
    tape = scratch_path(name, f"{name}_real.tape")
    try:
        # PRE-CLONE OUT OF BAND so a clone failure (404/private/timeout) cleanly falls back to the
        # scripted demo, AND so the VHS recording opens on already-fetched code (no dead wait on the
        # network in the footage). Shallow, no submodules, no hooks, hard timeout.
        env_safe = "GIT_TERMINAL_PROMPT=0 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null"
        code, log = await _sh(
            f'{env_safe} git clone --depth 1 --no-tags --recurse-submodules=no '
            f'{shlex.quote(clone_url)} {shlex.quote(str(repo_dir))} 2>&1',
            timeout=60)
        if code != 0 or not repo_dir.exists():
            return None
        # find a README to display (case-insensitive, common extensions)
        readme = None
        for cand in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
            if (repo_dir / cand).exists():
                readme = cand; break
        if not readme:
            for p in repo_dir.iterdir():
                if p.is_file() and p.name.lower().startswith("readme"):
                    readme = p.name; break
        # PRE-CLEAN the README to plain prose in Python (avoids fragile in-tape sed escaping). Strips
        # HTML tags, badge/image lines, markdown markup (**/*/`/#), and flattens [text](url)->text so
        # VHS just cats clean readable text instead of noisy markdown source (caught on a real frame).
        readme_clean = None
        if readme:
            try:
                raw = (repo_dir / readme).read_text(errors="ignore").splitlines()
                out_lines = []
                for ln in raw:
                    s = ln.strip()
                    if not s or s.startswith("<") or "![" in s or s.startswith("[!["):
                        continue
                    s = re.sub(r'<[^>]+>', '', s)                       # HTML tags
                    s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)      # [text](url) -> text
                    s = s.replace("**", "").replace("`", "")
                    s = re.sub(r'(?<!\w)\*(?!\w)', '', s)               # stray bullets/emphasis
                    s = re.sub(r'^#+\s*', '', s)                        # headers
                    s = s.strip()
                    if s:
                        out_lines.append(s)
                    if len(out_lines) >= 16:
                        break
                if out_lines:
                    (repo_dir / "_readme.clean.txt").write_text("\n".join(out_lines) + "\n")
                    readme_clean = "_readme.clean.txt"
            except Exception:
                readme_clean = None
        owner_repo = clone_url.rsplit("/", 2)[-2] + "/" + clone_url.rsplit("/", 1)[-1].replace(".git", "")
        # The tape runs in repo_dir (already cloned). Every command here is READ-ONLY inspection.
        # We re-show the clone command for narrative honesty, but point it at the already-fetched
        # copy via `echo` of the real result so we don't re-hit the network in the footage.
        # FAST TO PAYOFF: the demo previously spent ~6s typing a header + a 'git clone' comment line
        # before any real output — a near-empty terminal for the whole opening (caught on a real frame).
        # Cut the ceremony: a SHORT header (faster typing) then go straight to the real `ls` output so
        # the repo contents (the actual proof) fill the screen quickly.
        body = [
            f"Output {out.name}",
            "Set Width 1920", "Set Height 1080", "Set FontSize 30",
            "Set TypingSpeed 18ms", 'Set Theme "Dracula"', "Set Padding 40",
            f'Type "# {owner_repo}"', "Enter", "Sleep 300ms",
            'Type "ls"', "Enter", "Sleep 2200ms",
        ]
        # show the project structure (one level, dirs first) — real `ls`/`tree` output
        if shutil.which("tree"):
            body += ['Type "tree -L 1 -C"', "Enter", "Sleep 2200ms"]
        # real recent history — proves it's a live repo, not a mockup
        body += ['Type "git log --oneline -5"', "Enter", "Sleep 2200ms"]
        # real README head — clean prose (pre-stripped of markdown/HTML above), just cat it.
        if readme_clean:
            body += [f'Type "cat {readme_clean}"', "Enter", "Sleep 3400ms"]
        elif readme:
            pager = "bat --style=plain --color=always --line-range :22" if shutil.which("bat") else "head -22"
            body += [f'Type "{pager} {readme}"', "Enter", "Sleep 3200ms"]
        tape.write_text("\n".join(body) + "\n")
        # VHS runs with cwd = repo_dir, so ls/tree/git/cat all operate on the REAL cloned repo.
        # Output is relative, so we hand VHS an absolute Output by writing it as the first line and
        # moving the produced file (VHS writes Output relative to its cwd).
        code, log = await _sh(
            f'cd {shlex.quote(str(repo_dir))} && vhs {shlex.quote(str(tape))} 2>&1', timeout=120)
        # VHS wrote <name>_demo.mp4 into repo_dir (relative Output); move it to ASSETS
        produced = repo_dir / out.name
        if produced.exists():
            shutil.move(str(produced), str(out))
        if out.exists():
            return _asset_url(out)
        return None
    except Exception:
        return None
    finally:
        try:
            import shutil as _sh2
            _sh2.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
        try:
            if tape.exists():
                tape.unlink()
        except Exception:
            pass


# ---------------- TITLE CARD ----------------
async def _card(headline, sub, name):
    out = asset_path_from_slug(name, "card")
    base = str(Path(__file__).resolve().parent.parent / "fonts")
    font = f"{base}/TikTokSans16pt-Black.ttf"
    if not Path(font).exists():
        font = f"{base}/Montserrat-ExtraBold.ttf"
    subfont = f"{base}/TikTokSans-Bold.ttf"
    if not Path(subfont).exists():
        subfont = font
    head = _clip(headline, 34)
    # designed card, not a slide: diagonal dark-blue gradient + radial glow + accent bar, sexy fonts
    cmd = (
        f'magick -size 1920x1080 gradient:"#0b1020"-"#08090b" '
        f'\\( -size 1920x1080 radial-gradient:rgba\\(110,139,255,0.18\\)-none \\) -compose over -composite '
        # accent bar above the headline
        f'-fill "#4db8e8" -draw "rectangle 810,372 1110,380" '
        f'-gravity center -font {shlex.quote(font)} -interline-spacing -8 '
        f'-fill black -annotate +4-86 {shlex.quote(head)} '            # shadow
        f'-fill "#f2f4f7" -pointsize 92 -annotate +0-90 {shlex.quote(head)} '
        f'-font {shlex.quote(subfont)} -fill "#8b9bd4" -pointsize 42 -annotate +0+44 {shlex.quote(_clip(sub, 54))} '
        f'-fill "#3a4254" -pointsize 26 -gravity south -annotate +0+60 "AGENTICBUILDERNEWS" '
        f'{shlex.quote(str(out))}')
    code, log = await _sh(cmd, timeout=60)
    if code != 0 or not out.exists():
        raise RuntimeError(f"card: {log[-200:]}")
    return _asset_url(out)


# ---------------- THUMBNAIL (Flux-generated background + bold text overlay — the real deal) ----------------
def _codex_image(prompt: str, out_name: str, size: str = "1536x1024") -> str | None:
    """Generate a cinematic image via Codex's built-in image_gen tool (runs on the ChatGPT PRO plan —
    NO API key, no metered billing) and copy it into ASSETS as <out_name>.png. Returns the asset path
    or None. Strategy: snapshot ~/.codex/generated_images before, run `codex exec`, grab the NEW png."""
    import subprocess, shutil, glob as _glob
    gen_dir = Path(os.path.expanduser(os.getenv("CODEX_HOME", "~/.codex"))) / "generated_images"
    before = set(_glob.glob(str(gen_dir / "*" / "ig_*.png")))
    full = (f"Use the image_gen tool to generate a {size} image. {prompt} "
            f"No text, no words, no watermark, no logos. Reply only with the saved file path.")
    try:
        subprocess.run(["codex", "exec", "--skip-git-repo-check", full],
                       capture_output=True, text=True, timeout=180)
    except Exception as e:
        _log.warning("_codex_image: codex exec failed for %r (%s)", out_name, e)
        return None
    after = set(_glob.glob(str(gen_dir / "*" / "ig_*.png")))
    new = sorted(after - before, key=lambda p: os.path.getmtime(p), reverse=True)
    if not new:
        _log.warning("_codex_image: codex exec produced no new image for %r", out_name)
        return None
    # NOT episode-scoped (out_name is e.g. '_tmp_bg_0' / a thumb-bg name) — cross-episode
    # generation intermediate. Routed through the gateway chokepoint so the _scratch/ write
    # path is validated at runtime (reapable), then the caller copies the keeper into
    # card_backgrounds/ or the broll library.
    try:
        dest = _cross_scratch_path(f"{out_name}.png")
        shutil.copy(new[0], dest)
        return _asset_url(dest)
    except Exception as e:
        _log.warning("_codex_image: failed to copy %s into _scratch for %r (%s)", new[0], out_name, e)
        return None


def _flux_sync(prompt):
    """Generate a real cinematic image via Replicate Flux (raw HTTP, no lib). Returns image URL."""
    tok = os.getenv("REPLICATE_API_TOKEN")
    if not tok:
        return None
    try:
        body = {"input": {"prompt": prompt, "aspect_ratio": "16:9", "output_format": "png"}}
        req = urllib.request.Request(
            "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Prefer": "wait"})
        r = json.load(urllib.request.urlopen(req, timeout=90))
        out = r.get("output")
        return out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
    except Exception:
        return None


def _wan_i2v_sync(image_url, name):
    """Animate a clean (text-free) still into a looping b-roll mp4 via Replicate wan-2.5-i2v.
    RULE (validated): only feed TEXT-FREE images — text overlays warp under motion. ~70s, async-polled."""
    tok = os.getenv("REPLICATE_API_TOKEN")
    if not tok or not image_url:
        return None
    try:
        body = {"input": {"image": image_url, "duration": 5,
                          "prompt": "slow cinematic drift, ambient particles, subtle depth parallax, no text"}}
        req = urllib.request.Request("https://api.replicate.com/v1/models/wan-video/wan-2.5-i2v/predictions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=60))
        get_url = r.get("urls", {}).get("get")
        for _ in range(40):  # poll up to ~3.3 min
            time.sleep(5)
            p = json.load(urllib.request.urlopen(urllib.request.Request(get_url, headers={"Authorization": f"Bearer {tok}"}), timeout=30))
            if p.get("status") == "succeeded":
                out = p.get("output"); u = out[0] if isinstance(out, list) else out
                # library b-roll generation (name is 'libgenN', not episode-scoped) — write the
                # raw clip to _scratch/ through the gateway chokepoint (validated, reapable); the
                # caller copies the keeper into broll_library/.
                dest = _cross_scratch_path(f"{name}_broll.mp4")
                _download(u, str(dest), timeout=90)
                return _asset_url(dest) if dest.exists() else None
            if p.get("status") in ("failed", "canceled"):
                return None
    except Exception:
        return None
    return None


# Distinct b-roll looks so we DON'T reuse the same clip every segment. CRITICAL: these are PURE
# ABSTRACT MOTION only — NO code panels, NO circuit boards, NO network-node/connecting-line
# structures, NO wireframe meshes. Flux-schnell turns all of those into garbled fake-glyph
# "symbols-for-words" SLOP (the exact defect John flagged). Gradients, particles, light, fog,
# bokeh, and ink have no text-like structure for the model to hallucinate into pseudo-text.
_BG_LOOKS = [
    "deep black to midnight-blue gradient with soft drifting cyan light particles and gentle bokeh, slow, atmospheric",
    "smooth dark gradient with slow-flowing cyan and red light streaks like aurora, soft motion blur, cinematic",
    "black background with drifting volumetric fog lit by faint cyan glow and soft red embers, moody, slow drift",
    "abstract dark fluid ink swirling slowly, deep navy with cyan and crimson highlights, organic motion, shallow depth",
    "dark field of soft out-of-focus bokeh lights, cyan and red, slowly drifting and breathing, dreamy and premium",
    "smooth charcoal-to-black gradient with a slow sweep of soft cyan rim light and subtle grain, minimal and elegant",
]
# Hard negative appended to every bg prompt — names what Flux must NOT render.
_BG_NEGATIVE = ("NO text, NO words, NO letters, NO numbers, NO code, NO UI panels, NO circuit boards, "
                "NO network diagrams, NO connecting lines or nodes, NO wireframe, NO symbols, NO glyphs, "
                "NO charts. Pure abstract motion graphics only.")


# Cached b-roll library. The clean abstract loops (bokeh/gradient/fog) are NOT episode-specific —
# regenerating 3 fresh Flux→wan-i2v clips every episode was the biggest compute waste in the factory
# (~6 expensive Replicate calls/episode for backgrounds nobody watches the video FOR). Instead we
# build a small LIBRARY once, cache the clips to disk, and serve a rotating subset per episode.
_BG_LIB_DIR = ASSETS / "broll_library"
_BG_LIB_TARGET = 8          # build up to 8 distinct cached loops, then stop generating
_BG_LIB_MIN = 3             # generate lazily until at least this many exist


def _bg_library() -> list[Path]:
    """Cached b-roll clips on disk, sorted (stable rotation)."""
    if not _BG_LIB_DIR.exists():
        return []
    return sorted(p for p in _BG_LIB_DIR.glob("broll_*.mp4") if p.stat().st_size > 4096)


async def _grow_bg_library(want=1):
    """Generate `want` NEW clean clips into the cached library (Flux → OCR self-review → wan-i2v →
    download to disk). Only called when the library is below target. This is where the expensive
    video-gen happens — but now ONCE per library slot, not once per episode."""
    _BG_LIB_DIR.mkdir(parents=True, exist_ok=True)
    have = len(_bg_library())
    made = 0
    for k in range(want):
        if have + made >= _BG_LIB_TARGET:
            break
        look = _BG_LOOKS[(have + made) % len(_BG_LOOKS)]
        prompt = (f"Abstract dark background for an AI/dev channel: {look}. "
                  f"16:9, premium broadcast motion-graphics style, electric cyan + red brand palette. "
                  f"{_BG_NEGATIVE}")
        tag = f"libgen{have + made}"
        still = await asyncio.to_thread(_flux_sync, prompt)
        if not still:
            continue
        if not await asyncio.to_thread(_bg_is_clean, still, tag):
            BUS.emit("editor-agent", "bg.reject", f"library clip had text-slop — rejected", stage="assets")
            continue
        clip_url = await asyncio.to_thread(_wan_i2v_sync, still, tag)
        if not clip_url:
            continue
        # persist the clip into the library so it's reused forever
        dest = _BG_LIB_DIR / f"broll_{have + made:02d}.mp4"
        try:
            if isinstance(clip_url, str) and clip_url.startswith("http"):
                await asyncio.to_thread(_download, clip_url, str(dest), 90)
            else:  # already a local /agenticnews-assets path (now under _scratch/)
                src = ASSETS / str(clip_url).removeprefix("/agenticnews-assets/")
                if src.exists():
                    await asyncio.to_thread(lambda: dest.write_bytes(src.read_bytes()))
            if dest.exists() and dest.stat().st_size > 4096:
                made += 1
                BUS.emit("editor-agent", "bg.cache", f"cached b-roll clip {dest.name} (library now {have+made})", stage="assets")
        except Exception as e:
            BUS.emit("editor-agent", "error", f"bg cache failed (non-fatal): {e!r}"[:120], stage="assets")
    return made


async def _animated_bg(ep_id, n=3):
    """Return n brand-themed animated b-roll backgrounds, served from the CACHED LIBRARY. Only
    generates new clips when the library is short — so steady-state cost is ~0 video-gen calls per
    episode instead of ~6. Rotates the subset per episode so segments don't all share one clip."""
    import services.abn_memory as _m
    lib = _bg_library()
    # lazily top up the library toward the minimum (cheap once full)
    if len(lib) < _BG_LIB_MIN:
        await _grow_bg_library(_BG_LIB_MIN - len(lib))
        lib = _bg_library()
    if not lib:
        return []  # nothing cached yet AND generation failed → static cards (graceful)
    # rotate which cached clips this episode uses, so consecutive episodes look different.
    # CRITICAL: only ever return clips that EXIST ON DISK RIGHT NOW. Returning a target index that
    # hadn't finished generating yet (a race with the background library fill) put a non-existent
    # broll_NN.mp4 in the timeline → Remotion 404 → the WHOLE render died silently. Re-stat here and
    # verify each pick exists before handing it to the timeline.
    try:
        rot = int(_m.stats().get("episodes", 0))
    except Exception:
        rot = 0
    picks, k = [], 0
    while len(picks) < min(n, len(lib)) and k < len(lib) * 2:
        p = lib[(rot + k) % len(lib)]
        if p.exists() and p.stat().st_size > 4096 and p not in picks:
            picks.append(p)
        k += 1
    return [f"/agenticnews-assets/broll_library/{p.name}" for p in picks]


def _bg_is_clean(still_url, name) -> bool:
    """Asset SELF-REVIEW gate. Download the Flux still, OCR it; if it contains real word-like text
    runs, it's the fake-glyph slop John flagged → reject. Abstract motion has no readable text, so
    a clean bg OCRs to near-nothing. Best-effort: if OCR is unavailable, don't block (return True)."""
    import shutil, subprocess, urllib.request, re
    try:
        # throwaway OCR probe (name is 'libgenN', not episode-scoped) -> _scratch/ via the gateway
        # chokepoint, so the probe's write path is validated/reapable like every other asset write.
        p = _cross_scratch_path(f"{name}_still.png")
        if not _download(still_url, str(p), timeout=60):
            return True  # OCR check can't run on a failed download — don't block (matches prior behavior)
        if not shutil.which("tesseract"):
            return True  # no OCR available — don't block the pipeline
        r = subprocess.run(["tesseract", str(p), "-", "--psm", "11"],
                           capture_output=True, text=True, timeout=30)
        txt = re.sub(r'[^A-Za-z]', '', r.stdout or "")
        # a clean abstract bg yields a few stray chars at most; a slop bg yields dozens of
        # glyph-runs that OCR reads as gibberish words. >24 alpha chars = text-like slop.
        return len(txt) <= 24
    except Exception:
        return True  # never let the review step itself break the render


def _thumb_hook(thumb_spec, lead_title):
    """Extract a clean 2-4 word UPPERCASE hook for the thumbnail. Never leak spec labels like 'TEXT:'."""
    import re
    # The TITLE is the reliable hook source. Only use the spec if it has an explicit quoted hook —
    # spec prose like "dark neon, related to X" describes the IMAGE, not the overlay text.
    cand = ""
    if thumb_spec:
        # the thumbnailer expert emits 'HOOK: <2-4 words>' (also accept Text:/Overlay text:) as its first line
        m = re.search(r'(?:hook|overlay text|thumbnail text|text)\s*[:\-]\s*["\']?([^\n"\']{2,30})', thumb_spec, re.I)
        if m:
            cand = m.group(1)
    if not cand:
        cand = lead_title or "agentic build"
    # meaty part of the title: drop "ask hn:/show hn:" prefixes and any clause after — – or :
    cand = re.split(r'\s+[—–:]\s+', cand)[0]
    cand = re.sub(r'^\s*(ask hn|show hn|why|the|how|a|an|is|are|this|that)\b[:\s]*', '', cand.strip(), flags=re.I)
    cand = re.sub(r'[^A-Za-z0-9 \-$%]', '', cand).strip().upper()
    # whole words only, up to ~22 chars — never truncate mid-word
    hook, acc = [], 0
    for w in [w for w in cand.split() if w][:4]:
        if acc + len(w) + 1 > 22 and hook:
            break
        hook.append(w); acc += len(w) + 1
    return " ".join(hook) or "AGENTIC BUILD"


async def _thumbnail(ep_id, lead_title, thumb_spec):
    """Flux generates a cinematic dev background; ImageMagick overlays the bold hook text."""
    # the thumbnailer expert spec drives the image prompt; derive a clean bg prompt
    bg_prompt = (f"YouTube thumbnail background for an AI/dev channel, dark tech aesthetic, deep navy to "
                 f"near-black gradient, a glowing terminal or code editor with abstract neural lines, "
                 f"electric cyan and red accents, cinematic lighting, high contrast, depth of field, "
                 f"NO text, NO words, professional, related to: {lead_title[:80]}")
    bg_url = await asyncio.to_thread(_flux_sync, bg_prompt)
    bg = asset_path(ep_id, "thumb_bg")
    out = asset_path(ep_id, "thumb")
    if bg_url:
        try:
            _download(bg_url, str(bg), timeout=60)
        except Exception:
            bg = None
    if not (bg and bg.exists()):
        return None  # no slop fallback — if Flux fails, no thumbnail rather than a powerpoint
    # overlay: bold 2-4 word hook. Derive a CLEAN punchy phrase, never scrape spec prose.
    text = _thumb_hook(thumb_spec, lead_title)
    font = str(Path(__file__).resolve().parent.parent / "fonts" / "TikTokSans16pt-Black.ttf")
    if not Path(font).exists():
        font = str(Path(__file__).resolve().parent.parent / "fonts" / "Montserrat-ExtraBold.ttf")
    wrapped = _wrap(text, 12)
    cmd = (
        f'magick {shlex.quote(str(bg))} -resize 1280x720^ -gravity center -extent 1280x720 '
        f'-brightness-contrast -8x12 '
        f'\\( -size 1280x300 gradient:none-rgba\\(0,0,0,0.78\\) \\) -gravity south -composite '
        f'-font {shlex.quote(font)} -gravity southwest -interline-spacing -10 '
        f'-fill black -annotate +58+118 {shlex.quote(wrapped)} '
        f'-fill white -pointsize 104 -annotate +54+122 {shlex.quote(wrapped)} '
        f'-fill "#22d3ee" -pointsize 24 -gravity southeast -annotate +44+44 "AGENTICBUILDERNEWS" '
        f'{shlex.quote(str(out))}'
    )
    code, log = await _sh(cmd, timeout=60)
    if code == 0 and out.exists():
        return _asset_url(out)
    return None


def _wrap(text, per_line=14):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > per_line and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])


# ---------------- REMOTION COMPOSITOR (replaces the screenshot-slideshow assemble) ----------------
REMOTION_DIR = Path(__file__).resolve().parent.parent / "yt-pipeline" / "remotion"

_KB_PRESETS = [
    (1.00, 1.10, 0.50, 0.50, 0.52, 0.48, "easeOut"),
    (1.05, 1.16, 0.25, 0.25, 0.27, 0.27, "easeInOut"),
    (1.12, 1.02, 0.75, 0.25, 0.73, 0.27, "easeOut"),
    (1.00, 1.12, 0.30, 0.55, 0.32, 0.53, "easeIn"),
    (1.08, 1.18, 0.70, 0.55, 0.68, 0.57, "easeInOut"),
    (1.00, 1.08, 0.50, 0.75, 0.50, 0.73, "easeOut"),
    (1.15, 1.04, 0.50, 0.50, 0.50, 0.50, "easeOut"),
    (1.00, 1.06, 0.60, 0.40, 0.58, 0.42, "linear"),
]

# Ken-Burns MOVE LIBRARY — each entry is a directionally distinct camera move so consecutive shots
# never repeat the same gesture (the "all pans go the same way / same slow push-in" repetition John
# flagged). Tagged by intent so the picker can alternate push-IN vs pull-OUT vs lateral DRIFT and
# never serve two moves with the same direction back-to-back.
#  dir: 'in' (zoom in) | 'out' (zoom out) | 'pan' (hold scale, slide framing)
#  fields: (startScale, endScale, startX, startY, endX, endY, easing, dir)
_KB_MOVES = [
    (1.00, 1.12, 0.50, 0.50, 0.52, 0.46, "easeOut",   "in"),    # straight push-in, slight rise
    (1.04, 1.18, 0.30, 0.32, 0.34, 0.30, "easeInOut", "in"),    # push-in toward top-left
    (1.03, 1.16, 0.70, 0.34, 0.66, 0.32, "easeInOut", "in"),    # push-in toward top-right
    (1.05, 1.20, 0.40, 0.70, 0.42, 0.66, "easeIn",    "in"),    # push-in toward bottom
    (1.18, 1.02, 0.50, 0.50, 0.50, 0.52, "easeOut",   "out"),   # pull-back reveal, center
    (1.20, 1.04, 0.66, 0.34, 0.62, 0.38, "easeInOut", "out"),   # pull-back from top-right
    (1.16, 1.03, 0.34, 0.66, 0.38, 0.62, "easeInOut", "out"),   # pull-back from bottom-left
    (1.08, 1.08, 0.28, 0.46, 0.70, 0.50, "linear",    "pan"),   # lateral drift left→right
    (1.08, 1.08, 0.72, 0.50, 0.30, 0.46, "linear",    "pan"),   # lateral drift right→left
    (1.10, 1.10, 0.50, 0.30, 0.50, 0.70, "easeInOut", "pan"),   # vertical tilt up→down
    (1.10, 1.10, 0.50, 0.70, 0.50, 0.30, "easeInOut", "pan"),   # vertical tilt down→up
    (1.06, 1.15, 0.32, 0.66, 0.66, 0.34, "easeOut",   "in"),    # diagonal push-in across frame
]


def _kb_picker(seed=0):
    """Return a stateful closure that hands out a fresh Ken-Burns move on each call, guaranteeing:
      • no two consecutive shots use the same move, and
      • the zoom DIRECTION alternates (in → out/pan → in …) so the whole segment doesn't feel like one
        long push-in. `seed` (segment index) rotates the starting point so segments differ from each
        other and the episode doesn't look templated."""
    order = list(range(len(_KB_MOVES)))
    state = {"i": seed % len(_KB_MOVES), "last_dir": None}

    def pick():
        n = len(order)
        chosen = None
        # walk the rotation looking for a move whose direction differs from the previous shot's
        for step in range(n):
            cand = order[(state["i"] + step) % n]
            if _KB_MOVES[cand][7] != state["last_dir"]:
                chosen = cand
                state["i"] = (state["i"] + step + 1) % n
                break
        if chosen is None:  # everything matched (shouldn't happen with 3 dirs) — just advance
            chosen = order[state["i"]]
            state["i"] = (state["i"] + 1) % n
        m = _KB_MOVES[chosen]
        state["last_dir"] = m[7]
        return {"startScale": m[0], "endScale": m[1], "startX": m[2], "startY": m[3],
                "endX": m[4], "endY": m[5], "easing": m[6]}

    return pick
# brand palette (matches the broadcast logo): cyan wordmark + red anvil + silver. Cyan is the primary
# accent; red is the high-energy pop; silver/blue round it out. Keyword pops cycle these on-brand colors.
BRAND_CYAN = "#7FD2FF"
BRAND_RED = "#FF2B2F"
_ACCENTS = [BRAND_CYAN, BRAND_RED, "#F4F7FB", "#FFCD38", "#16A7FF"]
_MODEL_RE = re.compile(r'\b(GPT-?[\w.]+|Claude[\s-][\w.-]+|Gemini[\s\w.]*|Llama[\s\d.]*|Mistral[\s\w]*|'
                       r'Grok[\s\d.]*|Qwen[\s\d.]*|DeepSeek[\s\w-]*|Codex|Sora|MCP|RAG|API)\b', re.I)
_NUM_RE = re.compile(r'(\$[\d,.]+[BMK]?|[\d,.]+[BMK]\b|[\d.]+[×x]|[\d.]+%|\d+[Kk]\s*context|\d+\s*gig\w*)', re.I)
_ORG_RE = re.compile(r'\b(OpenAI|Anthropic|Google|DeepMind|Meta|Microsoft|NVIDIA|xAI|Mistral|Perplexity)\b', re.I)


def _extract_keywords(script, words, tool_name=None, seg_duration=0.0):
    """NER-lite: tool/repo names, model names, numbers, orgs → resolved to word-timestamps. Top 5.

    When Whisper alignment failed (words empty), we FALL BACK to even time-distribution so keyword
    pops STILL fire — the overlay components are good; they just need timings. A bare video with no
    pops (the 'overlays severely lacking' defect) happened whenever alignment was missing."""
    cands = []
    have_words = bool(words)
    def add(text, typ, pri):
        tgt = re.sub(r'[^\w\s.-]', '', text.lower()).strip()
        if not tgt:
            return
        if have_words:
            for w in words:
                wc = re.sub(r'[^\w\s.-]', '', (w.get("w") or "").lower()).strip()
                if tgt and (tgt in wc or wc in tgt):
                    cands.append({"text": text.strip(), "s": w.get("s", 0), "e": w.get("e", 0), "type": typ, "pri": pri})
                    return
        else:
            # NO alignment — record the candidate with a placeholder time; we space them out below.
            cands.append({"text": text.strip(), "s": -1.0, "e": -1.0, "type": typ, "pri": pri})
    # the TOOL NAME is the most important pop for this niche — lowercase/hyphenated repo names the
    # model/org regexes miss. Pull it from the segment title (most reliable source) + script mentions.
    if tool_name:
        # the tool name is everything before the title separator (— or :), KEEPING internal hyphens
        # so "locally-uncensored" stays whole instead of splitting to the common word "locally".
        full = re.split(r'\s+[—–:]\s+|\s+[—–]\s+', tool_name.strip())[0].strip()
        full = re.split(r'\s+', full)[0]  # first space-token, but hyphens preserved
        if len(full) > 2:
            add(full, "tool", 13)                       # prefer the full hyphenated name
            if "-" in full:                              # fall back to the distinctive last part, not the common first
                parts = full.split("-")
                tail = max(parts, key=len)               # longest part is usually the distinctive one
                if len(tail) > 3 and tail != full.split("-")[0]:
                    add(tail, "tool", 12)
    for m in re.finditer(r'\b([a-z][a-z0-9]*(?:-[a-z0-9]+){1,3})\b', script):  # hyphenated tool names
        if len(m.group(0)) > 4: add(m.group(0), "tool", 11)
    for m in _MODEL_RE.finditer(script): add(m.group(0), "model", 10)
    for m in _NUM_RE.finditer(script): add(m.group(0), "number", 8)
    for m in _ORG_RE.finditer(script): add(m.group(0), "noun", 5)
    seen, out = set(), []
    for c in sorted(cands, key=lambda x: x["s"]):
        k = c["text"].lower()
        if k not in seen:
            seen.add(k); out.append(c)
    out = sorted(out, key=lambda x: -x["pri"])[:5]
    # FALLBACK TIMING: no Whisper alignment → space the top pops evenly across the segment so
    # they still animate (instead of all sitting at -1 and never appearing). Skip the first/last
    # ~12% so a pop never collides with the segment cut.
    if not have_words and out and seg_duration > 2:
        n = len(out)
        lo, hi = seg_duration * 0.12, seg_duration * 0.88
        step = (hi - lo) / max(1, n)
        for i, c in enumerate(out):
            c["s"] = round(lo + step * i + step * 0.2, 2)
            c["e"] = round(c["s"] + 1.6, 2)
    out.sort(key=lambda x: x["s"])
    for i, c in enumerate(out):
        c["color"] = _ACCENTS[i % len(_ACCENTS)]
    return out


def _chop(t0, t1, target=6.0, max_n=6, lead=0.0):
    """Split [t0,t1] into N sub-shots each ~target seconds, hard-capped so NO sub-shot exceeds ~8s
    (the editing rule). Returns list of (startSec, endSec, clipStartSec) where clipStartSec carries the
    continuous source offset (+lead-in to skip warm-up frames). Rounds N UP near the cap so a long beat
    is never held on one frame."""
    span = max(0.0, round(t1 - t0, 2))
    if span <= 0:
        return []
    import math
    n = max(1, min(max_n, round(span / target)))
    if span / n > 8.0:                     # never let a single sub-shot run past ~8s
        # The 8s rule is a HARD editing rule and wins over the soft max_n preference: re-clamping
        # back under max_n here would silently re-create the >8s shot we just rejected (e.g. a 50s
        # span at max_n=6 → 8.33s/shot). Let the cap recompute raise N above max_n when they conflict.
        n = math.ceil(span / 7.0)
    slot = span / n
    out = []
    for j in range(n):
        s = round(t0 + j * slot, 2)
        e = round(t0 + (j + 1) * slot, 2) if j < n - 1 else round(t1, 2)
        out.append((s, e, round(lead + j * slot, 2)))
    # NO MICRO-BEATS (John: "0.7 sec screen recording... way too short"): a tail fragment under
    # 3.5s reads as a glitch, not a beat — merge it into the previous window instead.
    if len(out) > 1 and (out[-1][1] - out[-1][0]) < 3.5:
        s, _, off = out[-2]
        out[-2:] = [(s, out[-1][1], off)]
    return out


def _hi_box(kw):
    """Highlight/lower-third box for a keyword pop, positioned by type so number/model/tool pops don't
    all stack in one spot (variety in WHERE emphasis lands)."""
    typ = kw.get("type")
    box = ({"x": .06, "y": .55, "w": .58, "h": .12} if typ == "number"
           else {"x": .06, "y": .22, "w": .64, "h": .10} if typ == "model"
           else {"x": .10, "y": .38, "w": .54, "h": .09})
    return {**box, "color": kw["color"], "opacity": .85, "borderWidth": 3, "label": kw["text"]}


def _plan_shots(duration, screenshot, card, words, keywords, source_url, demo=None, ui=None, seg_index=0,
                kinetic=None, bgs=None):
    """Mixed-media: live UI scroll → Ken-Burns artifact punch-ins → live CODE DEMO. New visual every 4-7s.

    Dynamism rules (the variety pass John asked for):
      • ONE shared Ken-Burns picker per segment → every sub-shot gets a distinct, direction-alternating
        move (push-in / pull-out / lateral-pan never repeat back-to-back). No more identical slow zooms.
      • Per-segment RHYTHM variation seeded by seg_index → the UI/demo split, the number of artifact
        windows, and the card position all shift between segments so no two segments look templated.
      • Keyword pops attach a tight highlight box to WHICHEVER beat the keyword is spoken in (UI,
        artifact, or demo) — emphasis lands on the spoken word, not only when it falls in the middle.
      • Hard 4–7s pacing (≤8s) on every beat via _chop."""
    # existence checks must resolve the FULL subpath (assets live in per-episode subdirs now);
    # flattening to basename would read every migrated asset as missing and silently blank the segment.
    if screenshot and not _resolve_asset(screenshot).exists():
        screenshot = None
    if not (card and _resolve_asset(card).exists()):
        card = screenshot
    has_demo = demo and _resolve_asset(demo).exists()
    has_ui = ui and _resolve_asset(ui).exists()
    pick = _kb_picker(seed=seg_index)  # shared across UI + artifacts + demo so moves never repeat

    # RHYTHM VARIATION: nudge the structural split per segment so the episode isn't templated.
    # MIX REBALANCE (John, 06-09): "static slideshows" — he wants MORE live screen recording
    # (browser + terminal), with designed cards as punctuation, not the main course. The earlier
    # anti-slop pass over-corrected UI capture down to a 3s cutaway, which left Ken-Burns stills
    # carrying most of the runtime. New split: browser capture gets a real ~22-30% share again
    # (it's a SCREEN RECORDING — exactly the texture he asked for), demo keeps its block, and the
    # artifact middle shrinks to cards-as-accents.
    _v2_on = (_V2_VISUALS and _USE_V2_VISUALS)
    if _v2_on and has_ui and duration > 6:
        ui_frac = (0.26, 0.30, 0.22)[seg_index % 3]
    else:
        ui_frac = (0.40, 0.45, 0.50)[seg_index % 3]
    demo_frac = (0.68, 0.72, 0.70)[seg_index % 3]
    ui_end = round(duration * ui_frac, 2) if has_ui else 0.0
    demo_start = round(duration * demo_frac, 2) if has_demo else duration
    artifact_start = ui_end
    artifact_dur = max(0.0, demo_start - artifact_start)

    # quick lookup: does a keyword fall inside [s,e)?  used to attach pops to ANY beat.
    def kw_in(s, e):
        for kw in keywords:
            if s <= kw["s"] < e:
                return kw
        return None

    shots = []
    # ── UI capture (open) ─────────────────────────────────────────────────────────────────────────
    if has_ui:
        UI_LEADIN = 2.0  # skip the page-load white frames at the very start of the recording
        for j, (us, ue, off) in enumerate(_chop(0.0, ui_end, target=6.0, max_n=6, lead=UI_LEADIN)):
            shot = {"id": f"ui{j}", "type": "broll", "src": _disk(ui), "startSec": us, "endSec": ue,
                    "muteSource": True, "clipStartSec": off, "kenBurns": pick()}
            kw = kw_in(us, ue)
            if kw:
                pass  # keyword highlight boxes 100% DEADED (John, 06-09) — emphasis lives in the karaoke captions
            shots.append(shot)

    # ── KINETIC INSERT beat (seekable-html motion graphics) opens the middle ────────────────────
    # "this is what i want to see more of": when this segment has a rendered kinetic insert, it
    # takes the FIRST middle beat as a full-bleed animated sequence (it carries its own motion —
    # no Ken-Burns, no frame). The remaining middle shrinks accordingly.
    src_domain = re.sub(r'^https?://(www\.)?', '', source_url or "").split("/")[0][:34]
    kin_src = kinetic.get("src") if isinstance(kinetic, dict) else kinetic
    kin_dur = float(kinetic.get("dur", 11.0)) if isinstance(kinetic, dict) else 10.0
    # the insert's payoff lands in its final seconds — run it FULL length or not at all
    # (cutting a zine-slam before the circled payoff word is worse than no insert)
    if kin_src and artifact_dur >= kin_dur + 2.0:
        k_len = round(kin_dur, 2)
        shots.append({"id": "kin0", "type": "kinetic", "src": kin_src, "startSec": round(artifact_start, 2),
                      "endSec": round(artifact_start + k_len, 2), "muteSource": True})
        artifact_start = round(artifact_start + k_len, 2)
        artifact_dur = max(0.0, demo_start - artifact_start)

    # ── Artifact middle (Ken-Burns on stills/cards) ──────────────────────────────────────────────
    # PACING LAW (John, 06-09): beats run 4-6s, hard cap ~7s. The old art_cap=5 let a 50s middle
    # become 5×10s static holds — "13 seconds of static fucking bullshit". Window count now scales
    # with duration so slots land ~5s, capped only by sanity (12).
    import math
    n = min(12, max(2, math.ceil(artifact_dur / 5.0))) if artifact_dur > 0 else 0
    if n:
        slot = artifact_dur / n
        windows = [(round(artifact_start + i * slot, 2), round(artifact_start + (i + 1) * slot, 2)) for i in range(n)]
        windows[-1] = (windows[-1][0], round(demo_start, 2))
        # CARD PLACEMENT varies per segment: center-ish on some, near-end on others — not always slot 0.
        card_idx = (n // 2) if (seg_index % 2 == 0) else max(n - 1, 0)
        for i, (s, e) in enumerate(windows):
            is_card = (i == card_idx)
            src = card if is_card else (screenshot or card)
            # the card gets a gentle still-hold so the title reads; everything else gets a live move
            kb = ({"startScale": 1.0, "endScale": 1.04, "startX": .5, "startY": .5,
                   "endX": .5, "endY": .5, "easing": "easeOut"} if is_card else pick())
            shot = {"id": f"shot{i}", "type": "artifact", "src": _disk(src), "startSec": s, "endSec": e, "kenBurns": kb}
            # LAYERED THEATRICS: a bare source SCREENSHOT never runs full-bleed-static anymore —
            # it renders as a floating PRESS CLIPPING (framed, source chip) over an animated
            # brand background. Designed cards keep full-bleed (they ARE designed frames).
            if not is_card and screenshot and src == screenshot and bgs:
                shot["frame"] = "clip"
                shot["bgSrc"] = bgs[(seg_index + i) % len(bgs)]
                shot["sourceChip"] = src_domain
            shots.append(shot)

    # ── Live code demo (close) ────────────────────────────────────────────────────────────────────
    if has_demo:
        # With the trimmed ceremony (short header + ls), real `ls` output appears by ~2.5s. Lead-in
        # of 2.5s opens the demo on actual repo content, not the typing warm-up.
        DEMO_LEADIN = 2.5
        for j, (ds, de, off) in enumerate(_chop(demo_start, round(duration, 2), target=6.0, max_n=4, lead=DEMO_LEADIN)):
            m = pick()
            # demo is a terminal — keep moves subtler (scale toward 1.0) but still rotate direction
            kb = {**m, "startScale": round(1.0 + (m["startScale"] - 1.0) * 0.5, 3),
                  "endScale": round(1.0 + (m["endScale"] - 1.0) * 0.5, 3)}
            shot = {"id": f"demo{j}", "type": "broll", "src": _disk(demo), "startSec": ds, "endSec": de,
                    "muteSource": True, "clipStartSec": off, "kenBurns": kb}
            kw = kw_in(ds, de)
            if kw:
                pass  # keyword highlight boxes 100% DEADED (John, 06-09) — emphasis lives in the karaoke captions
            shots.append(shot)
    return shots


def _disk(url_path):
    # Return the HTTP-served path (the lab mounts /agenticnews-assets/); Remotion
    # headless fetches over HTTP, which avoids the file:// read failures.
    if not url_path: return None
    s = str(url_path)
    # PRESERVE SUBDIRECTORIES. Flattening to just Path(...).name dropped the broll_library/
    # subdir → every cached b-roll 404'd → the whole Remotion render died. If the url already
    # carries the mount prefix, return it as-is; if it names a known subdir, keep it; else flatten.
    if s.startswith("/agenticnews-assets/"):
        return s
    name = Path(s).name
    parent = Path(s).parent.name
    if parent and parent not in ("", ".", "agenticnews_assets") and parent != ASSETS.name:
        return f"/agenticnews-assets/{parent}/{name}"
    return f"/agenticnews-assets/{name}"


def _hook_line(cold_open_text: str) -> str:
    """Pull the PUNCHIEST fragment from the cold-open VO for the first-5s hook card — the payload,
    not the throat-clearing. Prefers the clause carrying a stat, stripped of lead-in filler so it
    reads big and lands fast ('one million token context', not 'Anthropic just shipped a model with')."""
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', (cold_open_text or "").strip()) if s.strip()]
    if not sents:
        return "THE AI STORY YOU MISSED"
    stat = next((s for s in sents if re.search(r'\$?\d|\b(million|billion|x|percent|%)\b', s, re.I)), None)
    # SKIP a one-word/too-short leading sentence ('Scout.') — it makes a useless 1-word hook ('SCOUT').
    # Pick the first SUBSTANTIVE sentence (>=4 words) for the most important frame in the video.
    substantive = next((s for s in sents if len(s.split()) >= 4), None)
    pick = stat or substantive or sents[0]
    # Strip the subject+verb lead-in so the hook opens on the PAYLOAD ("one million token context", not
    # "Anthropic just shipped a model with a one million token context"). BUT only when stripping yields
    # a STRONG payload — otherwise we throw away a newsworthy named subject and leave a headless fragment.
    # Caught on a real hook: "Microsoft's Scout agent just raised the bar for AI-powered hacking" became
    # "BAR FOR AI-POWERED HACKING" (lost the subject — who raised it?). So: only strip if what FOLLOWS the
    # verb starts with a number/stat OR an article+noun+'with' payload; if it would leave a bare
    # preposition fragment ('the bar for...', 'a way to...'), keep the original (the subject IS the hook).
    _stripped = re.sub(r'^.*?\b(shipped|launched|released|dropped|unveiled|announced|built|made|hit|reached|'
                       r'raised|now has|just got|added|introduced)\b\s*', '', pick, count=1, flags=re.I)
    _payload_ok = bool(re.match(r'^\s*(\$?\d|a |an |the )?(\w+\s+)?(with |that has |featuring )', _stripped, re.I) or
                       re.match(r'^\s*\$?\d', _stripped))
    _leaves_prep_fragment = bool(re.match(r'^\s*(the |a |an )?\w+\s+(for|to|of|in|on|with|at|by)\b', _stripped, re.I)) \
        and not re.search(r'\$?\d|\b(million|billion|percent|%)\b', _stripped, re.I)
    if _stripped and _payload_ok and not _leaves_prep_fragment:
        pick = _stripped
        pick = re.sub(r'^(a |an |the |its |their )?(model |tool |startup |company )?(with |that has |featuring )?(a |an |the )?',
                      '', pick, count=1, flags=re.I) or pick
    # else: keep the full sentence — its named subject is the clickable hook
    # normalize joiners to SPACES before stripping punctuation, else 'chatbots—they're' fuses into
    # 'CHATBOTSTHEYRE' (a mashed word on the hook card — caught on a real render). em/en-dash and
    # slashes become spaces; apostrophes are dropped in-place ("they're"->"theyre", not "they re").
    # CUT at the first CLAUSE BOUNDARY so the hook is a complete thought, not a sentence fragment.
    # 'prototypes — they're becoming X' must cut at 'prototypes', not run into the next clause and
    # leave a dangling "...PROTOTYPES THEYRE" (caught on a real hook card). The em-dash/comma already
    # marks the boundary; also break before a clause-starting pronoun+verb.
    pick = re.split(r'\s*[—–,;]\s*', pick)[0]
    # Cut a dangling clause that starts with a pronoun+verb ('...prototypes they're becoming X'). CRITICAL:
    # do NOT strip bare possessive 'its' — 'Anthropic open-sourced ITS entire agent evaluation harness' is
    # one clause where 'its' is a determiner, and stripping it left a 2-word hook 'ANTHROPIC OPEN-SOURCED'
    # (caught stress-testing real cold-opens). Only strip CONTRACTIONS (they're/it's/that's/here's) and
    # relative pronouns (which/who) that genuinely begin a new clause — never possessive its/their.
    # NOTE: 'it'?s' would match bare possessive 'its' (the '?' makes the apostrophe optional) — that ate
    # 'its entire agent evaluation harness' down to a 2-word hook. Match the CONTRACTION 'it's' ONLY
    # (require the apostrophe via "it['’]s"); never the possessive determiner 'its'.
    pick = re.sub(r"\b(they['’]re|theyre|it['’]s|that['’]s|thats|here['’]s|heres|which|who)\b.*$", "", pick, flags=re.I).strip() or pick
    # normalize curly apostrophes to straight; KEEP a possessive 's (Microsoft's -> MICROSOFT'S, not
    # MICROSOFTS — caught on a real hook), drop only OTHER apostrophes (contractions like don't->dont,
    # stray quotes) so words don't fuse or carry a stray mark.
    pick = pick.replace("’", "'")
    pick = re.sub(r"'(?!s\b)", "", pick)
    # split on em/en-dash and slash only — NOT hyphens, which belong to model names (GPT-5, Llama-4,
    # Claude-3). A '[—–/\-]' replace turned 'GPT-5' into 'GPT 5' (caught on a real hook). Keep hyphens.
    pick = re.sub(r'\s*[—–]\s*|\s*/\s*', ' ', pick)
    # keep hyphens (model names) + the possessive apostrophe; drop other punct incl. trailing periods
    words = re.sub(r"[^\w\s%$'-]", '', pick).split()[:8]
    # don't END the hook on a dangling conjunction/preposition/article — "...OUT OF LABS AND" leaves
    # the viewer hanging on a connective instead of a punch. Trim trailing weak words.
    _WEAK_TAIL = {"and", "or", "but", "the", "a", "an", "of", "to", "with", "for", "in", "on", "at",
                  "by", "from", "as", "is", "are", "that", "this", "its", "their", "into", "than",
                  "theyre", "thats", "heres", "just", "no", "not", "still", "now",
                  # interrogative/relative lead-ins that DANGLE when the 8-word cap cuts before the
                  # payoff ('...JUST SHOWED WHY' / '...EXPLAINS HOW' — caught on a real hook card).
                  "why", "how", "what", "when", "where", "whether", "if", "because", "so",
                  # copulas/auxiliaries/adverbs that leave the hook hanging mid-predicate when the cap
                  # cuts right after them ('...MUST NOW BE', '...IS ENTIRELY', '...WILL') — caught while
                  # fixing the AI-POWERED dangle: trimming the modifier exposed a weak copula tail.
                  "be", "been", "being", "was", "were", "will", "would", "can", "could", "should",
                  "must", "may", "might", "has", "have", "had", "very", "entirely", "fundamentally",
                  "really", "actually", "basically", "literally", "more", "most", "much", "also"}
    # also a trailing HYPHENATED MODIFIER that obviously needs a noun ('AI-POWERED', 'CLOUD-BASED',
    # 'OPEN-SOURCE') — it dangles the same way ('...WHY AI-POWERED' has no subject). Caught on the
    # most-important frame (the first-5s hook). Trim it, then re-trim any weak word it exposes.
    _DANGLE_MOD = re.compile(r"-(powered|based|driven|ready|grade|native|first|backed|scale|level|"
                             r"source|sized|focused|enabled|fueled|class)$", re.I)
    while words and (words[-1].lower() in _WEAK_TAIL or _DANGLE_MOD.search(words[-1])):
        words.pop()
    line = " ".join(words).strip()
    return (line or pick[:48]).upper()


def _diagram_steps(text: str) -> list:
    """Extract real short mechanism steps for a diagram card. Filters out CTAs/questions/filler
    (e.g. 'Want to see how it works in real') so a diagram never shows a junk single box. Returns
    [] if there aren't clean steps (caller then falls back to a quote card)."""
    raw = [s.strip() for s in re.split(r'[.;]|\bthen\b|\bnext\b|->|→', text) if s.strip()]
    steps = []
    for s in raw:
        sl = s.lower()
        # skip CTAs, questions, meta-filler — not mechanism steps
        if "?" in s or re.search(r'\b(want to|subscribe|comment|let me know|stay tuned|in real|'
                                 r'check out|link below|honestly|basically|imagine)\b', sl):
            continue
        # skip NAMING clauses ('it's called X', 'it is named Y') — a diagram shows HOW it works, not
        # what it's called (caught on a real card: 'It's called the Defending Code Ref').
        if re.search(r"^(it'?s |it is |this is )?(called|named|known as|dubbed)\b", sl):
            continue
        words = s.split()
        if 2 <= len(words) <= 9:                 # a step is a short action phrase
            # clip to <=34 chars at a WORD boundary — never mid-word ('Reference'->'Ref', caught on a card)
            step = s if len(s) <= 34 else s[:34].rsplit(" ", 1)[0]
            steps.append(step.strip())
        if len(steps) >= 4:
            break
    return steps


def _statement(text: str) -> str:
    """A short bold STATEMENT (≤8 words) from a scene for a hook-style statement card — the quote-cap
    rotation target + the vs/diagram no-data fallback. Strips filler lead-ins + trailing punctuation
    so it reads as a punchy complete thought, not a mid-sentence fragment ('AND THAT IS WHY IT...')."""
    s = re.split(r'\s*[—–,;]\s*', (text or "").strip())[0]
    s = re.sub(r"['’]", "", s)
    # strip filler OPENERS so the statement leads on substance, not a connective/setup phrase
    s = re.sub(r'^\s*(and|but|so|then|also|plus)\b\s+', '', s, flags=re.I)
    s = re.sub(r'^\s*(this matters because|that is why|thats why|here is why|heres why|the point is|'
               r'what this means is|in other words|basically|honestly|the thing is)\b\s*', '', s, flags=re.I)
    s = re.sub(r'^\s*(it is|its|it|this is|this|that is|that)\b\s+', '', s, flags=re.I)
    words = re.sub(r'[^\w\s%$]', '', s).split()[:8]    # drop '.' too — no trailing period on a card
    _WEAK = {"and", "or", "but", "the", "a", "an", "of", "to", "with", "for", "in", "on", "is", "are",
             "that", "this", "its", "their", "than", "just", "no", "not", "so", "as", "it", "you"}
    while words and words[-1].lower() in _WEAK:
        words.pop()
    return (" ".join(words) or re.sub(r'[.\s]+$', '', s)[:48]).upper()


# Parent company ↔ its products — a vs card pitting one against the other is nonsense ('iPhone vs Apple').
_OWNS = {
    "microsoft": {"mai-code", "mai code", "copilot", "github copilot", "phi", "azure", "vs code", "vscode", "bing"},
    "google":    {"gemini", "deepmind", "vertex", "bard", "tensorflow", "android"},
    "openai":    {"gpt", "chatgpt", "codex", "sora", "dall-e", "dalle", "whisper"},
    "anthropic": {"claude", "mythos"},
    "meta":      {"llama", "pytorch", "react"},
    "amazon":    {"aws", "bedrock", "titan", "q"},
    "apple":     {"mlx", "siri", "core ml", "coreml"},
}


def _entity_family(name: str) -> str:
    """Map a tool/company name to its parent family ('mai-code'->'microsoft', 'gemini'->'google',
    'microsoft'->'microsoft'). Returns the name itself if it's not in a known family."""
    n = (name or "").strip().lower()
    for parent, kids in _OWNS.items():
        if n == parent or any(k in n for k in kids):
            return parent
    return n


def _same_entity(a: str, b: str) -> bool:
    """True if a and b are the same company/product, one contains the other, OR they belong to the
    same parent family — so a 'vs' between them is meaningless (Microsoft vs MAI-Code, GPT vs OpenAI)."""
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    return _entity_family(x) == _entity_family(y)


def _quote_text(text: str) -> str:
    """A clean COMPLETE quote for a quote card — never truncated mid-sentence, no em-dash pile-ups.
    Takes the first 1-2 whole sentences that fit, dropping a trailing partial."""
    text = text.replace("—", ", ").replace("–", ", ")
    sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
    out = ""
    for s in sents:
        if len(out) + len(s) + 1 > 100 and out:   # quote card holds ~4 short lines ≈ 100 chars
            break
        out = (out + " " + s).strip()
    out = out or (sents[0] if sents else text)[:100]
    # strip a filler LEAD-IN so the quote opens on substance, not throat-clearing ('But here's the
    # catch: it's mainly...' -> 'it's mainly...'). Caught on a real quote card.
    out = re.sub(r"^\s*(but|and|so|now|well|look|honestly|the thing is|here'?s the (catch|thing|kicker)|"
                 r"that said|of course|in fact|frankly)\b[\s:,—-]*", "", out, flags=re.I).strip()
    # also drop a leading 'X:' preamble clause if a substantive clause follows it
    m = re.match(r"^[^:]{3,40}:\s+(.{20,})$", out)
    if m:
        out = m.group(1).strip()
    # DROP a leading CONDITIONAL/dependent clause so the quote leads with the PAYOFF, not a dangling
    # setup. 'If you want to build AI tools..., this is the one to watch' -> 'this is the one to watch'
    # (caught on a real card where the fit-trim cut the payoff, leaving the bare 'If you want to...').
    cm = re.match(r"^\s*(if|when|whenever|because|since|while|although|though|unless|as)\b[^,]{6,80},\s+(.{12,})$",
                  out, flags=re.I)
    if cm:
        out = cm.group(2).strip()
    # FIT THE CARD: the quote card wraps ~26 chars/line and shows only 4 lines (~100 chars). A longer
    # quote gets chopped MID-SENTENCE by the renderer ('...the real story is' — caught on a real card).
    # Trim to a COMPLETE phrase that fits: cut at the last clause boundary (comma) under the cap, else
    # the last whole word, and drop any dangling connective so it never ends on 'is/the/and'.
    CAP = 96
    if len(out) > CAP:
        head = out[:CAP]
        cut = head.rfind(", ")
        out = (head[:cut] if cut >= 40 else head[:head.rfind(" ")]).strip().rstrip(",")
        _wk = {"is", "are", "the", "a", "an", "and", "or", "but", "of", "to", "on", "in", "for", "with", "as", "that", "this"}
        ws = out.split()
        while ws and ws[-1].lower() in _wk:
            ws.pop()
        out = " ".join(ws)
    return (out[:1].upper() + out[1:]) if out else (sents[0] if sents else text)[:96]


def _v2_scene_cards(ep_id, seg_index, seg, ep_budget=None):
    """Generate v2 DESIGNED CARDS for a segment's scenes — the anti-slop replacement for the
    blog-screenshot visual. Deconstructs the VO into scenes, picks a shot per scene from the
    format catalog, and renders the designed cards (number/vs/quote/diagram). Returns an ordered
    list of /agenticnews-assets/ urls for the designed frames, or [] if v2 is off/unavailable.

    ep_budget is a mutable dict tracking EPISODE-WIDE card counts so the quote-cap holds across ALL
    segments (the per-segment direct_visuals cap reset each segment → 9/11 quotes in a real render).

    Best-effort: any failure returns [] so the legacy visual still renders (never breaks a render)."""
    if not (_V2_VISUALS and _USE_V2_VISUALS):
        return []
    if ep_budget is None:
        ep_budget = {}
    try:
        from factory.formats import get_format
        from factory.contracts.stages import VideoFormat
        # all current production is roundup-style segments → PULSE catalog
        spec = get_format(VideoFormat.ROUNDUP)
        scenes = tag_scenes(seg.get("script", ""), segment_index=seg_index,
                            is_first_segment=(seg_index == 0), is_last_segment=False)
        shots = direct_visuals(scenes, spec)
        # EPISODE-WIDE quote-cap: direct_visuals caps quotes per-SEGMENT, but across 8 segments the
        # episode can still be ~80% quotes. Enforce an episode budget — cap quotes at ~30% of all
        # scenes (loose 45% left a quote-heavy script still mostly quotes), and rotate the EXCESS
        # through VARIED alternatives (statement/diagram) so it isn't one new monotony.
        ep_budget["scenes"] = ep_budget.get("scenes", 0) + len(scenes)
        ep_budget.setdefault("quotes", 0)
        ep_budget.setdefault("rot", 0)
        _ALT_CYCLE = ("title_card", "diagram", "title_card")   # statement, diagram, statement, ...
        cap = max(1, int(ep_budget["scenes"] * 0.30))           # quotes ≤ ~30% of all scenes seen
        for sh in shots:
            if sh.shot_type == "quote_card":
                if ep_budget["quotes"] >= cap:
                    sh.shot_type = _ALT_CYCLE[ep_budget["rot"] % len(_ALT_CYCLE)]
                    ep_budget["rot"] += 1
                else:
                    ep_budget["quotes"] += 1
        title = seg.get("title", "") or ""
        # clean tool name for the vs-card left side: take the first segment of the title, and if it's
        # a slash-joined list ('Anthropic/OpenAI') keep only the FIRST name so the card reads cleanly.
        tool = re.split(r'\s+[—–:]\s+', title)[0].strip() if title else "this"
        tool = re.split(r'\s*/\s*', tool)[0].strip()[:24] or "this"
        out = []
        # v2 cards are a css-layer asset: write them straight into {ep_id}/css/ by handing cards.py
        # the gateway subdir as its assets_dir. The filename carries only the segment-local slug
        # (the ep_id is already the parent dir), and _asset_url builds the in-schema URL.
        css_dir = asset_path(ep_id, "card", f"s{seg_index}").parent  # -> {ep_id}/css/ (dir created)
        for sc, sh in zip(scenes, shots):
            nm = f"s{seg_index}_v2sc{sc.index}"
            try:
                # FIRST-5-SECONDS HOOK: the very first scene of the episode (seg 0, scene 0) gets a
                # dedicated bold HOOK card built from the cold-open's striking fact — the single most
                # important visual for retention. Distinct, high-energy, not a calm mid-video card.
                if seg_index == 0 and sc.index == 0:
                    hk = _hook_line(sc.text)
                    p = _v2cards.hook_card(hk, nm, css_dir, _FONTS_DIR)
                    out.append(_asset_url(p))
                    continue
                if sh.shot_type == "number_card":
                    h = hero_number(sc.text)
                    if not h:
                        continue
                    p = _v2cards.number_card(h, hero_number_label(sc.text, h), nm, css_dir, _FONTS_DIR)
                elif sh.shot_type == "vs_card":
                    from factory.formats.scenes import comparison_target
                    rival = comparison_target(sc.text)
                    # GUARD the LEFT entity too: 'tool' is the title's first segment, which can be a
                    # verb-phrase fragment ('Use your' -> 'Use your VS SSD' is meaningless — caught on a
                    # real card). A real vs needs a NOUN/entity on the left, not an imperative/fragment.
                    # If the left isn't usable, drop to a statement (same as the rival guard).
                    _tool_bad = (not tool or tool.lower() in ("this", "it", "they")
                                 or bool(re.match(r"^(use|get|make|try|see|build|run|find|how|what|why|when|"
                                                  r"why|your|the|a|an|this|that|these|those)\b", tool.strip(), re.I))
                                 or len(tool.split()) > 3)
                    if _tool_bad:
                        rival = ""   # force the statement fallback below
                    if rival and _same_entity(tool, rival):
                        # NOT a real matchup — the 'rival' is the tool itself or its PARENT company
                        # (caught on a real card: 'MAI-Code-1-Flash VS Microsoft' — Microsoft MAKES
                        # MAI-Code; you can't pit a product against its own maker). Drop to a statement.
                        rival = ""
                    if not rival:
                        # no real competitor named → render a bold STATEMENT card (hook style), not
                        # another quote (vs/diagram falling back to quote was a big quote-skew source).
                        p = _v2cards.hook_card(_statement(sc.text), nm, css_dir, _FONTS_DIR, accent=_v2cards.BRAND_CYAN)
                    else:
                        p = _v2cards.vs_card(tool, rival, nm, css_dir, _FONTS_DIR)
                elif sh.shot_type == "quote_card":
                    _qt = _quote_text(sc.text)
                    # FLOOR: a quote card needs a substantive quote. A tiny fragment ('The catch?') leaves
                    # the card mostly empty (caught on a real card) — render a bold STATEMENT instead.
                    if len(_qt) < 24 or len(_qt.split()) < 4:
                        p = _v2cards.hook_card(_statement(sc.text), nm, css_dir, _FONTS_DIR, accent=_v2cards.BRAND_CYAN)
                    else:
                        p = _v2cards.quote_card(_qt, nm, css_dir, _FONTS_DIR)
                elif sh.shot_type in ("diagram", "diagram_card"):
                    steps = _diagram_steps(sc.text)
                    if len(steps) >= 2:
                        p = _v2cards.diagram_card(f"How {tool} works", steps, nm, css_dir, _FONTS_DIR)
                    else:
                        # no real multi-step mechanism → a bold STATEMENT card, not another quote
                        p = _v2cards.hook_card(_statement(sc.text), nm, css_dir, _FONTS_DIR, accent=_v2cards.BRAND_CYAN)
                elif sh.shot_type in ("title_card", "brand_broll"):
                    # quote-cap rotation target: a bold cyan STATEMENT card (hook generator) — visually
                    # distinct from the centered pull-quote, so the episode isn't a wall of quotes.
                    p = _v2cards.hook_card(_statement(sc.text), nm, css_dir, _FONTS_DIR, accent=_v2cards.BRAND_CYAN)
                else:
                    continue
                out.append(_asset_url(p))
            except Exception as ce:
                BUS.emit("editor-agent", "error", f"v2 card {sh.shot_type} failed (non-fatal): {ce!r}"[:120],
                         episode_id=ep_id)
        if out:
            BUS.emit("editor-agent", "v2.cards", f"seg {seg_index+1}: {len(out)} designed cards (no blog-screenshot slop)",
                     episode_id=ep_id)
        return out
    except Exception as e:
        BUS.emit("editor-agent", "error", f"v2 visuals failed (non-fatal, legacy fallback): {e!r}"[:120],
                 episode_id=ep_id)
        return []


def _build_timeline(ep_id, ep_idx, segments, animated_bg=None):
    fps, total = 30, 0.0
    tsegs = []
    # animated_bg may be a single url (legacy) or a LIST of distinct bgs to rotate through (no repeats)
    bg_list = animated_bg if isinstance(animated_bg, list) else ([animated_bg] if animated_bg else [])
    bg_i = 0
    _ep_card_budget = {}   # EPISODE-WIDE card counts (e.g. quote-cap) shared across all segments
    for seg_index, seg in enumerate(segments):
        kws = _extract_keywords(seg["script"], seg["words"], tool_name=seg.get("title"), seg_duration=seg.get("duration", 0.0))
        shots = _plan_shots(seg["duration"], seg.get("screenshot"), seg["card"], seg["words"], kws, seg["source_url"], seg.get("demo"), seg.get("ui"), seg_index=seg_index,
                            kinetic=seg.get("kinetic"), bgs=bg_list or None)
        # V2 ANTI-SLOP: replace the 'artifact' shots (which were blog/page SCREENSHOTS — the slop)
        # with DESIGNED CARDS from the v2 scene→catalog system. Keeps all the timing/Ken-Burns/
        # highlight logic; only swaps the SOURCE image from a scrolled blog to a designed frame.
        v2_cards = _v2_scene_cards(ep_id, seg_index, seg, ep_budget=_ep_card_budget)
        if v2_cards:
            ci = 0
            for sh in shots:
                # framed press-clipping shots keep their REAL screenshot — swapping in a designed
                # card would put a card inside a clip frame with a bogus source chip
                if sh.get("frame") == "clip":
                    continue
                if sh.get("type") == "artifact":
                    card_url = v2_cards[ci % len(v2_cards)]; ci += 1
                    cf = _resolve_asset(card_url)
                    if cf.exists() and cf.stat().st_size > 1024:
                        sh["src"] = _disk(card_url)
                        # designed cards read best with a gentle hold, not an aggressive Ken-Burns
                        sh["kenBurns"] = {"startScale": 1.0, "endScale": 1.05, "startX": .5,
                                          "startY": .5, "endX": .5, "endY": .5, "easing": "easeOut"}
            # FIRST-5-SECONDS: on the OPENING segment, the HOOK card must be the very FIRST thing on
            # screen (0:00) — not 3s of webpage scroll THEN the hook. Find the hook shot and move it to
            # the front, shifting the brief UI cutaway to after it. The hook is the whole point of the open.
            if seg_index == 0:
                hook_shots = [sh for sh in shots if "hook" in (sh.get("src") or "")]
                if hook_shots:
                    h = hook_shots[0]
                    others = [sh for sh in shots if sh is not h]
                    # rebuild timing so the hook owns 0.0s onward; everything else shifts after it
                    hook_len = max(2.5, h.get("endSec", 3) - h.get("startSec", 0))
                    h["startSec"], h["endSec"] = 0.0, round(hook_len, 2)
                    shots = [h] + others
                    cursor = h["endSec"]
                    for sh in others:
                        d = max(2.0, sh.get("endSec", 0) - sh.get("startSec", 0))
                        sh["startSec"], sh["endSec"] = round(cursor, 2), round(cursor + d, 2)
                        cursor += d
        # MOTION UPGRADE: swap flat title-card 'artifact' shots for an animated brand b-roll — and
        # ROTATE through distinct bgs so we never repeat the same clip across the episode.
        if bg_list:
            cardname = Path(seg["card"]).name if seg.get("card") else None
            for sh in shots:
                if sh.get("type") == "artifact" and cardname and cardname in (sh.get("src") or ""):
                    bg = bg_list[bg_i % len(bg_list)]; bg_i += 1
                    # DEFENSE-IN-DEPTH: only swap in the b-roll if the file ACTUALLY exists on disk.
                    # A missing clip in the timeline = Remotion 404 = the whole render dies silently.
                    # If it's gone, leave the original card shot (always present) — never inject a 404.
                    bgfile = _resolve_asset(bg)  # preserves broll_library/ (and any) subdir
                    if bgfile.exists() and bgfile.stat().st_size > 4096:
                        sh["type"] = "broll"; sh["src"] = _disk(bg); sh["muteSource"] = True
                        sh["clipStartSec"] = 0.5  # skip any i2v warm-up frame
        pops = [{"word": k["text"], "s": k["s"], "atSec": k["s"], "durationSec": min(2.4, k["e"] - k["s"] + 1.5), "color": k["color"]} for k in kws]
        # DON'T POP OVER A DESIGNED CARD: a number/vs/diagram/quote/hook card IS the visual emphasis;
        # a keyword-pop box layered on it is redundant clutter (caught on a real frame: a '4x' pop box
        # stacked on the '4x' number card). Drop pops that fall inside a designed-card shot window.
        _card_windows = [(sh.get("startSec", 0), sh.get("endSec", 0)) for sh in shots
                         if "v2sc" in (sh.get("src") or "")]
        if _card_windows:
            pops = [p for p in pops if not any(cs <= p["s"] < ce for cs, ce in _card_windows)]
        # KEEP THE HOOK CLEAN: on seg 0, suppress keyword-pops only during the OPENING hook window
        # (the first ~12s) so the opening statement stands alone (a 'real-world' highlight box layered
        # on the hook cluttered the most important frame — caught on a real render). Capped at 12s so
        # pops still fire across the rest of the segment (variety preserved).
        if seg_index == 0:
            hook_starts = [sh.get("startSec", 0) for sh in shots if "hook" in (sh.get("src") or "")]
            if hook_starts and min(hook_starts) <= 0.5:        # hook owns the open
                clean_until = 12.0
                pops = [p for p in pops if p["s"] >= clean_until]
                # also strip per-SHOT highlight boxes in the hook window — pops aren't the only path
                # to a box over the hook (a 'end-to-end' highlight box survived the pop-only filter).
                for sh in shots:
                    if sh.get("startSec", 0) < clean_until and sh.get("highlight"):
                        sh.pop("highlight", None)
        # CAPTION best-practices (format-aware, not the old hardcoded 4s-on-every-segment template):
        # - DON'T put a lower-third over the first-5s HOOK (seg 0) — it competes with the hook card.
        # - vary timing/hold per segment so it never reads formulaic (lead the audio slightly, per research).
        # - lead the audio by ~0.2s; hold a touch longer on longer segments.
        lower_thirds = []
        if seg_index != 0:                                   # hook segment opens clean — no lower-third
            lt_start = round(0.6 + (seg_index % 3) * 0.25, 2)  # slight stagger so it's not identical each time
            lt_hold = 3.5 if seg.get("duration", 60) < 70 else 4.2
            lt_end = lt_start + lt_hold
            # DON'T render a lower-third over a DESIGNED CARD (number/quote/vs/diagram) — they're both
            # center/lower text and COLLIDE (caught on a real frame: a number card's label sat under an
            # overlapping lower-third headline). If a v2sc card occupies the lower-third window, skip it.
            card_clash = any(
                "v2sc" in (sh.get("src") or "")
                and sh.get("startSec", 0) < lt_end
                and sh.get("startSec", 0) + sh.get("durationSec", 0) > lt_start
                for sh in shots)
            if card_clash:
                lower_thirds = []
            else:
                lower_thirds = [{"startSec": lt_start, "durationSec": lt_hold,
                                 "headline": _clip(seg["title"], 64), "sourceUrl": seg["source_url"]}]
            # DEDUP: drop keyword-pops whose text already appears in the lower-third headline — showing
            # the same tool name as BOTH a pop AND the lower-third is redundant clutter (caught on a
            # real frame: 'agent-governance-toolkit' popped while the lower-third said the same thing).
            if lower_thirds:                                  # may be empty when suppressed by a card clash
                lt_text = lower_thirds[0]["headline"].lower()
                pops = [p for p in pops if p["word"].lower() not in lt_text]
        # CHOREOGRAPHED SHOT TRANSITIONS: the dynamism pass (_plan_shots) cuts a new visual
        # every 4-7s, but bare boundaries hard-cut. Tag each shot AFTER the first with a short
        # crossfade so consecutive beats dissolve instead of jump-cutting. editor_timeline
        # promotes `transitionSec` to a validated `crossfade` effect → OpenShot dissolves the
        # boundary. Skip kinetic inserts (they carry their own full-bleed motion in/out).
        for sh in shots[1:]:
            if sh.get("type") == "kinetic" or "transitionSec" in sh:
                continue
            sh["transitionSec"] = 0.4
        tsegs.append({
            "segmentId": seg["segment_id"], "title": seg["title"], "sourceUrl": seg["source_url"],
            # keywordPops 100% DEADED (John, 06-09): the floating labeled rectangles were broken-looking
            # clutter no real channel uses. Keyword emphasis = the karaoke caption highlight.
            "shots": shots, "wordTimestamps": seg["words"], "keywordPops": [],
            "audio": {"vo": {"src": _disk(seg["vo_path"]), "duration": seg["duration"]}},
            "lowerThirds": lower_thirds,
            "durationSec": seg["duration"],
        })
        total += seg["duration"]
    bed = "/agenticnews-assets/bed.mp3" if (ASSETS / "bed.mp3").exists() else None
    sfx = "/agenticnews-assets/whoosh.mp3" if (ASSETS / "whoosh.mp3").exists() else None
    # prefer the transparent logo for the sting so it blends seamlessly (no visible black square)
    logo = ("/agenticnews-assets/abn_logo_transparent.png" if (ASSETS / "abn_logo_transparent.png").exists()
            else "/agenticnews-assets/abn_logo.png" if (ASSETS / "abn_logo.png").exists() else None)
    return {"fps": fps, "width": 1920, "height": 1080, "accent": BRAND_CYAN,
            "brandKit": "/agenticnews-assets/brand/abn-forge-signal/manifest.json",
            "episodeId": str(ep_id), "title": f"AgenticBuilderNews — Episode {ep_idx}",
            "totalSec": round(total, 2), "segments": tsegs, "musicBed": bed, "sfx": sfx, "logo": logo}


async def _render_remotion(ep_id, timeline, force=False):
    if not (REMOTION_DIR / "node_modules").exists():
        raise RuntimeError("remotion not installed")
    props = asset_path(ep_id, "timeline")
    atomic_save(props, timeline)
    out = asset_path(ep_id, "episode")
    # RE-RENDER GUARD: if this episode's mp4 already exists AND is a complete, long-enough video, REUSE
    # it instead of re-rendering from scratch. A post-render hiccup (e.g. normalize/duck throwing) could
    # re-enter this function for the same ep_id — observed a single episode rendering TWICE (~2x compute,
    # 35-min total, 12 chrome workers re-burning on an already-rendered mp4). Skipping a valid existing
    # render makes re-entry cheap. (A short/partial leftover is ignored and re-rendered.)
    # force=True (operator edits from the editor bay) ALWAYS re-renders — the timeline just changed, so
    # reusing the old mp4 would silently ship the unedited video.
    if out.exists() and not force:
        try:
            _d = await _dur(out)
            # only reuse a FULLY-PROCESSED render: long enough AND already normalized to yuv420p (the
            # normalize pass converts yuvj420p->yuv420p). A raw/partial leftover (yuvj420p, the pre-
            # normalize first-render output) is NOT reused — it'd skip loudnorm/duck — so render fresh.
            _pc, _pf = await _sh(f'ffprobe -v error -select_streams v -show_entries stream=pix_fmt '
                                 f'-of csv=p=0 {shlex.quote(str(out))}', timeout=20)
            if _d and _d >= MIN_EPISODE_SEC and _pf.strip().startswith("yuv420p"):
                BUS.emit("editor-agent", "render.reuse",
                         f"reusing existing complete {_d:.0f}s render (skip redundant re-render)", episode_id=ep_id)
                # return the SAME (path, dur) 2-tuple shape the caller unpacks — a bare string here
                # would break `mp4, dur = await _render_remotion(...)` and trigger the retry/double-render.
                return _asset_url(out), _d
        except Exception:
            pass   # unreadable/partial → fall through and render fresh
    # --crf 23 ≈ visually-lossless for this flat-graphics content but ~half the file size of Remotion's
    # default high bitrate (episodes were 200MB+ → disk exhaustion on a 24/7 system). Big disk-saver.
    # Concurrency: each Remotion chrome worker spawns its OWN threads, so it's NOT one-core-per-worker.
    # cores-2 (=8 on a 10-core box) oversubscribed badly — observed load 25-33 on 10 cores, which SLOWED
    # renders to 17+min via contention (a regression). Use ~HALF the cores (cores//2, min 3) so workers
    # don't thrash and the FastAPI app keeps headroom. Overridable via ABN_RENDER_CONCURRENCY.
    try:
        _cc = int(os.getenv("ABN_RENDER_CONCURRENCY") or max(3, (os.cpu_count() or 4) // 2))
    except (TypeError, ValueError):
        _cc = 4
    cmd = (f'cd {shlex.quote(str(REMOTION_DIR))} && npx remotion render Episode {shlex.quote(str(out))} '
           f'--props={shlex.quote(str(props))} --codec=h264 --crf 23 --concurrency={_cc} --log=error 2>&1')
    BUS.emit("editor-agent", "remotion.start", "Remotion compositor rendering", episode_id=ep_id)
    code, log = await _sh(cmd, timeout=1200)
    if code != 0 or not out.exists():
        raise RuntimeError(f"remotion: exit {code}: {log[-400:]}")
    # NORMALIZE PIXEL FORMAT: Remotion's h264 encoder emits yuvj420p (full/PC-range JPEG
    # color). Browsers + VLC tolerate it, but QuickTime / Preview / Finder Quick Look /
    # macOS native players choke a few seconds in and quit — and YouTube re-encodes it
    # with shifted colors. Force standard yuv420p limited (TV) range so the file plays
    # everywhere. Cheap re-encode (flat graphics + crf 20 ≈ visually identical).
    # Also NORMALIZE LOUDNESS here (same pass, ~free): the per-chunk VO was -16 LUFS but the final
    # baked mix (VO + ducked music) drifts quiet (~-24 dB mean — caught on a real episode). A quiet
    # video sounds weak next to other channels in the feed and loses viewers. Bring the whole episode
    # to YouTube's ~-14 LUFS target with a true-peak limiter so it's competitively loud + clip-safe.
    norm = asset_path(ep_id, "assembled", "norm")  # renders/ intermediate, replaced into `out`
    ncmd = (f'ffmpeg -y -i {shlex.quote(str(out))} '
            f'-vf format=yuv420p -colorspace bt709 -color_primaries bt709 '
            f'-color_trc bt709 -color_range tv '
            f'-af loudnorm=I=-14:TP=-1.5:LRA=11 '
            f'-c:v libx264 -crf 20 -preset veryfast -c:a aac -b:a 192k '
            f'-movflags +faststart {shlex.quote(str(norm))} 2>&1')
    nc, nlog = await _sh(ncmd, timeout=600)
    if nc == 0 and norm.exists():
        norm.replace(out)
        BUS.emit("editor-agent", "render.normalize", "pixel→yuv420p + audio→-14 LUFS (YouTube-loud)", episode_id=ep_id)
    else:
        BUS.emit("editor-agent", "error", f"normalize pass failed (non-fatal): {nlog[-120:]}", episode_id=ep_id)
    # POST PASS: real sidechain ducking. Remotion bakes the VO+SFX; mix the music bed UNDER it with
    # sidechaincompress keyed off the VO so music auto-dips when narration plays (pro audio, not a flat bed).
    bed = timeline.get("musicBed")
    if bed:
        bedfile = ASSETS / str(bed).removeprefix("/agenticnews-assets/") if str(bed).startswith("/agenticnews-assets/") else ASSETS / Path(bed).name
        if bedfile.exists():
            ducked = asset_path(ep_id, "assembled", "ducked")  # renders/ intermediate, replaced into `out`
            # [0:a]=baked VO/SFX (the sidechain key), [1:a]=music looped; duck music by the VO, mix,
            # THEN loudnorm to -14 LUFS. CRITICAL ORDER FIX: the duck pass runs AFTER the normalize
            # pass and overwrites the file, so the loudness normalization MUST live here (the last
            # audio stage) — otherwise the ducked re-mix shipped quiet (~-22 dB, caught on a real
            # episode) because it undid the normalize pass's loudnorm.
            # CRITICAL: the duck pass is the LAST video stage. '-c:v copy' here passed the upstream pixel
            # format straight through — so if the normalize pass hiccuped, the episode SHIPPED yuvj420p and
            # FAILED the hard yuv420p gate (caught on ep_d640a3eb: 16.2min but yuvj420p). Re-encode the video
            # to yuv420p here too (belt-and-suspenders) so the gate holds regardless of the normalize pass.
            dcmd = (f'ffmpeg -y -i {shlex.quote(str(out))} -stream_loop -1 -i {shlex.quote(str(bedfile))} '
                    f'-filter_complex "[1:a]volume=0.22[m];[m][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[duck];'
                    f'[0:a][duck]amix=inputs=2:duration=first:dropout_transition=0:weights=1 0.9[mix];'
                    f'[mix]loudnorm=I=-14:TP=-1.5:LRA=11[a]" '
                    f'-map 0:v -map "[a]" -vf format=yuv420p -colorspace bt709 -color_primaries bt709 '
                    f'-color_trc bt709 -color_range tv -c:v libx264 -crf 20 -preset veryfast '
                    f'-c:a aac -b:a 192k -shortest {shlex.quote(str(ducked))} 2>&1')
            dc, dlog = await _sh(dcmd, timeout=300)
            if dc == 0 and ducked.exists():
                ducked.replace(out)
                BUS.emit("editor-agent", "audio.duck", "music ducked under VO (sidechaincompress)", episode_id=ep_id)
            else:
                BUS.emit("editor-agent", "error", f"duck pass failed (non-fatal): {dlog[-120:]}", episode_id=ep_id)
    return _asset_url(out), await _dur(out)


# ---------------- ASSEMBLE (ffmpeg, real multi-segment) ----------------
async def _assemble_episode_openshot(ep_id, timeline):
    """Compile the full episode through OpenShot — the sanctioned compiler (CLAUDE.md).

    The factory already builds a complete ABN timeline (`_build_timeline`); the editor-bay
    bridge already turns that exact shape into a libopenshot Timeline and renders it. This
    wires those together so final episodes flow through `editor_render.choose_renderer()`
    (OpenShot when its bindings are present, ffmpeg-layered fallback otherwise) instead of
    bypassing the compiler. Returns (episode_url, duration) like the other assemble paths.

    Raises on any failure so `produce_one_episode` falls through to Remotion / ffmpeg —
    this is additive, never a regression to the existing chain.
    """
    import services.editor_timeline as et
    import services.editor_render as er

    project = et.project_from_abn_timeline(str(ep_id), timeline, source_episode_id=str(ep_id))
    out = asset_path(ep_id, "episode")  # {ep_id}/renders/episode.mp4 via the gateway
    renderer = er.choose_renderer(out.parent, asset_root=ASSETS)
    # render() is sync (spawns a subprocess); don't block the factory event loop.
    result = await asyncio.to_thread(renderer.render, project, output_path=str(out))
    video = result.get("video") or str(out)
    final = Path(video)
    if not final.exists():
        raise RuntimeError(f"openshot compile produced no file: {result}")
    BUS.emit("editor-agent", "openshot.done",
             f"OpenShot compiled episode ({result.get('backend','?')}, {result.get('duration',0):.0f}s)",
             episode_id=ep_id, data={"backend": result.get("backend")})
    return _asset_url(final), (result.get("duration") or await _dur(final))


async def _compile_episode(ep_id, timeline, segments):
    """Run the compiler cascade: OpenShot → Remotion (×2) → ffmpeg, in that order.

    COMPILER ORDER (CLAUDE.md): OpenShot is the sanctioned compiler — try it FIRST; the
    factory's `_build_timeline` already emits the editor-bay shape it consumes. Remotion is a
    live layer source / second compositor (retried once for transient asset-fetch failures);
    the ffmpeg slideshow is the last-resort fallback. Each step is wrapped so a failure flows
    to the next — additive, never a regression. Emits a diagnostic BUS event at every stage
    boundary so the fall-through is observable. Returns (mp4_url, duration); raises if all
    three compilers fail (caller maps that to idle + abort)."""
    mp4 = dur = None
    last_err = ""
    try:
        mp4, dur = await _assemble_episode_openshot(ep_id, timeline)
    except Exception as e0:
        last_err = str(e0)[:300]
        BUS.emit("editor-agent", "openshot.fallback",
                 f"OpenShot compile failed ({last_err[:100]}); trying Remotion", episode_id=ep_id)
    if mp4 is None:
        for attempt in (1, 2):  # Remotion can fail transiently (network asset fetch) — retry once
            try:
                mp4, dur = await _render_remotion(ep_id, timeline)
                BUS.emit("editor-agent", "remotion.done", f"Remotion render: {dur:.0f}s (attempt {attempt})", episode_id=ep_id)
                break
            except Exception as e:
                last_err = str(e)[:300]
                BUS.emit("editor-agent", "error", f"remotion attempt {attempt} failed: {last_err}", episode_id=ep_id)
    if mp4 is None:
        BUS.emit("editor-agent", "remotion.fallback", f"OpenShot+Remotion failed ({last_err[:100]}); ffmpeg fallback", episode_id=ep_id)
        mp4, dur = await _assemble_episode(ep_id, segments)  # last resort; raises on its own failure
    return mp4, dur


async def _assemble_episode(ep_id, segments):
    """Each segment = source screenshot (or title card) with Ken-Burns + karaoke captions,
    over its VO. Concat all segments → one full episode MP4."""
    seg_clips = []
    for i, s in enumerate(segments):
        # deep-dive/animated segments may have no screenshot AND no card — fall back to the animated
        # bg or the logo so a visual always exists (was crashing on Path(None) → "no segment clips").
        visual = s.get("screenshot") or s.get("card") or s.get("ui") or "/agenticnews-assets/abn_logo.png"
        # assets now live in per-episode subdirs — resolve the FULL subpath from the URL, don't
        # flatten to basename (that silently dropped the subdir → every visual missing → logo fallback).
        vis = _resolve_asset(visual)
        if not vis.exists():
            vis = ASSETS / "abn_logo.png"
        wav = _resolve_asset(s["vo_path"])
        clip = asset_path(ep_id, "scratch", f"seg{i}", ext="mp4")  # per-episode concat intermediate
        # build a drawtext karaoke-ish caption (current sentence) + Ken-Burns zoom
        cap = s.get("script", "").replace(":", " ").replace("'", "")[:120]
        cap = re.sub(r'[^\w \-.,]', '', cap)
        vf = (f"scale=1920:1080,zoompan=z='min(zoom+0.0005,1.10)':d=1:s=1920x1080:fps=25,"
              f"drawbox=y=ih-180:w=iw:h=180:color=black@0.55:t=fill,"
              f"drawtext=text={shlex.quote(cap)}:fontsize=42:fontcolor=white:"
              f"x=(w-text_w)/2:y=h-120:line_spacing=8:box=0:fontfile={shlex.quote('/System/Library/Fonts/Helvetica.ttc')}")
        cmd = (f'ffmpeg -y -loop 1 -i {shlex.quote(str(vis))} -i {shlex.quote(str(wav))} '
               f'-vf {shlex.quote(vf)} -map 0:v -map 1:a '
               f'-c:v libx264 -pix_fmt yuv420p -c:a aac -shortest {shlex.quote(str(clip))}')
        code, log = await _sh(cmd, timeout=180)
        if code == 0 and clip.exists():
            seg_clips.append(clip)
            BUS.emit("editor-agent", "assemble.segment", f"rendered segment {i+1}/{len(segments)}",
                     episode_id=ep_id, data={"i": i})
    if not seg_clips:
        raise RuntimeError("no segment clips")
    # concat
    listf = asset_path(ep_id, "scratch", "list", ext="txt")  # concat manifest intermediate
    listf.write_text("".join(f"file '{c}'\n" for c in seg_clips))
    final = asset_path(ep_id, "episode")
    code, log = await _sh(
        f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(listf))} -c copy {shlex.quote(str(final))}', timeout=180)
    if code != 0 or not final.exists():
        # fallback: re-encode concat
        code, log = await _sh(
            f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(listf))} -c:v libx264 -pix_fmt yuv420p -c:a aac {shlex.quote(str(final))}', timeout=300)
        if code != 0 or not final.exists():
            raise RuntimeError(f"concat: {log[-200:]}")
    return _asset_url(final), await _dur(final)


# ---------------- THE RUN LOOP ----------------
async def produce_one_episode(force_deepdive=False, force_lore=None):
    await _PAUSE.wait()
    STATE["running"] = True
    ep_idx = int(time.time()) % 100000
    ep_id = db._uid("ep")

    # SCRAPE
    _set("scraping", "scraper-agent", "scanning Hacker News + GitHub for fresh AI stories")
    items = await asyncio.to_thread(_scrape_sync)
    for it in items[:12]:
        BUS.emit("scraper-agent", "scrape.item", f"{it['pts']}pts · {it['title'][:60]}", data=it)
    if not items:
        _set("idle", "scraper-agent", "no items found; retrying next cycle")
        return None

    # SCORE + SCOUT (the research agent DRIVES A-tier selection, not just HN points)
    # FLYWHEEL: drop stories we've covered recently (freshness ledger from memory)
    try:
        import services.abn_memory as mem
        fresh_before = len(items)
        items = [it for it in items if not mem.is_recently_used(it.get("title", ""))]
        if fresh_before - len(items) > 0:
            BUS.emit("research-agent", "memory.freshness", f"dropped {fresh_before - len(items)} already-covered stories", episode_id=ep_id)
    except Exception:
        pass
    # ADAPTIVE EPISODE SIZE — real news flow doesn't always give 7 A-tier stories. Produce a
    # tighter episode (min 3) when news is moderate; only idle if there's genuinely too little.
    # A 4-story episode of fresh content beats waiting forever for 7.
    MIN_SEG = MIN_SEGMENTS    # HARD floor (was 3 → produced 5-min videos). Episodes must START
                              # with enough segments to clear the 10-min MIN_EPISODE_SEC gate.
    if len(items) < MIN_SEG:
        # EVERGREEN FALLBACK — thin news day. Don't go dark; top up with evergreen deep-dives on
        # notable agentic tools (not freshness-gated). The niche-teardown said 60% should be evergreen
        # anyway, and it keeps a daily channel alive when breaking news is quiet.
        ever = await asyncio.to_thread(_evergreen_topics, MIN_SEG - len(items))
        # Progressive relaxation so the channel NEVER goes dark: prefer evergreen tools not covered in
        # the last 12h; if still short, accept ones not covered in 3h; if STILL short, take the freshest
        # available regardless (a re-covered tool with a new angle beats no episode at all).
        picked, used = [], {it["title"] for it in items}
        for window in (12 * 3600, 3 * 3600, 0):
            for it in ever:
                if it["title"] in used:
                    continue
                if window == 0 or not mem_recent(it["title"], window):
                    picked.append(it); used.add(it["title"])
                if len(items) + len(picked) >= MIN_SEG:
                    break
            if len(items) + len(picked) >= MIN_SEG:
                break
        items += picked
        if picked:
            BUS.emit("scraper-agent", "evergreen", f"thin news — topped up with {len(picked)} evergreen deep-dives", episode_id=ep_id)
    # a forced DEEP-DIVE only needs ONE tool (it expands into facets); LORE needs NO scraped news at all
    # (it's a single-subject narrative built from the loremaster) — both bypass the roundup minimum.
    floor = 0 if force_lore else (1 if force_deepdive else MIN_SEG)
    if len(items) < floor:
        _set("idle", "scraper-agent", f"only {len(items)} stories (<{floor}) even with evergreen — waiting", ep_id)
        BUS.emit("scraper-agent", "news.thin", f"only {len(items)} stories; backing off", episode_id=ep_id)
        return None
    seg_target = min(N_SEGMENTS, len(items))  # this episode's size, adaptive to available fresh news
    _set("scoring", "scraper-agent", f"signal-scoring {len(items)} fresh stories → {seg_target}-segment episode")
    items.sort(key=_score, reverse=True)
    seen, ranked = set(), []
    for it in items:
        k = it["title"].lower()
        if k in seen:
            continue
        seen.add(k); ranked.append(it)
    # the SCOUT expert curates the shortlist with a POV (A-tier for agentic builders)
    import services.abn_experts as experts
    _set("scouting", "research-agent", f"scout curating A-tier picks from {len(ranked)} stories", ep_id)
    cand = "\n".join(f"{i}. {it['title']} ({it['pts']}pts) — {it['url']}" for i, it in enumerate(ranked[:18]))
    pick_idx = await asyncio.to_thread(experts.ask, "scout",
        f"Pick the {seg_target} best stories for an episode. Return ONLY the numbers (comma-separated) of your picks, best first.\n\n{cand}")
    picks = []
    if pick_idx:
        import re as _re
        nums = [int(n) for n in _re.findall(r'\d+', pick_idx)][:seg_target]
        picks = [ranked[n] for n in nums if 0 <= n < len(ranked)]
        for it in picks:
            BUS.emit("research-agent", "scout.pick", f"A-tier: {it['title'][:52]}", data={"src": it.get('source_signal', '')})
    if len(picks) < seg_target:  # scout failed → fall back to score order
        for it in ranked:
            if it not in picks:
                picks.append(it)
            if len(picks) >= seg_target:
                break

    # FORMAT VARIETY: every 4th episode, do a single-tool DEEP-DIVE instead of a roundup.
    # Takes the top pick, expands it into 3 focused angles (what/how/why) — the in-the-mud format.
    deepdive = False
    try:
        import services.abn_memory as _mm
        if picks and (force_deepdive or int(_mm.stats().get("episodes", 0)) % 4 == 3):
            top = picks[0]
            angles = await asyncio.to_thread(experts.ask, "deepdive", f"Tool: {top['title']}\nSource: {top.get('url','')}")
            lines = [l.strip() for l in (angles or "").splitlines() if l.strip()][:3]
            if len(lines) == 3:
                picks = [{**top, "title": f"{top['title'].split(' — ')[0].split(':')[0]}: {l.split('—')[0].replace('ANGLE:','').strip()}",
                          "url": top.get("url", ""), "_facet": l} for l in lines]
                deepdive = True
                BUS.emit("research-agent", "deepdive", f"single-tool deep-dive: {top['title'][:40]}", episode_id=ep_id)
    except Exception:
        pass

    # LORE FORMAT: a single-subject narrative ("The Rise of Anthropic") built from the loremaster's
    # 6-beat arc (cold-open → origin → bet → conflict → where-it-stands → open-question). Sibling to
    # deepdive: reshapes `picks` into 6 beat pseudo-segments. force_lore = the subject string.
    lore = False
    lore_subject = force_lore
    try:
        if lore_subject:
            beats_raw = await asyncio.to_thread(experts.ask, "loremaster", f"Subject: {lore_subject}")
            beats = []
            for line in (beats_raw or "").splitlines():
                line = line.strip()
                if line.upper().startswith("BEAT:"):
                    beats.append(line[5:].strip())
            beats = beats[:9]  # lore beats run ~85-90s each; 6 lands ~8:40 (under the 600s floor).
                               # Allow up to 9 so a lore episode can clear MIN_EPISODE_SEC like a
                               # roundup does via MIN_SEGMENTS=8.
            if len(beats) >= 4:
                picks = [{"title": f"{lore_subject}: {b.split('—')[0].strip()[:40]}",
                          "url": "", "_facet": b, "_lore": True} for b in beats]
                lore = True
                BUS.emit("research-agent", "lore", f"lore episode: {lore_subject} ({len(beats)} beats)", episode_id=ep_id)
    except Exception:
        pass

    # honest format label: reflect the ACTUAL shape, not a hardcoded string
    ep_format = (f"Lore: {lore_subject}" if lore else "Deep-Dive" if deepdive else f"Roundup ({len(picks)} stories)")
    _set("bundling", "editor-agent", f"bundling {ep_format} into Episode {ep_idx}", ep_id)
    ep = await db.create_video(dict(
        id=ep_id, kind="episode", title=f"AgenticBuilderNews — Episode {ep_idx}",
        stage="scripting", lane="today", format=ep_format,
        episode_index=ep_idx, segment_count=len(picks), artifacts={}))
    BUS.emit("editor-agent", "episode.created", f"Episode {ep_idx} created with {len(picks)} segments", episode_id=ep_id, data={"index": ep_idx})

    # ─── NARRATIVE ARC: find the thesis connecting the stories → cold-open (tell stories, not recaps) ───
    import services.abn_experts as experts
    _set("narrative", "narrator-agent", "finding the thesis that threads the stories into one arc", ep_id)
    story_list = "\n".join(f"- {it['title']}" for it in picks)
    # FLYWHEEL read-side: seed the narrator with theses from APPROVED episodes (proven framing)
    proven = ""
    try:
        import services.abn_memory as mem
        wins = mem.winning_theses(4)
        if wins:
            proven = ("\n\nThese cold-opens have worked well before — match their punch and structure, "
                      "do NOT reuse their wording:\n" + "\n".join(f"• {w[:120]}" for w in wins))
            BUS.emit("narrator-agent", "memory.proven", f"seeded with {len(wins)} proven theses", episode_id=ep_id)
    except Exception:
        pass
    # COMPETITOR INTEL: feed the narrator the real hook rule distilled from top AI channels' actual
    # openings (e.g. 'I've read the 244-page report Anthropic put out') so the hook MODELS proven
    # winners instead of guessing. Falls back silently if the intel isn't available.
    try:
        import services.abn_competitors as _comp
        _hook_pb = _comp.hook_playbook()
    except Exception:
        _hook_pb = ""
    cold_open = await asyncio.to_thread(experts.ask, "narrator",
                                        f"Episode stories:\n{story_list}{proven}\n\nFind the connecting thesis and write the cold-open.",
                                        _hook_pb)
    if cold_open:
        BUS.emit("narrator-agent", "narrative.thesis", f"cold-open: {cold_open[:70]}", episode_id=ep_id, data={"cold_open": cold_open})
    # FLYWHEEL: record this episode's stories + thesis into self-refinement memory
    try:
        import services.abn_memory as mem
        mem.record_episode(ep_id, [it["title"] for it in picks], cold_open or "", approved=False)
    except Exception:
        pass

    # ─── WAVE-BASED PARALLEL PRODUCTION ───
    # The orchestrator fans the crew out across ALL segments at once, wave by wave:
    #   WAVE 1: research+script every segment in parallel (research-agent + script-agent team)
    #   WAVE 2: VO+align every segment in parallel (vo-agent team)
    #   WAVE 3: capture/build every segment's assets in parallel (editor-agent team)
    # Not a flat conveyor — concurrent agent teams coordinated by the orchestrator.

    async def _wave1_script(i, it):
        sid = f"{ep_id}_s{i}"
        # LIGHT UP THE BOARD: create the segment card the INSTANT research starts, so the board shows
        # cards appearing + moving through stages live (researching→scripting→voicing→assets), not all
        # popping in finished at the end.
        try:
            await db.create_video(dict(id=sid, kind="segment", episode_id=ep_id, segment_index=i,
                                       title=it["title"], stage="researching", lane="today",
                                       hook="", source_url=it["url"], source_signal=f"HN {it.get('pts',0)}pts"))
        except Exception:
            pass
        BUS.emit("research-agent", "research.start", f"seg {i+1}: researching {it['title'][:30]}", episode_id=ep_id, segment_id=sid)
        # DEEPDIVE FACETS: research EACH facet's own angle so the 3 briefs differ (was: one shared brief
        # → all 3 scripts repeated the same hook). Plus an opener-ban so later facets don't restate the pitch.
        is_facet = bool(it.get("_facet"))
        facet_angle = it["_facet"] if (is_facet and (deepdive or lore)) else None
        brief = await asyncio.to_thread(_research_sync, it["title"], it["url"], facet_angle)
        if deepdive and is_facet:
            opener_ban = ("" if i == 0 else
                          " The audience ALREADY knows what this tool is — do NOT open by restating its "
                          "pitch (no 'X runs Y in Rust' summary). Open mid-substance, on THIS angle only.")
            brief = ((brief or "") +
                     f"\n\nTHIS SEGMENT'S ANGLE (cover ONLY this): {it['_facet']}.{opener_ban}")
        elif lore and is_facet:
            # narrative beat of one continuous story — flows from the previous beat, documentary register
            brief = ((brief or "") +
                     f"\n\nYou are writing ONE BEAT of a continuous documentary-style origin story about "
                     f"{lore_subject}. This beat: {it['_facet']}. Pick up the narrative thread, advance the "
                     f"story, use real names/dates/numbers (never invent them). No 'in this video' framing.")
        await _best_effort_update(sid, {"stage": "scripting"})
        BUS.emit("script-agent", "script.start", f"seg {i+1}: scriptwriter drafting", episode_id=ep_id, segment_id=sid)
        # segment 0 opens with the NARRATIVE cold-open (thesis), then its story beat.
        # deepdive + lore episodes script each beat DEEPER (single-topic long format).
        deep_script = deepdive or lore
        if i == 0 and cold_open and not lore:
            beat = await _script_segment(it["title"], it["url"], i, False, brief, deep=deep_script)
            script = cold_open.strip() + " " + beat
        else:
            # lore beat 0 IS the cold-open (the loremaster's first beat) — no separate thesis intro
            script = await _script_segment(it["title"], it["url"], i, i == 0 and not lore, brief, deep=deep_script)
        # last segment CTA: lore ends on its open-question beat (just subscribe); roundup/deepdive get the full CTA
        if i == len(picks) - 1:
            if lore:
                script = script.rstrip() + " Subscribe for more deep dives into the builders shaping agentic AI."
            else:
                script = script.rstrip() + (" That's the rundown. Which of these are you actually "
                    "shipping with? Drop it in the comments. Subscribe for the daily agentic-builder brief.")
        BUS.emit("script-agent", "script.done", f"seg {i+1} script ({len(script.split())}w)", episode_id=ep_id, segment_id=sid, data={"script": script})
        await _best_effort_update(sid, {"stage": "voicing", "hook": script[:80]})
        return {"i": i, "sid": sid, "it": it, "script": script}

    async def _wave2_voice(s):
        i, sid = s["i"], s["sid"]
        BUS.emit("vo-agent", "vo.start", f"seg {i+1}: voicing", episode_id=ep_id, segment_id=sid)
        vo_path, dur = await _voice(s["script"], sid)  # VO is essential — if this fails the segment can't exist
        BUS.emit("vo-agent", "vo.done", f"seg {i+1}: {dur:.0f}s VO", episode_id=ep_id, segment_id=sid, artifact_url=vo_path, data={"duration": dur})
        await _best_effort_update(sid, {"stage": "assets"})
        # alignment (karaoke captions) is an ENHANCEMENT — never let it kill the segment
        try:
            words = await _align(sid)
            words = _script_align(words, s["script"])  # captions show what we WROTE, timed by whisper
        except Exception as ex:
            words = []
            BUS.emit("vo-agent", "error", f"seg {i+1}: align failed (non-fatal, no captions) — {ex!r}"[:120], episode_id=ep_id, segment_id=sid)
        BUS.emit("vo-agent", "align.done", f"seg {i+1}: {len(words)} words aligned", episode_id=ep_id, segment_id=sid, data={"word_timestamps": words[:200]})
        return {**s, "vo_path": vo_path, "duration": dur, "words": words}

    async def _wave3_assets(s):
        # Every visual step is best-effort. A segment only needs ONE usable visual; no single
        # asset failure should kill it (the bug class that produced silent 'all segments failed').
        i, sid, it = s["i"], s["sid"], s["it"]
        async def _try(coro, label):
            try:
                return await coro
            except Exception as ex:
                BUS.emit("editor-agent", "error", f"seg {i+1}: {label} failed (non-fatal) — {ex!r}"[:120], episode_id=ep_id, segment_id=sid)
                return None
        shot = await _try(_screenshot(it["url"], sid), "screenshot")
        card = await _try(_card(it["title"].upper(), it["title"][:50], sid), "card")
        ui = None
        url = it.get("url", "")
        if url and url.startswith("http") and "news.ycombinator" not in url:
            import services.abn_capture as cap
            BUS.emit("editor-agent", "ui.recording", f"seg {i+1}: recording live UI ({url[:34]})", episode_id=ep_id, segment_id=sid)
            ui = await _try(asyncio.to_thread(cap.capture_sync, url, sid, 14.0), "ui-capture")
            if ui:
                BUS.emit("editor-agent", "ui.capture", f"seg {i+1}: live UI capture (page scroll)", episode_id=ep_id, segment_id=sid, artifact_url=ui, data={"type": "ui"})
        # a segment needs SOME visual; card is the guaranteed fallback. If even that's gone, last resort
        # is the screenshot or ui; if truly nothing, the timeline's KenBurns fallback handles a missing src.
        primary = ui or shot or card
        BUS.emit("editor-agent", "asset.placed", f"seg {i+1}: {'live UI' if ui else 'source shot' if shot else 'title card' if card else 'no visual'}", episode_id=ep_id, segment_id=sid, artifact_url=primary, data={"type": "ui" if ui else "source" if shot else "title_card"})
        # BUILD BEAT: if the story is a tool/repo/coding story, generate a real VHS code demo (best-effort)
        demo = None
        t = it["title"].lower()
        if any(k in t for k in ("agent", "code", "cli", "tui", "tool", "open source", "open-source", "sdk", "api", "framework", "repo", "build")):
            # PREFER A REAL DEMO: if the source is a github repo, clone it and record REAL read-only
            # output (the credibility moat). Falls back to the scripted snippet only if that fails or
            # the source isn't a clonable repo. Both produce the same <sid>_demo.mp4 slot.
            real_repo = _github_repo(url)
            if real_repo:
                BUS.emit("editor-agent", "demo.real.start", f"seg {i+1}: cloning + recording REAL repo output (VHS)", episode_id=ep_id, segment_id=sid)
                demo = await _try(_real_demo(url, sid), "real-demo")
                if demo:
                    BUS.emit("editor-agent", "demo.real", f"seg {i+1}: REAL repo demo rendered (clone+inspect)", episode_id=ep_id, segment_id=sid, artifact_url=demo, data={"type": "code", "real": True})
            if not demo:
                BUS.emit("editor-agent", "code.rendering", f"seg {i+1}: rendering code demo (VHS)", episode_id=ep_id, segment_id=sid)
                demo = await _try(_code_demo(it["title"], s.get("script", ""), sid), "code-demo")
                if demo:
                    BUS.emit("editor-agent", "code.demo", f"seg {i+1}: code demo rendered (VHS)", episode_id=ep_id, segment_id=sid, artifact_url=demo, data={"type": "code"})
        # KINETIC INSERT (every ~3rd segment ≈ 3-4/episode, bounded render cost): deterministic
        # browser motion-graphics beat (hook → facts → takeaway) — the seekable-html explainer
        # texture John explicitly asked to mix into episodes.
        kinetic = None
        if i % 3 == 1:
            BUS.emit("editor-agent", "kinetic.start", f"seg {i+1}: rendering kinetic insert (seekable-html)", episode_id=ep_id, segment_id=sid)
            kinetic = await _try(_kinetic_insert(it["title"], s.get("script", ""), url, sid), "kinetic-insert")
            if kinetic:
                BUS.emit("editor-agent", "kinetic.done", f"seg {i+1}: kinetic insert rendered", episode_id=ep_id, segment_id=sid, artifact_url=kinetic, data={"type": "kinetic"})
        return {**s, "screenshot": shot, "card": card, "demo": demo, "ui": ui, "kinetic": kinetic}

    # WAVE 1 — research + script, all segments concurrently
    _set("scripting", "script-agent", f"WAVE 1: research+script {len(picks)} segments in parallel", ep_id)
    def _surface(stage_name, results):
        """Don't silently swallow wave failures — log each exception so we can SEE what broke."""
        ok, errs = [], []
        for x in results:
            if isinstance(x, dict):
                ok.append(x)
            elif isinstance(x, BaseException):
                errs.append(repr(x))
        if errs:
            BUS.emit("editor-agent", "error", f"{stage_name}: {len(errs)}/{len(results)} segments failed — {errs[0][:120]}", episode_id=ep_id)
        return ok

    # kick off the episode's animated b-roll background concurrently — its ~70s overlaps the waves
    BUS.emit("editor-agent", "broll.gen", "generating animated brand b-roll (Flux→wan i2v)", episode_id=ep_id)
    bg_task = asyncio.create_task(_animated_bg(ep_id))

    w1 = await asyncio.gather(*[_wave1_script(i, it) for i, it in enumerate(picks)], return_exceptions=True)
    w1 = _surface("WAVE1-script", w1)
    # WAVE 2 — VO + align, all segments concurrently
    _set("voicing", "vo-agent", f"WAVE 2: voicing+aligning {len(w1)} segments in parallel", ep_id)
    w2 = await asyncio.gather(*[_wave2_voice(s) for s in w1], return_exceptions=True)
    w2 = _surface("WAVE2-voice", w2)
    # WAVE 3 — assets, all segments concurrently
    _set("placing", "editor-agent", f"WAVE 3: capturing assets for {len(w2)} segments in parallel", ep_id)
    w3 = await asyncio.gather(*[_wave3_assets(s) for s in w2], return_exceptions=True)
    w3 = _surface("WAVE3-assets", w3)

    # collect the animated b-roll bgs (best-effort; list of distinct clips). Generating 3 takes longer
    # than the waves, so allow more headroom; whatever's ready by the deadline is used.
    animated_bg = None
    try:
        animated_bg = await asyncio.wait_for(bg_task, timeout=180)
        if animated_bg:
            BUS.emit("editor-agent", "broll.done", f"{len(animated_bg)} distinct animated b-rolls ready", episode_id=ep_id)
    except Exception:
        animated_bg = None

    segments = []
    for s in sorted(w3, key=lambda x: x["i"]):
        i, sid, it = s["i"], s["sid"], s["it"]
        segments.append(dict(segment_id=sid, segment_index=i, title=it["title"], source_url=it["url"],
                             script=s["script"], vo_path=s["vo_path"], duration=s["duration"], words=s["words"],
                             screenshot=s["screenshot"], card=s["card"], demo=s.get("demo"), ui=s.get("ui")))
        # card already exists (created live at research start) — UPDATE it to its finished state
        await db.update_video(sid, {"stage": "voice_visuals", "hook": s["script"][:80],
                                    "artifacts": {"vo": True, "vo_path": s["vo_path"], "thumbnail": True,
                                                  "thumbnail_path": s["screenshot"] or s["card"]}})

    if not segments:
        _set("idle", "editor-agent", "all segments failed; aborting episode", ep_id)
        return None

    # ASSEMBLE
    total_words = sum(len(s["script"].split()) for s in segments)
    _set("assembling", "editor-agent", f"assembling {len(segments)}-segment episode (~{total_words} words)", ep_id)
    BUS.emit("editor-agent", "assemble.start", f"rendering {len(segments)} segments → episode", episode_id=ep_id)
    timeline = _build_timeline(ep_id, ep_idx, segments, animated_bg=animated_bg)
    try:
        mp4, dur = await _compile_episode(ep_id, timeline, segments)
    except Exception as e2:
        _set("idle", "editor-agent", f"assemble failed: {e2}", ep_id)
        BUS.emit("editor-agent", "error", f"assemble failed: {e2}", episode_id=ep_id)
        return None

    # ─── HARD DURATION GATE (non-negotiable RPM/mid-roll floor) ───
    # An episode under MIN_EPISODE_SEC (10:00) is REJECTED here — it never reaches 'review',
    # never gets packaged, never ships. This is the enforced valuation parameter: a render
    # that doesn't clear the floor is unsatisfactory work, full stop. (We start with
    # MIN_SEGMENTS so this should rarely trip; when it does, the loop just produces the next.)
    real_dur = dur or 0.0
    if real_dur < MIN_EPISODE_SEC:
        _set("idle", "editor-agent",
             f"REJECTED: {real_dur:.0f}s < {MIN_EPISODE_SEC}s 10-min floor — discarding, producing next", ep_id)
        BUS.emit("qa", "reject.duration",
                 f"Episode {ep_idx} REJECTED: {real_dur:.0f}s under the {MIN_EPISODE_SEC//60}min RPM floor",
                 episode_id=ep_id, data={"duration": real_dur, "floor": MIN_EPISODE_SEC})
        try:
            await db.update_video(ep_id, {"stage": "rejected",
                                          "data": {"reject_reason": f"under_floor_{real_dur:.0f}s"}})
        except Exception:
            pass
        return None

    # ─── EXPERT PACKAGING TEAM (titles, SEO, thumbnail, pinned comment) in parallel ───
    _set("packaging", "expert-team", "title/SEO/thumbnail/comment experts packaging the episode", ep_id)
    import services.abn_experts as experts
    stories = " | ".join(s["title"][:60] for s in segments)
    pkg_keys = ["titler", "seo", "thumbnailer", "commenter"]
    # COMPETITOR INTEL: feed the titler/thumbnailer the REAL top-performing competitor titles to model.
    try:
        import services.abn_competitors as _comp
        _title_pb = _comp.title_playbook()
    except Exception:
        _title_pb = ""
    _pkg_ctx = {"titler": _title_pb, "thumbnailer": _title_pb}
    # BOUNDED packaging: the render is DONE and valid by now — the post-render packaging (titler/seo/thumb/
    # comment experts) must NOT be able to wedge the episode short of 'review'. Seen on real episodes
    # (ep_e4c90a5e, ep_f361b430): a hung experts.ask thread (or a slow competitor scrape) left the mp4
    # complete but the episode stuck pre-review for 30+ min until the watchdog killed it. Cap the whole
    # packaging gather at 120s; on timeout, ship empty package fields and proceed to review anyway.
    try:
        pkg_results = await asyncio.wait_for(
            asyncio.gather(*[asyncio.to_thread(experts.ask, k, f"Episode stories: {stories}", _pkg_ctx.get(k, "")) for k in pkg_keys]),
            timeout=120)
    except asyncio.TimeoutError:
        pkg_results = ["" for _ in pkg_keys]
        BUS.emit("editor-agent", "error", "packaging timed out (120s) — shipping to review without it", episode_id=ep_id)
    package = {k: (v or "") for k, v in zip(pkg_keys, pkg_results)}
    # REAL chapters from actual segment durations — the SEO expert guesses timestamps that don't land on
    # segment boundaries (broken YouTube chapters). Replace its chapter block with true cumulative times.
    def _ts(sec):
        return f"{int(sec)//60}:{int(sec)%60:02d}"
    real_ch, acc = [], 0.0
    for s in segments:
        real_ch.append(f"{_ts(acc)} — {s['title'][:60]}")
        acc += s.get("duration", 0)
    if package.get("seo") and real_ch:
        import re as _re
        # strip the LLM's guessed chapter lines, splice in real ones before the Tags: block
        seo = package["seo"]
        seo = _re.sub(r'(?m)^\s*\d+:\d{2}\s*[—-].*$', '', seo)  # remove fake "M:SS — ..." lines
        seo = _re.sub(r'[ \t]{2,}', ' ', seo)                   # collapse double-spaces (blemish in shipped desc)
        seo = _re.sub(r'(?m)[ \t]+$', '', seo)                  # strip trailing whitespace per line
        seo = _re.sub(r'\n{3,}', '\n\n', seo).strip()
        if "Tags:" in seo:
            head, tags = seo.split("Tags:", 1)
            package["seo"] = head.rstrip() + "\n\n" + "\n".join(real_ch) + "\n\nTags:" + tags
        else:
            package["seo"] = seo + "\n\n" + "\n".join(real_ch)
    # TITLE VARIETY: the titler always orders its 5 candidates the same way (slop-callout first), so
    # shipping title[0] every time makes every video read "X is slop". Rotate which candidate LEADS,
    # by episode count, so the channel varies its hook structure instead of looking formulaic.
    tlines = [l.strip() for l in (package.get("titler") or "").splitlines() if l.strip()]
    if len(tlines) > 1:
        try:
            import services.abn_memory as _tm
            r = int(_tm.stats().get("episodes", 0)) % len(tlines)
        except Exception:
            r = 0
        tlines = tlines[r:] + tlines[:r]  # rotate so a different pattern leads each episode
        package["titler"] = "\n".join(tlines)
        BUS.emit("expert-team", "title.rotate", f"lead title pattern rotated → {tlines[0][:50]}", episode_id=ep_id)
    for k in pkg_keys:
        if package[k]:
            BUS.emit("expert-team", f"expert.{k}", f"{k} expert delivered", episode_id=ep_id, data={"text": package[k][:160]})
    # generate the REAL thumbnail image from the title/thumbnailer experts
    lead_title = (package.get("titler") or "").strip().splitlines()[0] if package.get("titler") else segments[0]["title"]
    thumb_img = await _thumbnail(ep_id, lead_title, package.get("thumbnailer", ""))
    if thumb_img:
        package["thumbnail_image"] = thumb_img
        BUS.emit("expert-team", "expert.thumbnail_image", "thumbnail image rendered", episode_id=ep_id, artifact_url=thumb_img)

    # offload the finished mp4 to R2 cloud (durable fix for the disk wall) — no-op if R2 unconfigured.
    cloud_url = await asyncio.to_thread(_offload_episode, ep_id)
    arts = {"assembly": True, "assembly_path": mp4, "package": package}
    if cloud_url:
        arts["cloud_url"] = cloud_url
    # PROMOTE the titler's best candidate to the episode TITLE — was shipping the generic
    # "AgenticBuilderNews — Episode N" placeholder while the engaging searchable titles sat unused in
    # the package (an RPM/CTR leak: the good title never becomes the YouTube title). lead_title is the
    # rotated best candidate already used for the thumbnail; clean it for use as the title.
    final_title = re.sub(r'^\s*(title\s*[:\-]\s*|\d+[\).]\s*)', '', (lead_title or "").strip(), flags=re.I)
    final_title = final_title.strip(' "\'')[:100] or f"AgenticBuilderNews — Episode {ep_idx}"
    await db.update_video(ep_id, {"stage": "review",
                                  "title": final_title,
                                  "artifacts": arts,
                                  "timeline": {"segments": [{k: s[k] for k in ("segment_id", "title", "script", "vo_path", "duration", "screenshot", "card")} for s in segments]},
                                  "duration": dur})
    BUS.emit("expert-team", "title.set", f"episode title → {final_title[:60]}", episode_id=ep_id)
    # FLYWHEEL: mark these stories produced (rendered) so freshness only blocks REAL episodes
    try:
        import services.abn_memory as mem
        mem.record_episode(ep_id, [s["title"] for s in segments], cold_open or "", approved=False, rendered=True)
    except Exception:
        pass
    # AUTO-QA: grade every episode the moment it's produced (continuous self-monitoring, not just
    # on-demand). A low score surfaces as an alert so quality regressions can't slip in unseen.
    try:
        qa = {
            "segments": len(segments) >= 3,
            "all_have_vo": all(s.get("vo_path") for s in segments),
            "all_have_visual": all(s.get("ui") or s.get("screenshot") or s.get("card") for s in segments),
            "title": bool(package.get("titler")),
            "seo_chapters": bool(package.get("seo") and "0:00" in (package.get("seo") or "")),
            "thumbnail": bool(package.get("thumbnail_image")),
            "pinned_comment": bool(package.get("commenter")),
            "music_bed": (ASSETS / "bed.mp3").exists(),
        }
        score = sum(1 for v in qa.values() if v)
        total = len(qa)
        failed = [k for k, v in qa.items() if not v]
        if score == total:
            BUS.emit("qa", "qa.pass", f"Episode {ep_idx} QA {score}/{total} — all green", episode_id=ep_id, data=qa)
        else:
            BUS.emit("qa", "qa.alert", f"Episode {ep_idx} QA {score}/{total} — FAILED: {', '.join(failed)}", episode_id=ep_id, data=qa)
        # SEMANTIC QA: structural checks can't tell good-script from boring-but-present. The critic
        # judges whether a real builder would actually watch it (1-10), catching content-quality drift.
        try:
            import services.abn_experts as _ex, re as _re
            full_script = " ".join(s.get("script", "") for s in segments)[:2500]
            verdict = await asyncio.to_thread(_ex.ask, "critic", f"Episode script:\n{full_script}")
            m = _re.search(r'SCORE:\s*(\d+)', verdict or "")
            sem = int(m.group(1)) if m else None
            if sem is not None:
                act = "qa.pass" if sem >= 7 else "qa.alert"
                BUS.emit("qa", act, f"Episode {ep_idx} CRITIC {sem}/10 — {(verdict or '').split('|',1)[-1].strip()[:70]}", episode_id=ep_id, data={"critic_score": sem})
        except Exception:
            pass
    except Exception:
        pass
    _set("awaiting_approval", "editor-agent", f"Episode {ep_idx} ready: {dur:.0f}s, {len(segments)} segments", ep_id)
    BUS.emit("editor-agent", "episode.ready_for_review", f"Episode {ep_idx} ready for approval — {dur:.0f}s",
             episode_id=ep_id, artifact_url=mp4, data={"duration": dur, "segments": len(segments)})
    return ep_id


def _offload_episode(ep_id):
    """Push a finished episode mp4 to R2 cloud storage so disk doesn't fill on a 24/7 system.
    Returns the public/presigned URL if uploaded, else None (graceful no-op when R2 isn't configured).
    The durable fix for the recurring disk wall: finished episodes live in the cloud, newest few stay local."""
    try:
        import services.r2 as r2
        if not r2.is_configured():
            return None
        mp4 = asset_path(ep_id, "episode")
        if not mp4.exists():
            return None
        key = f"agenticnews/episodes/{ep_id}_episode.mp4"
        r2.upload_from_path(key, mp4, content_type="video/mp4")
        try:
            url = r2.public_url(key) if hasattr(r2, "public_url") else r2.presign_get(key)
        except Exception:
            url = None
        BUS.emit("system", "offload", f"episode {ep_id} → R2 cloud ({mp4.stat().st_size//1024//1024}MB)", episode_id=ep_id)
        return url or key
    except Exception as e:
        # R2 is configured + the mp4 exists, so this is a REAL upload failure (bad creds, denied
        # bucket, network) — NOT the graceful no-op above. Silently returning None here defeats the
        # whole point of offload (the durable fix for the recurring disk wall): episodes never leave
        # disk and it fills with no signal. Surface it as a system error so an alert fires.
        BUS.emit("system", "error", f"episode {ep_id} R2 offload FAILED ({type(e).__name__}: {e}) — disk will fill", episode_id=ep_id)
        return None


def _old_episode_renders():
    """Real schema episode renders ({ep_id}/renders/episode.mp4), newest first. The ONLY mp4s the
    low-disk trim may tombstone — never a flat glob, never a symlink, never a non-render file, and
    never a reserved-top dir (`_shared`/`_scratch`/`_published`/`_trash`): those start with `_`, and
    a tombstoned render that landed at `_trash/<ep>/renders/episode.mp4` must not be re-enumerated and
    re-trimmed. This keeps the enumerator in lockstep with tombstone_render()'s own `_`-prefix guard."""
    out = []
    try:
        for child in ASSETS.iterdir():
            if not child.is_dir() or child.is_symlink() or child.name.startswith("_"):
                continue
            mp4 = child / "renders" / "episode.mp4"
            try:
                # Belt-and-suspenders: only enumerate a real .mp4 render. The path is constructed
                # with a hardcoded basename, but guard the extension anyway so a non-render file
                # (e.g. 'episode' / 'episode.txt') can never be selected for tombstoning under low
                # disk — is_file()+not is_symlink() alone wouldn't catch that.
                if mp4.suffix == ".mp4" and mp4.is_file() and not mp4.is_symlink():
                    out.append(mp4)
            except OSError:
                pass
    except (FileNotFoundError, OSError):
        pass
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def purge_disk(intermediate_age_s=1800, keep_episodes=4, low_disk_gb=2.0):
    """Free disk by reaping ONLY per-episode scratch/ + cross-episode _scratch/ (the schema's
    reapable surface — abn_assets spec lines 27, 32), tombstoning to _trash/ instead of unlinking.
    Under low disk, also trim the oldest real episode renders — TOMBSTONED to _trash/ via
    tombstone_render(), never unlinked, so a buggy disk-trim is recoverable, not permanent data
    loss. Callable from the factory loop AND the manual /gc endpoint. Returns MB freed.

    The per-episode schema makes this safe by construction: the GC never enumerates schema dirs,
    renders, audio, or back-compat symlinks, so there is no flat-glob blast radius to guard against
    (this replaces the interim `_gc_unsafe` migration band-aid)."""
    import shutil as _sh2
    freed = 0
    try:
        now = time.time()
        # OBSERVABILITY (ticket: measure per-episode scratch growth in prod). Emit the per-owner
        # scratch byte breakdown BEFORE reaping so we can age the schema-rooted GC under load and
        # spot a runaway episode. Best-effort: never let measurement break the GC.
        try:
            usage = scratch_usage()
            if usage:
                total_mb = sum(usage.values()) // 1024 // 1024
                top = sorted(usage.items(), key=lambda kv: kv[1], reverse=True)[:5]
                detail = ", ".join(f"{owner}={sz//1024//1024}MB" for owner, sz in top)
                BUS.emit("system", "gc", f"scratch usage {total_mb}MB across {len(usage)} owners — top: {detail}")
        except Exception:
            pass
        protected_paths, protection_complete = _editor_timeline_asset_paths_checked()
        for f in reapable_scratch():
            try:
                if _is_editor_timeline_protected_asset(f, protected_paths):
                    continue
                if now - f.stat().st_mtime > intermediate_age_s:
                    freed += tombstone(f)
            except Exception:
                pass
        # FAIL SAFE: if the protection scan was incomplete (a timeline JSON we couldn't read, or a
        # glob that errored), we may not know every render an active Editor Bay timeline depends on.
        # Skip the destructive low-disk render trim rather than risk tombstoning a live render and
        # 500-ing the next render — disk pressure is recoverable, a deleted in-use render is not.
        #
        # RE-SCAN before the destructive trim: the protected set captured at the top of this function
        # is stale by the time we get here (the scratch-reap loop above takes wall-clock time, during
        # which a NEW Editor Bay timeline can be saved referencing a render this trim is about to
        # delete). A fresh scan at the moment of trimming closes that TOCTOU window; we OR the two sets
        # so a render protected by EITHER scan survives, and require BOTH scans complete before trimming.
        if protection_complete and _sh2.disk_usage(str(ASSETS)).free / 1e9 < low_disk_gb:
            fresh_paths, fresh_complete = _editor_timeline_asset_paths_checked()
            if not fresh_complete:
                return freed // 1024 // 1024
            protected_paths = protected_paths | fresh_paths
            for old in _old_episode_renders()[keep_episodes:]:
                try:
                    if _is_editor_timeline_protected_asset(old, protected_paths):
                        continue
                    freed += tombstone_render(old)  # safe-delete → _trash/, recoverable (not unlink)
                except Exception:
                    pass
    except Exception:
        pass
    return freed // 1024 // 1024


async def _gc_segments(keep_recent=12):
    """Self-maintenance: prune orphan segment cards + archive old episodes so the board stays lean."""
    try:
        vids = await db.list_videos()
        eps = sorted([v for v in vids if v.get("kind") == "episode"], key=lambda x: x.get("created_at", 0), reverse=True)
        archive = {e["id"] for e in eps[keep_recent:]}
        now = time.time()
        STALE = 2 * 3600  # a real episode completes in minutes; >2h in a mid-production stage = dead/crashed
        midstages = ("scripting", "voice_visuals", "assembly", "bundling", "narrative", "scouting", "scoring")
        n = 0
        for v in vids:
            kind, stage = v.get("kind"), v.get("stage")
            stale_inflight = (kind == "episode" and stage in midstages
                              and now - v.get("created_at", now) > STALE)
            # prune: STALE segment cards (>2h — never the fresh in-flight ones now lighting up the board);
            # archived old scheduled/live; rejected 'revision' episodes; episodes stuck mid-production
            stale_seg = (kind == "segment" and now - v.get("created_at", now) > STALE)
            # DEAD REVIEW ROW: a 'review' episode whose rendered mp4 was already GC'd from disk is a
            # stale board card pointing at a deleted file — trying to review/publish it fails. Drop it.
            dead_review = False
            if kind == "episode" and stage == "review":
                # db.list_videos() merges the JSON `data` blob to the TOP LEVEL (no nested 'data'
                # key), so artifacts/assembly_path live at v["artifacts"]. (The old nested lookup was
                # dead code that always missed.) Read the real location directly.
                _ap = (v.get("artifacts") or {}).get("assembly_path", "")
                if _ap:
                    _mp4 = _resolve_asset(_ap)  # preserve subpath — a migrated mp4 isn't "missing"
                    # only prune if it's also archived (old) — never a fresh review awaiting a human
                    dead_review = (not _mp4.exists()) and (v.get("id") in archive)
            if (stale_seg
                    or (v.get("id") in archive and stage in ("scheduled", "live"))
                    or (kind == "episode" and stage == "revision")
                    or stale_inflight
                    or dead_review):
                await db.delete_video(v["id"]); n += 1
        if n:
            BUS.emit("system", "gc", f"auto-pruned {n} board cards (segments, revision orphans, stale cards)")
        # DISK GC: per-segment intermediates (wavs, demos, b-rolls, screenshots, snippets, tapes,
        # concat lists) are the heavy disk hog (4.5G → ENOSPC) and are useless once the episode mp4
        # exists. Under the per-episode schema they ALL live in reapable scratch/ (per-episode) and
        # _scratch/ (cross-episode); nothing else is ever a GC candidate. Tombstone (→ _trash/) any
        # older than 30min instead of unlinking, so a mistaken reap is recoverable, not data loss.
        # This walks only the schema's reapable surface, so it can never touch a real schema dir,
        # render, audio file, or back-compat symlink — the interim `_gc_unsafe` guard is gone.
        try:
            import shutil as _sh2
            inter_freed = inter_n = 0
            protected_paths, protection_complete = _editor_timeline_asset_paths_checked()
            for f in reapable_scratch():
                try:
                    if _is_editor_timeline_protected_asset(f, protected_paths):
                        continue
                    if now - f.stat().st_mtime > 1800:  # 30min
                        inter_freed += tombstone(f); inter_n += 1
                except Exception:
                    pass
            if inter_n:
                BUS.emit("system", "gc", f"freed {inter_freed//1024//1024}MB — tombstoned {inter_n} spent scratch intermediates")
            # FREE-SPACE GUARD: if disk is critically low, tombstone the oldest real episode renders
            # (keep 4) → _trash/ via tombstone_render(), recoverable safe-delete, never unlink.
            free_gb = _sh2.disk_usage(str(ASSETS)).free / 1e9
            # FAIL SAFE + RE-SCAN before the destructive trim (see purge_disk for the twin guard):
            #   1. The ENTRY scan must have been complete — `protection_complete`. An incomplete entry
            #      scan (a timeline JSON we couldn't parse) means we never knew the full referenced set,
            #      so we must protect everything and skip the trim.
            #   2. The protected set captured at the top is STALE by now: the scratch reap above takes
            #      wall-clock time, during which a new Editor Bay timeline can be saved referencing a
            #      render this trim is about to delete (the TOCTOU this ticket guards). So re-scan, and
            #      require the FRESH scan to also be complete — `fresh_complete`. If either scan is
            #      incomplete, skip the trim entirely; disk pressure is recoverable, a deleted in-use
            #      render is not. When both are complete we OR the two sets so a render protected by
            #      EITHER survives.
            if protection_complete and free_gb < 2.0:
                fresh_paths, fresh_complete = _editor_timeline_asset_paths_checked()
                if fresh_complete:
                    protected_paths = protected_paths | fresh_paths
                    for old in _old_episode_renders()[4:]:
                        try:
                            if _is_editor_timeline_protected_asset(old, protected_paths):
                                continue
                            tombstone_render(old)  # safe-delete → _trash/, recoverable (not unlink)
                        except Exception:
                            pass
                    BUS.emit("system", "gc", f"low disk ({free_gb:.1f}GB) — trimmed old episodes to last 4")
        except Exception:
            pass
    except Exception:
        pass


async def run_factory_loop():
    STATE["started"] = time.time()
    BUS.emit("factory", "boot", "factory online — autonomous production starting")
    _cycles = 0
    while True:
        try:
            await _PAUSE.wait()
            _cycles += 1
            if _cycles % 3 == 0:  # self-maintain the board every few cycles
                await _gc_segments()
            # AUTO-PUBLISH: once YouTube creds exist, the system publishes approved episodes itself
            # (private by default) — fully autonomous, no human in the loop. Dormant until configured.
            review = await db.list_videos(stage="review")
            pending_eps = [v for v in review if v.get("kind") == "episode"]
            # Only touch the publisher when there's actually something to publish — otherwise the
            # missing-module ModuleNotFoundError fires every single cycle and gets swallowed silently.
            if pending_eps:
                try:
                    import services.abn_youtube as ytmod
                except ModuleNotFoundError:
                    # The auto-publish feature is unimplemented (services/abn_youtube.py absent). This
                    # is a config/build state, not a runtime error — surface it ONCE so the operator
                    # knows the feature is broken rather than burying it in per-cycle error spam.
                    if not STATE.get("_ytmod_warned"):
                        STATE["_ytmod_warned"] = True
                        BUS.emit("publisher", "unavailable",
                                 f"auto-publish disabled: services/abn_youtube.py not installed — "
                                 f"{len(pending_eps)} episode(s) waiting in review will NOT auto-publish")
                    ytmod = None
                if ytmod is not None:
                    try:
                        if ytmod.is_configured():
                            for e in pending_eps:
                                _set("publishing", "publisher", f"auto-publishing {e['id']} to YouTube", e["id"])
                                import urllib.request as _u
                                _u.urlopen(_u.Request("http://127.0.0.1:8000/api/agenticnews/episodes/" + e["id"] + "/publish",
                                                      data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=300)
                            pending_eps = []  # cleared via publish
                    except Exception as ex:
                        BUS.emit("publisher", "error", f"auto-publish: {ex}")
            # WORKSHOP MODE: do NOT park at "awaiting approval" — keep cooking modular segments/episodes
            # continuously. The review backlog is managed by the GC (prunes old ones); each new episode
            # is fresh material to workshop + harvest segments/shorts from. Only auto-publish if creds exist.
            if pending_eps:
                # Backlog cap. Was 12 — which, combined with the freshness-ledger bug (timestamps
                # never refreshed on re-render), let the loop flood review with duplicate episodes
                # overnight. Until auto-publish is live there is zero value in >4 unreviewed eps.
                if len(pending_eps) > 4:
                    STATE.update(stage="idle", actor="-", detail=f"{len(pending_eps)} episodes in review — letting GC catch up")
                    await asyncio.sleep(120)
                    continue
            # FORMAT ROTATION: every 6th episode, run a LORE story on a rotating subject (the high-value
            # ColdFusion-register tentpole) instead of a news roundup. Cheap to vary, big watch-time upside.
            lore_subject = None
            try:
                import services.abn_memory as _mm
                ecount = int(_mm.stats().get("episodes", 0))
                if ecount % 6 == 5:
                    lore_subject = _LORE_SUBJECTS[(ecount // 6) % len(_LORE_SUBJECTS)]
            except Exception:
                pass
            # WATCHDOG: a full episode (research → render → review) completes in ~20-25 min. Cap it at
            # 45 min so a HANG in the post-render path (observed: an episode rendered a complete mp4 then
            # wedged before reaching review, DB stuck at 'scripting', process at 0% CPU forever) force-
            # fails instead of blocking the loop indefinitely. The orphaned mp4/row get GC'd.
            try:
                result = await asyncio.wait_for(produce_one_episode(force_lore=lore_subject), timeout=45 * 60)
            except asyncio.TimeoutError:
                result = None
                BUS.emit("system", "error", "episode produce exceeded 45min watchdog — force-failed, moving on")
            # workshop cadence: short pause between episodes to keep the line hot; longer only when
            # there's genuinely no fresh material (None) so we don't spin on an empty feed.
            await asyncio.sleep(15 if result else 300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            BUS.emit("factory", "error", f"loop error: {e}")
            await asyncio.sleep(30)


# ---------------- REVISUALIZE (redo visuals, keep script + VO) ----------------
async def revisualize_episode(ep_id):
    """Re-skin an already-rendered episode with the NEW visual grammar (pacing law, framed press
    clippings over animated bgs, kinetic inserts, no keyword rectangles) while keeping the ENTIRE
    original audio mix (VO + bed + ducking + loudnorm) — 'redo all these stories with the script
    and VO we already have' (John, 06-09).

    Method: same segment durations + same transitions ⇒ identical internal timing ⇒ the original
    mp4's audio track muxes straight onto the new video. The logo sting is DROPPED from the
    re-render (sting length varied across old renders); the audio is front-trimmed by the measured
    duration delta so VO/caption sync is exact arithmetic, not guesswork."""
    tlf = asset_path(ep_id, "timeline")
    orig = asset_path(ep_id, "episode")
    if not tlf.exists() or not orig.exists():
        raise RuntimeError(f"{ep_id}: missing timeline or mp4")
    # keep the pristine original timeline once (idempotent re-runs) — renders/ singleton
    bak = asset_path(ep_id, "assembled", "timeline.orig", ext="json")
    if not bak.exists():
        bak.write_text(tlf.read_text())
    tl = json.loads(bak.read_text())
    d_orig = await _dur(orig)

    # 1) preserve the finished audio mix before anything overwrites the mp4 (renders/ keeper)
    aud = asset_path(ep_id, "assembled", "origaudio", ext="m4a")
    code, log = await _sh(f'ffmpeg -y -i {shlex.quote(str(orig))} -vn -c:a copy {shlex.quote(str(aud))}', timeout=120)
    if code != 0 or not aud.exists():
        raise RuntimeError(f"{ep_id}: audio extract failed: {log[-200:]}")

    # 2) rebuild each segment's visuals from assets already on disk + fresh kinetic inserts
    bgs = await _animated_bg(ep_id, n=4)
    segments = []
    for i, seg in enumerate(tl.get("segments", [])):
        sid = Path((seg.get("audio") or {}).get("vo", {}).get("src", "")).stem or seg.get("segmentId") or f"{ep_id}_s{i}"
        words = seg.get("wordTimestamps") or []
        transcript = " ".join(w.get("w", "") for w in words)
        # discover each segment's existing assets THROUGH THE GATEWAY (schema paths), falling back to
        # the legacy flat name (kept as a back-compat symlink by the migration) so episodes rendered
        # before the migration still re-skin. `have(kind, legacy)` returns the schema URL or None.
        def have(kind, legacy):
            p = asset_path_from_slug(sid, kind)
            if p.exists() and p.stat().st_size > 1024:
                return _asset_url(p)
            lp = ASSETS / legacy
            if lp.exists() and lp.stat().st_size > 1024:
                return f"/agenticnews-assets/{legacy}"
            return None
        ui = have("ui", f"{sid}_ui.mp4")
        demo = have("demo", f"{sid}_demo.mp4")
        shotpng = have("src", f"{sid}_src.png")
        card = have("card", f"{sid}_card.png") or (shotpng or "")
        kinetic = None
        if i % 3 == 1 and transcript:
            try:
                kinetic = await _kinetic_insert(seg.get("title", ""), transcript, seg.get("sourceUrl", ""), sid)
            except Exception as ex:
                BUS.emit("editor-agent", "error", f"revis seg {i+1}: kinetic failed (non-fatal) {ex!r}"[:120], episode_id=ep_id)
        # vo_path: prefer the schema voice asset; fall back to the legacy flat wav (symlinked).
        _vo = asset_path_from_slug(sid, "voice")
        vo_url = _asset_url(_vo) if _vo.exists() else f"/agenticnews-assets/{sid}.wav"
        segments.append({"segment_id": seg.get("segmentId") or sid, "title": seg.get("title", ""),
                         "source_url": seg.get("sourceUrl", ""), "script": transcript, "words": words,
                         "duration": seg.get("durationSec", 0), "vo_path": vo_url,
                         "screenshot": shotpng, "card": card, "demo": demo, "ui": ui, "kinetic": kinetic})
    new_tl = _build_timeline(ep_id, 0, segments, animated_bg=bgs)
    # original audio carries VO/bed/sfx — render the new video SILENT and drop the sting
    new_tl["logo"] = None
    new_tl["musicBed"] = None
    new_tl["sfx"] = None
    for s in new_tl["segments"]:
        s["audio"] = {}
    new_tl["title"] = tl.get("title", new_tl.get("title"))

    # 3) silent video render (direct remotion call — _render_remotion's loudnorm assumes audio)
    props = asset_path(ep_id, "timeline")
    atomic_save(props, new_tl)
    vid = asset_path(ep_id, "scratch", "revis", ext="mp4")  # intermediate, unlinked after mux
    try:
        _cc = int(os.getenv("ABN_RENDER_CONCURRENCY") or max(3, (os.cpu_count() or 4) // 2))
    except (TypeError, ValueError):
        _cc = 4
    BUS.emit("editor-agent", "remotion.start", f"REVISUALIZE render ({ep_id})", episode_id=ep_id)
    code, log = await _sh(f'cd {shlex.quote(str(REMOTION_DIR))} && npx remotion render Episode {shlex.quote(str(vid))} '
                          f'--props={shlex.quote(str(props))} --codec=h264 --crf 23 --concurrency={_cc} --log=error 2>&1',
                          timeout=1800)
    if code != 0 or not vid.exists():
        raise RuntimeError(f"{ep_id}: remotion exit {code}: {log[-300:]}")

    # 4) mux: front-trim the original audio by the measured delta (= the dropped sting), normalize px
    d_new = await _dur(vid)
    trim = max(0.0, round((d_orig or 0) - (d_new or 0), 2))
    out = asset_path(ep_id, "episode")
    code, log = await _sh(
        f'ffmpeg -y -i {shlex.quote(str(vid))} -ss {trim} -i {shlex.quote(str(aud))} '
        f'-map 0:v -map 1:a -vf format=yuv420p -colorspace bt709 -color_primaries bt709 '
        f'-color_trc bt709 -color_range tv -c:v libx264 -crf 20 -preset veryfast '
        f'-c:a copy -shortest -movflags +faststart {shlex.quote(str(out))}', timeout=900)
    vid.unlink(missing_ok=True)
    if code != 0:
        raise RuntimeError(f"{ep_id}: mux failed: {log[-300:]}")
    d_final = await _dur(out)
    BUS.emit("editor-agent", "revis.done",
             f"{ep_id} revisualized: {d_final:.0f}s (audio trim {trim}s, kinetic inserts in)", episode_id=ep_id)
    try:
        await db.update_video(ep_id, {"stage": "review", "note": "revisualized: new visual grammar"})
    except Exception:
        pass
    return _asset_url(out), d_final


_task = None


async def start_factory():
    global _task
    # point the cards at the cinematic-background pool immediately (uses any already-generated bgs),
    # then top up the pool in the background (Codex/PRO image_gen — never blocks factory startup).
    if _V2_VISUALS:
        try:
            _v2cards._ASSETS_DIR = _cards_assets_dir()
            asyncio.create_task(asyncio.to_thread(_ensure_card_backgrounds))
        except Exception:
            pass
    if _task is None or _task.done():
        _task = asyncio.create_task(run_factory_loop())
    return _task


async def stop_factory():
    global _task
    if _task:
        _task.cancel()
        _task = None


def pause(): _PAUSE.clear(); STATE["running"] = False; BUS.emit("factory", "paused", "factory paused by operator")
def resume(): _PAUSE.set(); STATE["running"] = True; BUS.emit("factory", "resumed", "factory resumed")
