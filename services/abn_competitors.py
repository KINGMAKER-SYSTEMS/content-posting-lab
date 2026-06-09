"""
AgenticBuilderNews — COMPETITOR INTELLIGENCE.

The research arm that was missing: scrape the AI-news channels that actually retain viewers, pull their
top recent video titles (and on demand, transcripts), and feed the distilled PATTERNS to the script /
title / hook teams — so the writers MODEL proven winners instead of guessing from training data.

Data lives in competitor_intel.json on the assets volume (refreshed periodically). The experts read
playbook()/topic_signals() into their prompts.
"""
from __future__ import annotations
import os
import json
import subprocess
import time
from pathlib import Path

_VOL = os.getenv("ABN_ASSETS_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
_BASE = Path(_VOL) if _VOL and Path(_VOL).exists() else Path(__file__).resolve().parent.parent / "agenticnews_assets"
_BASE.mkdir(parents=True, exist_ok=True)
INTEL_FILE = _BASE / "competitor_intel.json"

# Faceless AI-news/explainer channels with proven retention — the ones worth copying.
COMPETITORS = ["TheAIGRID", "matthew_berman", "WesRoth", "mreflow",
               "aiexplained-official", "AISearch", "TheAIAdvantage"]
_CACHE_TTL = 12 * 3600   # re-scrape at most twice a day


def _scrape_channel(handle: str, n: int = 8) -> list[dict]:
    try:
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-end", str(n), "--print", "%(title)s",
             f"https://www.youtube.com/@{handle}/videos"],
            capture_output=True, text=True, timeout=45).stdout
        return [{"channel": handle, "title": t.strip()} for t in out.splitlines() if t.strip()]
    except Exception:
        return []


# Distilled from a real transcript + title teardown (AI Explained, Matthew Berman, WesRoth, Matt Wolfe).
# These are the rules the experts should follow — derived from what the winners actually do.
FINDINGS = {
    "hook_rule": ("First spoken sentence names a SPECIFIC claim/number with ZERO throat-clearing. Real "
                  "winners: 'I've read the 244-page report Anthropic put out', 'A new coding meta just "
                  "dropped', 'Microsoft JUST BROKE OpenAI'. Never 'AI agents are changing everything'."),
    "title_formula": ("Caps on the payoff verb + a named tool/company/number + a curiosity gap. Proven "
                      "shapes: 'AI News: [Co] Finally Reveals…', '[Model]: N Things You May've Missed', "
                      "'[Co] WARNS/JUST BROKE [Co]', '[Tool] Explained'."),
    "density": "Winners pack DENSE specific narration (AI Explained ≈ 12k words / 22 min). Thin VO loses.",
    "format": "Information-dense, fast, real specifics — NOT slow narration over a static card slideshow.",
}


def refresh(force: bool = False) -> dict:
    if not force and INTEL_FILE.exists():
        try:
            blob = json.loads(INTEL_FILE.read_text())
            if blob.get("ts") and (time.time() - blob["ts"]) < _CACHE_TTL:
                return blob
        except Exception:
            pass
    videos = []
    for h in COMPETITORS:
        videos.extend(_scrape_channel(h))
    blob = {"ts": int(time.time()), "channels": COMPETITORS, "videos": videos, "findings": FINDINGS}
    try:
        tmp = INTEL_FILE.with_suffix(".json.tmp"); tmp.write_text(json.dumps(blob, indent=2)); tmp.replace(INTEL_FILE)
    except Exception:
        pass
    return blob


def _load() -> dict:
    try:
        return json.loads(INTEL_FILE.read_text())
    except Exception:
        return refresh()


def title_playbook(limit: int = 16) -> str:
    """For the TITLER/THUMBNAILER: the real top competitor titles to model + the title rule."""
    b = _load()
    vids = (b.get("videos") or [])[:limit]
    if not vids:
        return ""
    lines = "\n".join(f"  [{v['channel']}] {v['title']}" for v in vids)
    return ("REAL COMPETITOR TITLES THAT PERFORM (model these — don't invent a weaker formula):\n" + lines +
            "\n\nTITLE RULE: " + b.get("findings", FINDINGS)["title_formula"])


def hook_playbook() -> str:
    """For the NARRATOR: the hook rule derived from real competitor openings."""
    return "COMPETITOR HOOK RULE (from real top videos): " + _load().get("findings", FINDINGS)["hook_rule"]


def topic_signals(limit: int = 24) -> list[str]:
    """For the SCOUT: what the winning channels are covering NOW (demand signal)."""
    return [v["title"] for v in (_load().get("videos") or [])[:limit]]


if __name__ == "__main__":
    b = refresh(force=True)
    print(f"{len(b['videos'])} titles across {len(b['channels'])} channels")
    print(title_playbook())
