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
import urllib.request
import urllib.parse
from pathlib import Path
from collections import deque

import services.agenticnews as db

# v2 anti-slop visual system: deconstruct VO into scenes → designed cards instead of blog
# screenshots. Imported defensively so a v2 issue can never break the running v1 producer.
try:
    from factory.formats.scenes import tag_scenes, direct_visuals, hero_number, hero_number_label
    from factory.formats.types import SceneRole
    from factory.formats import cards as _v2cards
    _V2_VISUALS = True
except Exception as _v2e:  # pragma: no cover
    _V2_VISUALS = False

ASSETS = db.ASSETS_DIR
VOICE = str(ASSETS / "john_voice.safetensors")
WPM = 195
_FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
# v2 visuals on by default; ABN_V2_VISUALS=0 falls back to the legacy screenshot chain.
_USE_V2_VISUALS = os.getenv("ABN_V2_VISUALS", "1") == "1"
SEG_WORDS = 200           # ~63s spoken per segment — longer beats for depth + the 11min ad-RPM target
N_SEGMENTS = 11           # target episode size: 11 stories × ~63s + sting ≈ 11-12min

# ─────────────────────── HARD VALUATION GATES (non-negotiable) ───────────────────────
# These are ENFORCED parameters, not aspirations. An episode that violates a gate does NOT
# reach 'review' — it is rejected/looped. This is the contract that defines "satisfactory work".
MIN_EPISODE_SEC = 600     # 10:00 HARD FLOOR. Render shorter than this is auto-rejected (RPM/mid-roll).
MIN_SEGMENTS    = 8       # floor on segment count so we START long enough to clear MIN_EPISODE_SEC.
                          # 8 × ~63s ≈ 8.4min of VO + cold-open + sting → clears 10min with captions/pacing.

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


# stale / evergreen topics to EXCLUDE — we want current events, not explainers
STALE_PAT = re.compile(r'\b(explained|visualization|inevitabilism|introduction|guide|tutorial|'
                       r'what is|how to|learning to|fundamentals|101|deep dive into|history of|'
                       r'ask hn|show hn|launch hn)\b', re.I)


def _is_stale(it):
    return bool(STALE_PAT.search(it["title"]))


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
    # FLYWHEEL negative signal: deprioritize stories similar to ones the operator rejected
    penalty = 0
    try:
        import services.abn_memory as mem
        penalty = min(4, mem.rejection_penalty(it["title"]))  # cap so one bad topic doesn't nuke a whole vein
    except Exception:
        pass
    return it["pts"] / 100 + lab + shipping + drama + recency - penalty


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


def _research_sync(title, url, angle=None):
    """Deep-dive via the RESEARCHER EXPERT. For a deepdive FACET, pass `angle` so each of the 3 facets
    researches its OWN sub-topic (mechanism vs verdict vs problem) → distinct briefs → non-repeating
    scripts. Without an angle, researches the tool overall."""
    try:
        import services.abn_experts as experts
        if angle:
            q = (f"AI/dev tool: \"{title}\" (source: {url}). This is ONE facet of a deep-dive. Research "
                 f"ONLY this specific angle in depth — ignore the tool's general pitch: {angle}. Give the "
                 f"mechanism, numbers, and tradeoffs SPECIFIC to this angle.")
        else:
            q = f"AI/dev item: \"{title}\" (source: {url}). What's the core concept + the story angle a builder needs?"
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


# ---------------- VO (Chatterbox on Replicate, Pocket-TTS fallback) ----------------
# Chatterbox (resemble-ai/chatterbox) is an expressive, emotion-controllable TTS that
# delivers the dry-confident-energetic tech-narrator tonality we want (think Fireship /
# ThePrimeagen energy) — measurably more pitch movement and natural phrasing than the
# flat Pocket-TTS baseline. We try it first and fall back to Pocket-TTS if anything fails,
# because the VO is essential: a missing wav kills the segment (whisper alignment reads it).
#
# Tunable via env (sane defaults baked in):
#   ABN_TTS                — "chatterbox" (default) | "pocket" to force the local fallback
#   ABN_CHATTERBOX_VERSION — pinned model version hash
#   ABN_TTS_EXAGGERATION   — 0.5 neutral; ~0.6 = energetic-but-stable (default 0.6)
#   ABN_TTS_CFG            — CFG/pace weight; lower = faster, snappier (default 0.4)
#   ABN_TTS_TEMPERATURE    — sampling temperature (default 0.8)
# Voice consistency: drop a short reference clip at agenticnews_assets/john_voice_ref.wav
# (or set ABN_VOICE_REF) and every segment clones it for a consistent narrator across the
# episode. Without it, Chatterbox uses its built-in voice (still consistent run-to-run).
_CHATTERBOX_VERSION = os.getenv(
    "ABN_CHATTERBOX_VERSION",
    "1b8422bc49635c20d0a84e387ed20879c0dd09254ecdb4e75dc4bec10ff94e97")
VOICE_REF = os.getenv("ABN_VOICE_REF", str(ASSETS / "john_voice_ref.wav"))


def _chatterbox_chunks(text, max_words=55):
    """Split into sentence-grouped chunks ≤max_words. Chatterbox REPEATS/loops on long input
    (>~60 words), so we synth per-chunk then concat — fixes the 'or just mentioned or just
    mentioned' stutter seen on full 200-word scripts."""
    import re
    sents = re.split(r'(?<=[.!?])\s+', (text or "").strip())
    chunks, cur, n = [], [], 0
    for s in sents:
        w = len(s.split())
        if cur and n + w > max_words:
            chunks.append(" ".join(cur)); cur, n = [], 0
        cur.append(s); n += w
    if cur:
        chunks.append(" ".join(cur))
    return chunks or [text]


# Phrase used to MINT the channel's single locked narrator the first time we ever synth.
# Chatterbox with no audio_prompt invents a NEW random speaker per call — that is the
# "5-6 different voices in one episode" bug. The fix: synth this once (seeded), cache it to
# VOICE_REF, then clone EVERY subsequent chunk from it. One narrator, channel-wide, forever.
_VOICE_SEED_PHRASE = ("Welcome back to Agentic Builder News. Today we are breaking down the "
                      "tools, the releases, and the moves that actually matter for people building "
                      "with AI right now. Let's get into it.")


def _ensure_voice_ref() -> Path | None:
    """Guarantee a single locked narrator reference clip exists. Mints one (seeded, no ref)
    on first use, caches to VOICE_REF. Returns the path, or None if minting failed."""
    ref = Path(VOICE_REF)
    if ref.exists() and ref.stat().st_size > 1024:
        return ref
    # mint the founding narrator clip — seeded so it is reproducible, NO audio_prompt (this
    # one call is allowed to invent the voice; every future call clones THIS clip).
    if _chatterbox_one(_VOICE_SEED_PHRASE, ref, _minting=True):
        return ref if (ref.exists() and ref.stat().st_size > 1024) else None
    return None


def _chatterbox_one(chunk, out: Path, _minting: bool = False):
    """Synth ONE short chunk via Replicate Chatterbox → wav at `out`. True/False.

    Unless minting the founding reference, ALWAYS clones the locked narrator (VOICE_REF) so
    every chunk/segment/episode is the same voice."""
    tok = os.getenv("REPLICATE_API_TOKEN")
    if not tok:
        return False
    try:
        inp = {"prompt": chunk,
               "exaggeration": float(os.getenv("ABN_TTS_EXAGGERATION", "0.6")),
               "cfg_weight": float(os.getenv("ABN_TTS_CFG", "0.4")),
               "temperature": float(os.getenv("ABN_TTS_TEMPERATURE", "0.8")), "seed": 7}
        # clone the locked narrator on EVERY real chunk. Only the founding mint skips this.
        ref = Path(VOICE_REF)
        if not _minting and not ref.exists():
            minted = _ensure_voice_ref()
            ref = minted if minted else ref
        if not _minting and ref.exists():
            import base64
            ext = ref.suffix.lstrip(".") or "wav"
            inp["audio_prompt"] = f"data:audio/{ext};base64," + base64.b64encode(ref.read_bytes()).decode()
        body = {"version": _CHATTERBOX_VERSION, "input": inp}
        req = urllib.request.Request("https://api.replicate.com/v1/predictions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Prefer": "wait"})
        r = json.load(urllib.request.urlopen(req, timeout=180))
        if r.get("status") not in ("succeeded", "failed", "canceled"):
            get_url = (r.get("urls") or {}).get("get")
            for _ in range(40):
                time.sleep(2)
                r = json.load(urllib.request.urlopen(urllib.request.Request(get_url, headers={"Authorization": f"Bearer {tok}"}), timeout=30))
                if r.get("status") in ("succeeded", "failed", "canceled"):
                    break
        if r.get("status") != "succeeded":
            return False
        u = r.get("output"); u = (u[0] if isinstance(u, list) and u else u)
        if not isinstance(u, str):
            return False
        urllib.request.urlretrieve(u, str(out))
        return out.exists() and out.stat().st_size > 1024
    except Exception:
        return False


def _chatterbox_sync(text, out: Path):
    """Chunked + normalized Chatterbox TTS → 24kHz mono wav at `out`. Chunks long text (avoids the
    repetition bug), concats, then normalizes to fix the 0dB clipping. False → caller falls back."""
    import subprocess
    chunks = _chatterbox_chunks(text)
    parts = []
    try:
        for i, ch in enumerate(chunks):
            p = out.with_name(f"{out.stem}_c{i}.wav")
            if not _chatterbox_one(ch, p):
                for q in parts:
                    try: q.unlink()
                    except Exception: pass
                return False
            parts.append(p)
        raw = out.with_name(f"{out.stem}_raw.wav")
        if len(parts) == 1:
            parts[0].rename(raw)
        else:
            # use ABSOLUTE paths (no cwd) so the concat list resolves regardless of working dir
            listf = out.with_name(f"{out.stem}_list.txt")
            listf.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                            "-c", "copy", str(raw)], capture_output=True, timeout=120)
            try: listf.unlink()
            except Exception: pass
        if not raw.exists():
            return False
        # NORMALIZE: fix the 0dB clipping — target -16 LUFS w/ a true-peak limiter, resample 24k mono
        subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-af",
                        "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=24000", "-ac", "1", str(out)],
                       capture_output=True, timeout=120)
        for q in parts + [raw]:
            try: q.unlink()
            except Exception: pass
        return out.exists() and out.stat().st_size > 1024
    except Exception:
        for q in parts:
            try: q.unlink()
            except Exception: pass
        return False


async def _voice(text, name):
    out = ASSETS / f"{name}.wav"
    engine = os.getenv("ABN_TTS", "chatterbox").lower()

    # 1) Preferred: expressive Chatterbox (skipped if forced to pocket)
    if engine != "pocket":
        ok = await asyncio.to_thread(_chatterbox_sync, text, out)
        if ok and out.exists():
            dur = await _dur(out)
            if dur > 0:
                return f"/agenticnews-assets/{out.name}", dur
        # chatterbox failed/empty — clear any partial file before falling back
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass

    # 2) Fallback: local Pocket-TTS (essential — VO must exist for whisper alignment)
    cmd = f'pocket-tts generate --text {shlex.quote(text)} --output-path {shlex.quote(str(out))} --quiet'
    if Path(VOICE).exists():
        cmd += f' --voice {shlex.quote(VOICE)}'
    code, log = await _sh(cmd, timeout=300)
    if code != 0 or not out.exists():
        raise RuntimeError(f"tts: {log[-200:]}")
    dur = await _dur(out)
    return f"/agenticnews-assets/{out.name}", dur


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


def _align_sync(wav_path):
    try:
        m = _get_whisper()
        segs, _ = m.transcribe(str(wav_path), word_timestamps=True, language="en")
        words = []
        for s in segs:
            for w in (s.words or []):
                words.append({"w": (w.word or "").strip(), "s": round(float(w.start), 2), "e": round(float(w.end), 2)})
        return words
    except Exception:
        return []


async def _align(wav_name):
    wav = ASSETS / f"{wav_name}.wav"
    words = await asyncio.to_thread(_align_sync, wav)
    if words:
        return words
    # fallback: openai-whisper CLI (slow) if faster-whisper unavailable
    outdir = ASSETS / "align"; outdir.mkdir(exist_ok=True)
    cmd = (f'whisper {shlex.quote(str(wav))} --model tiny.en --word_timestamps True '
           f'--output_format json --output_dir {shlex.quote(str(outdir))} --language en 2>/dev/null')
    await _sh(cmd, timeout=300)
    jf = outdir / f"{wav_name}.json"
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
    out = ASSETS / f"{name}_src.png"
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
                try: out.unlink()
                except Exception: pass
                return None
            if not await _shot_is_usable(out):
                BUS.emit("editor-agent", "shot.reject", "screenshot near-blank — discarded")
                try: out.unlink()
                except Exception: pass
                return None
            return f"/agenticnews-assets/{out.name}"
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
    tape = ASSETS / f"{name}.tape"
    out = ASSETS / f"{name}_demo.mp4"
    # write the snippet to a file and DISPLAY it with bat (syntax-highlighted) — no execution,
    # so we never get 'command not found' errors. This shows clean code, not a broken shell.
    snippet = ASSETS / f"{name}_snippet.py"
    snippet.write_text("\n".join(lines) + "\n")
    bat = "bat" if Path("/opt/homebrew/bin/bat").exists() else "cat"
    body = [f"Output {out.name}", "Set Width 1920", "Set Height 1080", "Set FontSize 34",
            "Set TypingSpeed 42ms", "Set Theme \"Dracula\"",
            'Type "# AgenticBuilderNews — live build"', "Enter", "Sleep 500ms",
            # type the bat command that renders the code, then run it ONCE (bat just prints, never errors)
            f'Type "{bat} --style=numbers --color=always {snippet.name}"', "Enter", "Sleep 2500ms"]
    tape.write_text("\n".join(body) + "\n")
    # VHS must run with cwd in ASSETS (relative Output) to avoid the path-parse bug
    code_, log = await _sh(f'cd {shlex.quote(str(ASSETS))} && vhs {shlex.quote(tape.name)} 2>&1', timeout=120)
    if out.exists():
        return f"/agenticnews-assets/{out.name}"
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
    out = ASSETS / f"{name}_demo.mp4"
    tape = ASSETS / f"{name}_real.tape"
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
        owner_repo = clone_url.rsplit("/", 2)[-2] + "/" + clone_url.rsplit("/", 1)[-1].replace(".git", "")
        # The tape runs in repo_dir (already cloned). Every command here is READ-ONLY inspection.
        # We re-show the clone command for narrative honesty, but point it at the already-fetched
        # copy via `echo` of the real result so we don't re-hit the network in the footage.
        body = [
            f"Output {out.name}",
            "Set Width 1920", "Set Height 1080", "Set FontSize 30",
            "Set TypingSpeed 28ms", 'Set Theme "Dracula"', "Set Padding 40",
            f'Type "# AgenticBuilderNews — real repo, real output: {owner_repo}"', "Enter", "Sleep 600ms",
            # narrative clone: type the command for the viewer, but DON'T execute a real second clone
            # (the repo is already fetched above; re-running streams pages of progress that collide with
            # the next command). A trailing " #" comments it out so the shell shows it but runs nothing,
            # then we print one clean completion line. The REAL output comes from ls/tree/log/README below.
            # NOTE: leading '#' so the shell does NOT execute a real second clone (which streams pages of
            # progress that collide with the next command). It still DISPLAYS the clone command verbatim.
            # Keep echo args UNQUOTED — VHS's tape parser chokes on nested escaped quotes in Type "...".
            # show the clone command as a comment (no real second clone), then go straight to the REAL
            # ls output — the actual repo contents are the proof, no need for a fake echo confirmation line.
            f'Type "# $ git clone {clone_url}"', "Enter", "Sleep 900ms",
            'Type "ls"', "Enter", "Sleep 2000ms",
        ]
        # show the project structure (one level, dirs first) — real `ls`/`tree` output
        if shutil.which("tree"):
            body += ['Type "tree -L 1 -C"', "Enter", "Sleep 2200ms"]
        # real recent history — proves it's a live repo, not a mockup
        body += ['Type "git log --oneline -5"', "Enter", "Sleep 2200ms"]
        # real README head — the actual words the maintainers wrote
        if readme:
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
            return f"/agenticnews-assets/{out.name}"
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
    out = ASSETS / f"{name}_card.png"
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
    return f"/agenticnews-assets/{out.name}"


# ---------------- THUMBNAIL (Flux-generated background + bold text overlay — the real deal) ----------------
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
                dest = ASSETS / f"{name}_broll.mp4"
                urllib.request.urlretrieve(u, str(dest))
                return f"/agenticnews-assets/{dest.name}" if dest.exists() else None
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
                await asyncio.to_thread(urllib.request.urlretrieve, clip_url, str(dest))
            else:  # already a local /agenticnews-assets path
                src = ASSETS / Path(str(clip_url)).name
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
        p = ASSETS / f"{name}_still.png"
        urllib.request.urlretrieve(still_url, str(p))
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
    bg = ASSETS / f"{ep_id}_thumb_bg.png"
    out = ASSETS / f"{ep_id}_thumb.png"
    if bg_url:
        try:
            urllib.request.urlretrieve(bg_url, str(bg))
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
        return f"/agenticnews-assets/{out.name}"
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
        n = min(max_n, math.ceil(span / 7.0))
    slot = span / n
    out = []
    for j in range(n):
        s = round(t0 + j * slot, 2)
        e = round(t0 + (j + 1) * slot, 2) if j < n - 1 else round(t1, 2)
        out.append((s, e, round(lead + j * slot, 2)))
    return out


def _hi_box(kw):
    """Highlight/lower-third box for a keyword pop, positioned by type so number/model/tool pops don't
    all stack in one spot (variety in WHERE emphasis lands)."""
    typ = kw.get("type")
    box = ({"x": .06, "y": .55, "w": .58, "h": .12} if typ == "number"
           else {"x": .06, "y": .22, "w": .64, "h": .10} if typ == "model"
           else {"x": .10, "y": .38, "w": .54, "h": .09})
    return {**box, "color": kw["color"], "opacity": .85, "borderWidth": 3, "label": kw["text"]}


def _plan_shots(duration, screenshot, card, words, keywords, source_url, demo=None, ui=None, seg_index=0):
    """Mixed-media: live UI scroll → Ken-Burns artifact punch-ins → live CODE DEMO. New visual every 4-7s.

    Dynamism rules (the variety pass John asked for):
      • ONE shared Ken-Burns picker per segment → every sub-shot gets a distinct, direction-alternating
        move (push-in / pull-out / lateral-pan never repeat back-to-back). No more identical slow zooms.
      • Per-segment RHYTHM variation seeded by seg_index → the UI/demo split, the number of artifact
        windows, and the card position all shift between segments so no two segments look templated.
      • Keyword pops attach a tight highlight box to WHICHEVER beat the keyword is spoken in (UI,
        artifact, or demo) — emphasis lands on the spoken word, not only when it falls in the middle.
      • Hard 4–7s pacing (≤8s) on every beat via _chop."""
    if screenshot and not (ASSETS / Path(screenshot).name).exists():
        screenshot = None
    if not (card and (ASSETS / Path(card).name).exists()):
        card = screenshot
    has_demo = demo and (ASSETS / Path(demo).name).exists()
    has_ui = ui and (ASSETS / Path(ui).name).exists()
    pick = _kb_picker(seed=seg_index)  # shared across UI + artifacts + demo so moves never repeat

    # RHYTHM VARIATION: nudge the structural split per segment so the episode isn't templated.
    # UI moat still dominates the open (~40-50%); demo still closes; artifacts fill the middle.
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
                shot["highlight"] = _hi_box(kw)
            shots.append(shot)

    # ── Artifact middle (Ken-Burns on stills/cards) ──────────────────────────────────────────────
    # when artifacts carry the WHOLE segment (no UI/demo) allow more windows so we don't sit on 8s holds.
    art_cap = 5 if (has_ui or has_demo) else 8
    n = max(2, min(art_cap, int(artifact_dur / 5.5))) if artifact_dur > 0 else 0
    if n:
        slot = artifact_dur / n
        if slot > 8.0:                       # keep middle beats under the cap too
            import math
            n = min(art_cap, math.ceil(artifact_dur / 6.5)); slot = artifact_dur / n
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
            kw = kw_in(s, e)
            if kw and not is_card:
                shot["highlight"] = _hi_box(kw)
            shots.append(shot)

    # ── Live code demo (close) ────────────────────────────────────────────────────────────────────
    if has_demo:
        DEMO_LEADIN = 3.0  # skip the comment-header typing intro — open on real code
        for j, (ds, de, off) in enumerate(_chop(demo_start, round(duration, 2), target=6.0, max_n=4, lead=DEMO_LEADIN)):
            m = pick()
            # demo is a terminal — keep moves subtler (scale toward 1.0) but still rotate direction
            kb = {**m, "startScale": round(1.0 + (m["startScale"] - 1.0) * 0.5, 3),
                  "endScale": round(1.0 + (m["endScale"] - 1.0) * 0.5, 3)}
            shot = {"id": f"demo{j}", "type": "broll", "src": _disk(demo), "startSec": ds, "endSec": de,
                    "muteSource": True, "clipStartSec": off, "kenBurns": kb}
            kw = kw_in(ds, de)
            if kw:
                shot["highlight"] = _hi_box(kw)
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


def _v2_scene_cards(ep_id, seg_index, seg):
    """Generate v2 DESIGNED CARDS for a segment's scenes — the anti-slop replacement for the
    blog-screenshot visual. Deconstructs the VO into scenes, picks a shot per scene from the
    format catalog, and renders the designed cards (number/vs/quote/diagram). Returns an ordered
    list of /agenticnews-assets/ urls for the designed frames, or [] if v2 is off/unavailable.

    Best-effort: any failure returns [] so the legacy visual still renders (never breaks a render)."""
    if not (_V2_VISUALS and _USE_V2_VISUALS):
        return []
    try:
        from factory.formats import get_format
        from factory.contracts.stages import VideoFormat
        # all current production is roundup-style segments → PULSE catalog
        spec = get_format(VideoFormat.ROUNDUP)
        scenes = tag_scenes(seg.get("script", ""), segment_index=seg_index,
                            is_first_segment=(seg_index == 0), is_last_segment=False)
        shots = direct_visuals(scenes, spec)
        title = seg.get("title", "") or ""
        tool = re.split(r'\s+[—–:]\s+', title)[0][:24] if title else "this"
        out = []
        for sc, sh in zip(scenes, shots):
            nm = f"{ep_id}_s{seg_index}_v2sc{sc.index}"
            try:
                if sh.shot_type == "number_card":
                    h = hero_number(sc.text)
                    if not h:
                        continue
                    p = _v2cards.number_card(h, hero_number_label(sc.text, h), nm, ASSETS, _FONTS_DIR)
                elif sh.shot_type == "vs_card":
                    from factory.formats.scenes import comparison_target
                    rival = comparison_target(sc.text)
                    if not rival:
                        # no real competitor named → a generic "the alternative" card is slop;
                        # render the take/quote instead so the scene still gets a designed visual.
                        p = _v2cards.quote_card(sc.text, nm, ASSETS, _FONTS_DIR)
                    else:
                        p = _v2cards.vs_card(tool, rival, nm, ASSETS, _FONTS_DIR)
                elif sh.shot_type == "quote_card":
                    p = _v2cards.quote_card(sc.text, nm, ASSETS, _FONTS_DIR)
                elif sh.shot_type in ("diagram", "diagram_card"):
                    p = _v2cards.diagram_card(f"How {tool} works",
                                              [s.strip() for s in re.split(r'[.;]', sc.text) if s.strip()][:4],
                                              nm, ASSETS, _FONTS_DIR)
                else:
                    continue
                out.append(f"/agenticnews-assets/{Path(p).name}")
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
    for seg_index, seg in enumerate(segments):
        kws = _extract_keywords(seg["script"], seg["words"], tool_name=seg.get("title"), seg_duration=seg.get("duration", 0.0))
        shots = _plan_shots(seg["duration"], seg.get("screenshot"), seg["card"], seg["words"], kws, seg["source_url"], seg.get("demo"), seg.get("ui"), seg_index=seg_index)
        # V2 ANTI-SLOP: replace the 'artifact' shots (which were blog/page SCREENSHOTS — the slop)
        # with DESIGNED CARDS from the v2 scene→catalog system. Keeps all the timing/Ken-Burns/
        # highlight logic; only swaps the SOURCE image from a scrolled blog to a designed frame.
        v2_cards = _v2_scene_cards(ep_id, seg_index, seg)
        if v2_cards:
            ci = 0
            for sh in shots:
                if sh.get("type") == "artifact":
                    card_url = v2_cards[ci % len(v2_cards)]; ci += 1
                    cf = ASSETS / Path(card_url).name
                    if cf.exists() and cf.stat().st_size > 1024:
                        sh["src"] = _disk(card_url)
                        # designed cards read best with a gentle hold, not an aggressive Ken-Burns
                        sh["kenBurns"] = {"startScale": 1.0, "endScale": 1.05, "startX": .5,
                                          "startY": .5, "endX": .5, "endY": .5, "easing": "easeOut"}
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
                    bgfile = ASSETS / Path(str(bg)).name if "broll_library" not in str(bg) else ASSETS / "broll_library" / Path(str(bg)).name
                    if bgfile.exists() and bgfile.stat().st_size > 4096:
                        sh["type"] = "broll"; sh["src"] = _disk(bg); sh["muteSource"] = True
                        sh["clipStartSec"] = 0.5  # skip any i2v warm-up frame
        pops = [{"word": k["text"], "s": k["s"], "atSec": k["s"], "durationSec": min(2.4, k["e"] - k["s"] + 1.5), "color": k["color"]} for k in kws]
        tsegs.append({
            "segmentId": seg["segment_id"], "title": seg["title"], "sourceUrl": seg["source_url"],
            "shots": shots, "wordTimestamps": seg["words"], "keywordPops": pops,
            "audio": {"vo": {"src": _disk(seg["vo_path"]), "duration": seg["duration"]}},
            "lowerThirds": [{"startSec": 0.5, "durationSec": 4.0, "headline": _clip(seg["title"], 72), "sourceUrl": seg["source_url"]}],
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


async def _render_remotion(ep_id, timeline):
    if not (REMOTION_DIR / "node_modules").exists():
        raise RuntimeError("remotion not installed")
    props = ASSETS / f"{ep_id}_timeline.json"
    props.write_text(json.dumps(timeline))
    out = ASSETS / f"{ep_id}_episode.mp4"
    # --crf 23 ≈ visually-lossless for this flat-graphics content but ~half the file size of Remotion's
    # default high bitrate (episodes were 200MB+ → disk exhaustion on a 24/7 system). Big disk-saver.
    # Concurrency scales render throughput near-linearly for this flat-graphics content. Use most
    # of the box (cores-2, leaving headroom for the FastAPI app + OS) instead of a hardcoded 4 —
    # on a 10-core machine that roughly halves render time (the throughput bottleneck for a 24/7
    # channel). Overridable via ABN_RENDER_CONCURRENCY.
    try:
        _cc = int(os.getenv("ABN_RENDER_CONCURRENCY") or max(2, (os.cpu_count() or 4) - 2))
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
    norm = ASSETS / f"{ep_id}_norm.mp4"
    ncmd = (f'ffmpeg -y -i {shlex.quote(str(out))} '
            f'-vf format=yuv420p -colorspace bt709 -color_primaries bt709 '
            f'-color_trc bt709 -color_range tv '
            f'-c:v libx264 -crf 20 -preset veryfast -c:a copy '
            f'-movflags +faststart {shlex.quote(str(norm))} 2>&1')
    nc, nlog = await _sh(ncmd, timeout=600)
    if nc == 0 and norm.exists():
        norm.replace(out)
        BUS.emit("editor-agent", "render.normalize", "pixel format → yuv420p (Apple/YouTube-safe)", episode_id=ep_id)
    else:
        BUS.emit("editor-agent", "error", f"normalize pass failed (non-fatal): {nlog[-120:]}", episode_id=ep_id)
    # POST PASS: real sidechain ducking. Remotion bakes the VO+SFX; mix the music bed UNDER it with
    # sidechaincompress keyed off the VO so music auto-dips when narration plays (pro audio, not a flat bed).
    bed = timeline.get("musicBed")
    if bed:
        bedfile = ASSETS / Path(bed).name
        if bedfile.exists():
            ducked = ASSETS / f"{ep_id}_ducked.mp4"
            # [0:a]=baked VO/SFX (the sidechain key), [1:a]=music looped; duck music by the VO, then mix
            dcmd = (f'ffmpeg -y -i {shlex.quote(str(out))} -stream_loop -1 -i {shlex.quote(str(bedfile))} '
                    f'-filter_complex "[1:a]volume=0.22[m];[m][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[ duck];'
                    f'[0:a][duck]amix=inputs=2:duration=first:dropout_transition=0:weights=1 0.9[a]" '
                    f'-map 0:v -map "[a]" -c:v copy -c:a aac -shortest {shlex.quote(str(ducked))} 2>&1')
            dc, dlog = await _sh(dcmd, timeout=300)
            if dc == 0 and ducked.exists():
                ducked.replace(out)
                BUS.emit("editor-agent", "audio.duck", "music ducked under VO (sidechaincompress)", episode_id=ep_id)
            else:
                BUS.emit("editor-agent", "error", f"duck pass failed (non-fatal): {dlog[-120:]}", episode_id=ep_id)
    return f"/agenticnews-assets/{out.name}", await _dur(out)


# ---------------- ASSEMBLE (ffmpeg, real multi-segment) ----------------
async def _assemble_episode(ep_id, segments):
    """Each segment = source screenshot (or title card) with Ken-Burns + karaoke captions,
    over its VO. Concat all segments → one full episode MP4."""
    seg_clips = []
    for i, s in enumerate(segments):
        # deep-dive/animated segments may have no screenshot AND no card — fall back to the animated
        # bg or the logo so a visual always exists (was crashing on Path(None) → "no segment clips").
        visual = s.get("screenshot") or s.get("card") or s.get("ui") or "/agenticnews-assets/abn_logo.png"
        vis = ASSETS / Path(visual).name
        if not vis.exists():
            vis = ASSETS / "abn_logo.png"
        wav = ASSETS / Path(s["vo_path"]).name
        clip = ASSETS / f"{ep_id}_seg{i}.mp4"
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
    listf = ASSETS / f"{ep_id}_list.txt"
    listf.write_text("".join(f"file '{c}'\n" for c in seg_clips))
    final = ASSETS / f"{ep_id}_episode.mp4"
    code, log = await _sh(
        f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(listf))} -c copy {shlex.quote(str(final))}', timeout=180)
    if code != 0 or not final.exists():
        # fallback: re-encode concat
        code, log = await _sh(
            f'ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(listf))} -c:v libx264 -pix_fmt yuv420p -c:a aac {shlex.quote(str(final))}', timeout=300)
        if code != 0 or not final.exists():
            raise RuntimeError(f"concat: {log[-200:]}")
    return f"/agenticnews-assets/{final.name}", await _dur(final)


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
            beats = beats[:6]
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
    cold_open = await asyncio.to_thread(experts.ask, "narrator", f"Episode stories:\n{story_list}{proven}\n\nFind the connecting thesis and write the cold-open.")
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
        try: await db.update_video(sid, {"stage": "scripting"})
        except Exception: pass
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
        try: await db.update_video(sid, {"stage": "voicing", "hook": script[:80]})
        except Exception: pass
        return {"i": i, "sid": sid, "it": it, "script": script}

    async def _wave2_voice(s):
        i, sid = s["i"], s["sid"]
        BUS.emit("vo-agent", "vo.start", f"seg {i+1}: voicing", episode_id=ep_id, segment_id=sid)
        vo_path, dur = await _voice(s["script"], sid)  # VO is essential — if this fails the segment can't exist
        BUS.emit("vo-agent", "vo.done", f"seg {i+1}: {dur:.0f}s VO", episode_id=ep_id, segment_id=sid, artifact_url=vo_path, data={"duration": dur})
        try: await db.update_video(sid, {"stage": "assets"})
        except Exception: pass
        # alignment (karaoke captions) is an ENHANCEMENT — never let it kill the segment
        try:
            words = await _align(sid)
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
        return {**s, "screenshot": shot, "card": card, "demo": demo, "ui": ui}

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
    mp4 = dur = None
    # Remotion compositor first (Ken-Burns + karaoke captions + keyword pops + highlights),
    # fall back to the old ffmpeg slideshow only if Remotion fails.
    timeline = _build_timeline(ep_id, ep_idx, segments, animated_bg=animated_bg)
    mp4 = dur = None
    last_err = ""
    for attempt in (1, 2):  # Remotion can fail transiently (network asset fetch) — retry once
        try:
            mp4, dur = await _render_remotion(ep_id, timeline)
            BUS.emit("editor-agent", "remotion.done", f"Remotion render: {dur:.0f}s (attempt {attempt})", episode_id=ep_id)
            break
        except Exception as e:
            last_err = str(e)[:300]
            BUS.emit("editor-agent", "error", f"remotion attempt {attempt} failed: {last_err}", episode_id=ep_id)
    if mp4 is None:
        BUS.emit("editor-agent", "remotion.fallback", f"Remotion failed twice ({last_err[:100]}); ffmpeg fallback", episode_id=ep_id)
        try:
            mp4, dur = await _assemble_episode(ep_id, segments)
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
    pkg_results = await asyncio.gather(*[asyncio.to_thread(experts.ask, k, f"Episode stories: {stories}") for k in pkg_keys])
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
    await db.update_video(ep_id, {"stage": "review",
                                  "artifacts": arts,
                                  "timeline": {"segments": [{k: s[k] for k in ("segment_id", "title", "script", "vo_path", "duration", "screenshot", "card")} for s in segments]},
                                  "duration": dur})
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
        mp4 = ASSETS / f"{ep_id}_episode.mp4"
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
    except Exception:
        return None


def purge_disk(intermediate_age_s=1800, keep_episodes=4, low_disk_gb=2.0):
    """Free disk: drop spent per-segment intermediates older than N seconds + (if low) trim old episodes.
    Callable from the factory loop AND the manual /gc endpoint. Returns MB freed."""
    import shutil as _sh2
    freed = 0
    try:
        now = time.time()
        patterns = ("*_s*.wav", "*_demo.mp4", "*_bg*.mp4", "*_src.png", "*_snippet.py",
                    "*.tape", "*_raw.wav", "*_c[0-9].wav", "*_ducked.mp4", "*_list.txt")
        for pat in patterns:
            for f in ASSETS.glob(pat):
                try:
                    if now - f.stat().st_mtime > intermediate_age_s:
                        freed += f.stat().st_size; f.unlink()
                except Exception:
                    pass
        if _sh2.disk_usage(str(ASSETS)).free / 1e9 < low_disk_gb:
            epmp4s = sorted(ASSETS.glob("ep_*_episode.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in epmp4s[keep_episodes:]:
                try: freed += old.stat().st_size; old.unlink()
                except Exception: pass
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
            if (stale_seg
                    or (v.get("id") in archive and stage in ("scheduled", "live"))
                    or (kind == "episode" and stage == "revision")
                    or stale_inflight):
                await db.delete_video(v["id"]); n += 1
        if n:
            BUS.emit("system", "gc", f"auto-pruned {n} board cards (segments, revision orphans, stale cards)")
        # DISK GC: episode asset files (mp4/ui/snippet/etc) are never deleted when their DB card is
        # pruned — they accumulate unbounded (disk-exhaustion risk on a 24/7 system). Delete asset
        # files whose ep_id is no longer tracked in the DB AND whose file is older than 6h (safety window).
        try:
            live_ids = {v.get("id") for v in await db.list_videos() if v.get("kind") == "episode"}
            cutoff = now - 6 * 3600
            freed, dn = 0, 0
            for f in ASSETS.glob("ep_*"):
                m = re.match(r'(ep_[0-9a-f]+)_', f.name)
                if not m or m.group(1) in live_ids:
                    continue
                try:
                    if f.stat().st_mtime < cutoff:
                        freed += f.stat().st_size; f.unlink(); dn += 1
                except Exception:
                    pass
            if dn:
                BUS.emit("system", "gc", f"freed {freed//1024//1024}MB — pruned {dn} orphaned asset files")
        except Exception:
            pass
        # INTERMEDIATE PURGE: per-segment files (wavs, demos, b-rolls, screenshots, snippets, tapes) are
        # useless once the episode mp4 exists — they were the heavy disk hog (4.5G → ENOSPC). Delete any
        # older than 30min whose final episode mp4 is already on disk.
        try:
            import shutil as _sh2
            inter_freed = inter_n = 0
            patterns = ("*_s*.wav", "*_demo.mp4", "*_bg*.mp4", "*_src.png", "*_snippet.py",
                        "*.tape", "*_raw.wav", "*_c[0-9].wav", "*_ducked.mp4", "*_list.txt")
            for pat in patterns:
                for f in ASSETS.glob(pat):
                    try:
                        if now - f.stat().st_mtime > 1800:  # 30min
                            inter_freed += f.stat().st_size; f.unlink(); inter_n += 1
                    except Exception:
                        pass
            if inter_n:
                BUS.emit("system", "gc", f"freed {inter_freed//1024//1024}MB — pruned {inter_n} spent intermediates")
            # FREE-SPACE GUARD: if disk is critically low, also drop the oldest episode mp4s (keep 4)
            free_gb = _sh2.disk_usage(str(ASSETS)).free / 1e9
            if free_gb < 2.0:
                epmp4s = sorted(ASSETS.glob("ep_*_episode.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in epmp4s[4:]:
                    try: old.unlink()
                    except Exception: pass
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
            try:
                import services.abn_youtube as ytmod
                if pending_eps and ytmod.is_configured():
                    for e in pending_eps:
                        _set("publishing", "publisher", f"auto-publishing {e['id']} to YouTube", e["id"])
                        import urllib.request as _u
                        _u.urlopen(_u.Request("http://127.0.0.1:8000/api/agenticnews/episodes/" + e["id"] + "/publish",
                                              data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=300)
                    pending_eps = []  # cleared via publish
            except Exception as ex:
                BUS.emit("publisher", "error", f"auto-publish: {ex}", )
            # WORKSHOP MODE: do NOT park at "awaiting approval" — keep cooking modular segments/episodes
            # continuously. The review backlog is managed by the GC (prunes old ones); each new episode
            # is fresh material to workshop + harvest segments/shorts from. Only auto-publish if creds exist.
            if pending_eps:
                # cap the backlog so disk/board don't run away, but never STOP producing
                if len(pending_eps) > 12:
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
            result = await produce_one_episode(force_lore=lore_subject)
            # workshop cadence: short pause between episodes to keep the line hot; longer only when
            # there's genuinely no fresh material (None) so we don't spin on an empty feed.
            await asyncio.sleep(15 if result else 300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            BUS.emit("factory", "error", f"loop error: {e}")
            await asyncio.sleep(30)


_task = None


async def start_factory():
    global _task
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
