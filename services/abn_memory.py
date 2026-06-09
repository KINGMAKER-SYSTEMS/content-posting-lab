"""
AgenticBuilderNews — SELF-REFINEMENT MEMORY (the flywheel).

Records what the channel has made and what got approved, so the system learns:
- which story TOPICS/keywords recur in approved episodes (bias scoring toward them)
- which HOOK/thesis patterns worked (seed the narrator/scriptwriter)
- avoid repeating the same stories across episodes (freshness ledger)

Persisted as JSON on the volume so it survives restarts. Read each cycle.
"""
from __future__ import annotations
import os
import json
import time
from pathlib import Path

_VOL = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
_BASE = Path(_VOL) if _VOL and Path(_VOL).exists() else Path(__file__).resolve().parent.parent
MEM_PATH = _BASE / "abn_memory.json"


def _load() -> dict:
    if MEM_PATH.exists():
        try:
            return json.loads(MEM_PATH.read_text())
        except Exception:
            pass
    return {"episodes": [], "topic_counts": {}, "seen_titles": [], "approved_theses": []}


def _save(m: dict) -> None:
    tmp = MEM_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    tmp.replace(MEM_PATH)


def record_episode(ep_id: str, titles: list[str], thesis: str = "", approved: bool = False, rendered: bool = False) -> None:
    """Log an episode's stories + thesis. seen_titles only grows when rendered=True (a real produced episode)."""
    m = _load()
    m["episodes"].append({"ep_id": ep_id, "titles": titles, "thesis": thesis,
                          "approved": approved, "rendered": rendered, "ts": time.time()})
    # topic keyword counts (what we keep making)
    import re
    for t in titles:
        for w in re.findall(r'[A-Za-z][A-Za-z0-9.-]{2,}', t.lower()):
            if w not in ("the", "and", "for", "with", "your", "now", "new", "are", "this", "that"):
                m["topic_counts"][w] = m["topic_counts"].get(w, 0) + 1
    # only mark titles "seen" once an episode RENDERS (rendered=True) — never on scrape/abort,
    # so the freshness filter can't starve the pipeline by eating un-produced candidates.
    if rendered:
        existing = {s["t"] for s in m.get("seen_titles", []) if isinstance(s, dict)}
        for t in titles:
            key = _norm(t)
            if key and key not in existing:
                m.setdefault("seen_titles", []).append({"t": key, "ts": time.time()})
        m["seen_titles"] = m["seen_titles"][-150:]
    if approved and thesis:
        m["approved_theses"] = (m["approved_theses"] + [thesis])[-30:]
    m["episodes"] = m["episodes"][-200:]
    _save(m)


def _norm(t: str) -> str:
    import re
    return re.sub(r'[^a-z0-9 ]', '', (t or "").lower()).strip()


FRESHNESS_WINDOW = 36 * 3600  # a story can be revisited after ~1.5 days (matches a daily-news cadence)


def is_recently_used(title: str, window: float = FRESHNESS_WINDOW) -> bool:
    """Freshness: did we PRODUCE a video on this exact story within `window` seconds? Exact-ish, time-bounded."""
    m = _load()
    key = _norm(title)
    if not key:
        return False
    now = time.time()
    for s in m.get("seen_titles", []):
        if not isinstance(s, dict):
            continue
        if now - s.get("ts", 0) > window:
            continue
        # require strong overlap, not loose substring — same story, not just shared words
        if s["t"] == key or (len(key) > 20 and (key in s["t"] or s["t"] in key)):
            return True
    return False


def overexposed_topics(threshold: int = 6) -> set[str]:
    """Topics we've made too many videos about — deprioritize to keep variety."""
    m = _load()
    return {k for k, v in m["topic_counts"].items() if v >= threshold}


def winning_theses(n: int = 5) -> list[str]:
    """Recent approved theses — seed the narrator with proven angles."""
    m = _load()
    return m["approved_theses"][-n:]


def record_rejection(titles: list[str]) -> None:
    """A rejected episode = 'not good stories'. Log the story-keyword stems so the scorer can
    deprioritize similar topics going forward (learn what to AVOID, not just what worked)."""
    m = _load()
    import re
    rej = m.setdefault("rejected_keywords", {})
    for t in titles:
        for w in re.findall(r'[A-Za-z][A-Za-z0-9.-]{3,}', (t or "").lower()):
            if w not in ("this", "that", "with", "your", "from", "what", "agent", "agents", "tool", "code"):
                rej[w] = rej.get(w, 0) + 1
    # bound it
    if len(rej) > 200:
        m["rejected_keywords"] = dict(sorted(rej.items(), key=lambda x: -x[1])[:200])
    _save(m)


def rejection_penalty(title: str) -> int:
    """How many rejected episodes shared keywords with this story? Higher = more likely to be skippable."""
    m = _load()
    rej = m.get("rejected_keywords", {})
    if not rej:
        return 0
    import re
    words = set(re.findall(r'[A-Za-z][A-Za-z0-9.-]{3,}', (title or "").lower()))
    return sum(rej.get(w, 0) for w in words)


def stats() -> dict:
    m = _load()
    return {"episodes": len(m["episodes"]),
            "approved": sum(1 for e in m["episodes"] if e.get("approved")),
            "top_topics": sorted(m["topic_counts"].items(), key=lambda x: -x[1])[:8],
            "seen_titles": len(m["seen_titles"])}
