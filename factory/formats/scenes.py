"""
Meta-scene model + visual-director.

This is John's architecture made real: the script is deconstructed into SCENES by VO context,
each scene is tagged with a SceneRole (what the VO is doing), and the visual-director resolves
each scene to a shot type from the FORMAT'S catalog — instead of the legacy one-size fallback
chain (ui OR screenshot OR card) that screenshot-the-blog slop came from.

Flow:  segment script  ──tag_scenes──▶  list[Scene]  ──direct_visuals──▶  list[PlannedShot]

The scene-tagger here is RULE-BASED (deterministic, free, no LLM) — it reads the VO and classifies
each sentence-group by signals (numbers → NUMBER, "vs"/"compared to" → COMPARISON, "how it works"
→ MECHANISM, etc.). A later slice can upgrade this to an LLM scene-tagger, but the rule-based
version is enough to stop the slop and proves the catalog wiring.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from factory.formats.types import FormatSpec, SceneRole


@dataclass
class Scene:
    """One idea / one VO sentence-group — the unit the visual-director assigns a shot to."""
    index: int
    text: str                       # the VO sentences for this scene
    role: SceneRole
    approx_sec: float = 0.0         # filled once VO timing is known


@dataclass
class PlannedShot:
    """A shot the visual-director chose for a scene, ready for the asset stage to generate."""
    scene_index: int
    role: SceneRole
    shot_type: str                  # from the catalog vocabulary (screen_recording, diagram, ...)
    reason: str = ""                # why this shot was chosen (for review/debug)
    fallbacks: tuple[str, ...] = ()  # ordered alternatives if the primary can't be produced


# ── signals that classify a VO sentence-group into a SceneRole ──
_NUM_RE = re.compile(r'(\$\s?\d|\b\d+(\.\d+)?\s?(%|x|ms|gb|mb|k|m|b|billion|million|tokens|params|'
                     r'parameters|fps|seconds?|minutes?|hours?|requests?|users?)\b|\b\d{2,}\b)', re.I)

# A MEANINGFUL stat carries a magnitude/unit — these are the hero-number candidates. A bare number
# like a version ("4.8") or a list index is NOT the stat. Spelled-out magnitudes ("one million")
# count too. Ordered by visual punch.
_STAT_RE = re.compile(
    r'(\$\s?\d[\d,.]*\s?(?:billion|million|thousand|k|m|b)?'        # money
    r'|\b\d[\d,.]*\s?%'                                            # percent
    r'|\b\d[\d,.]*\s?x\b'                                          # multiplier (5x)
    r'|\b(?:one|two|three|four|five|ten|hundred)?\s?\d?[\d,.]*\s?(?:billion|million|thousand)\b'
    r'|\b\d[\d,.]*\s?(?:ms|gb|mb|tokens?|params?|parameters?|fps|requests?|users?)\b)', re.I)
# spelled-out leading magnitude ("one million token...")
_WORD_MAG_RE = re.compile(r'\b(one|two|three|four|five|ten)\s+(million|billion|thousand)\b', re.I)


def hero_number(text: str) -> str:
    """Pick the MEANINGFUL stat from a scene's VO for a number_card — NOT an incidental version
    number. Prefers a magnitude/unit'd figure (5x, 40%, one million tokens) over a bare number."""
    m = _WORD_MAG_RE.search(text)
    if m:
        return m.group(0)
    m = _STAT_RE.search(text)
    if m:
        return re.sub(r'\s+', ' ', m.group(0)).strip()
    # last resort: a bare number, but skip (a) version strings (X.Y after a product word) and
    # (b) bare YEARS (1900-2099) — a year alone is not a meaningful hero stat ('2023' rendered as a
    # number card with a sentence-fragment label — caught on a real card).
    for mm in re.finditer(r'\b\d[\d,.]*\b', text):
        tok = mm.group(0)
        if re.fullmatch(r'(19|20)\d\d', tok):          # bare year → not a stat
            continue
        before = text[max(0, mm.start() - 14):mm.start()].lower()
        if not re.search(r'(opus|gpt|claude|llama|gemini|v|version|model)\s*$', before):
            return tok
    return ""


def hero_number_label(text: str, stat: str) -> str:
    """A short clean label for under the hero number — the noun/phrase the stat MEASURES (e.g.
    'token context window', 'faster on Apple silicon'), never a truncated full sentence with an
    em-dash. Always derived from the words immediately around the stat."""
    text = text.replace("—", " ").replace("–", " ")     # never let an em-dash into the label
    idx = text.lower().find(stat.lower())
    if idx >= 0:
        # words AFTER the stat are usually what it measures — but CUT at a clause-boundary word so the
        # label is a tight noun phrase ('tokens'), not a run-on ('tokens huge for complex tasks').
        tail = re.split(r'[.,;]', text[idx + len(stat):])[0].strip(" ,.-")
        # cut at a clause-boundary word so the label stays a tight noun phrase ('developers'), not a
        # run-on into the next clause ('developers weekly to write code faster'). Includes 'to'+verb
        # infinitives and 'per/every/while/when' adverbials that start a new descriptive clause.
        _BOUND = (r'huge|big|massive|which|that|so|but|now|enough|making|meaning|compared|than|to|per|'
                  r'every|while|when|after|before|using|because|since|in order|across|on every|for each')
        cut = re.split(r'\s+(?:' + _BOUND + r')\b', tail, 1, flags=re.I)[0].strip()
        # if the boundary word was at the very START (cut → empty), the tail is itself a clause
        # ('per query across the board'); keep just its first 2 words as the noun phrase instead.
        if not cut:
            cut = " ".join(tail.split()[:2])
        words = cut.split()
        # drop a trailing dangling connective ('of', 'for', 'with', 'on' with nothing meaningful after)
        while words and words[-1].lower() in ("of", "for", "with", "on", "to", "in", "and", "the", "a"):
            words.pop()
        if 1 <= len(words) <= 5:
            return " ".join(words)
        if len(words) > 5:
            return " ".join(words[:3])    # tight noun phrase, never a run-on
        # tail too short — try the words BEFORE the stat (e.g. "cuts cost by 60%")
        head = re.split(r'[.,;]', text[:idx])[-1].strip(" ,.-")
        hwords = head.split()
        if 2 <= len(hwords) <= 6:
            return head
        if len(hwords) > 6:
            return " ".join(hwords[-5:])
    # last resort: a short generic label rather than a truncated negative sentence fragment
    return "by the numbers"
_VS_RE = re.compile(r'\b(vs\.?|versus|compared to|beats|outperforms|faster than|cheaper than|'
                    r'better than|instead of|replaces?)\b', re.I)
# capture the thing AFTER the comparison word — the real competitor to put on the vs card
_VS_TARGET_RE = re.compile(
    r'\b(?:vs\.?|versus|compared to|beats|outperforms|faster than|cheaper than|better than|'
    r'instead of|replaces?)\s+(?:the\s+|a\s+|an\s+)?'
    # capture up to ~4 words, stopping at the first preposition/conjunction/clause word
    r'((?:(?!\b(?:on|at|in|for|by|with|which|that|and|but|so|because|when|where|to|of|from)\b)'
    r'[A-Za-z][\w.\-]*\s*){1,4})', re.I)

# generic non-competitors that should NOT be put on a vs card (no real named rival)
_VS_GENERIC = {"old model", "old one", "the old model", "last one", "last version", "previous one",
               "one giant model", "giant model", "everything else", "the rest", "others", "it",
               "this", "that", "them", "one", "before",
               # generic tech nouns that aren't a NAMED competitor (e.g. 'cheaper than the API')
               "api", "apis", "sdk", "cli", "ui", "the api", "the sdk", "the cli", "the model",
               "the tool", "the framework", "the library", "the competition", "the alternative",
               "the rest of them", "the others", "the field", "the market", "the status quo"}


def comparison_target(text: str) -> str:
    """Pull the real competitor named after a comparison word ('faster than GPT-4' → 'GPT-4',
    'beats LangChain' → 'LangChain'). Returns '' for generic non-rivals ('the old model') so the
    caller renders something else instead of a meaningless 'X vs the alternative' card."""
    m = _VS_TARGET_RE.search(text or "")
    if not m:
        return ""
    t = re.sub(r'\s+', ' ', m.group(1)).strip(" .,").strip()
    tl = t.lower()
    if len(t) < 2 or tl in _VS_GENERIC:
        return ""
    # generic if it CONTAINS a non-rival phrase (over-capture like 'old model it is') or has no
    # capitalized/proper-noun token (real competitors are named: GPT-4, LangChain, Cursor...)
    if any(g in tl for g in ("old model", "giant model", "old one", "last one", "last version", "previous")):
        return ""
    if not re.search(r'[A-Z0-9]', t):           # no proper-noun/version token → not a named rival
        return ""
    # trim to the proper-noun core (first 1-3 tokens that look named)
    toks = t.split()
    keep = [w for w in toks if re.search(r'[A-Z0-9]', w) or w.lower() in ("the", "ai")][:3]
    return " ".join(keep) if keep else t
_MECH_RE = re.compile(r'\b(how it works|under the hood|works by|the way it|the trick is|'
                      r'mechanism|architecture|pipeline|it routes|it embeds|the model|algorithm|'
                      r'internally|the process)\b', re.I)
_TAKE_RE = re.compile(r"\b(honestly|the catch|the truth|here'?s the thing|in my view|the real|"
                      r"is it worth|overrated|underrated|the verdict|bottom line|what nobody)\b", re.I)


def _classify(text: str, is_first: bool, is_last: bool) -> SceneRole:
    """Rule-based scene-role classification from VO content. Deterministic, no LLM."""
    t = text.strip()
    if is_first:
        return SceneRole.HOOK
    if is_last:
        return SceneRole.TAKE          # land on the take/verdict
    if _VS_RE.search(t):
        return SceneRole.COMPARISON
    if _MECH_RE.search(t):
        return SceneRole.MECHANISM
    if _TAKE_RE.search(t):
        return SceneRole.TAKE
    if _NUM_RE.search(t):
        return SceneRole.NUMBER
    return SceneRole.CLAIM              # default: stating what happened


def _split_sentences(script: str) -> list[str]:
    """Split VO into sentences, grouped into scenes. More scenes = more visual variety = the
    anti-slop goal, so we keep most sentences as their own scene. Only group when sentences are
    SHORT (<10 words each), so a long segment doesn't fragment into a visual every 3 seconds but a
    3-sentence segment still yields ~3 scenes instead of collapsing to 1."""
    parts = re.split(r'(?<=[.!?])\s+', script.strip())
    parts = [p.strip() for p in parts if p.strip()]
    scenes, cur = [], []
    for s in parts:
        cur.append(s)
        words = sum(len(x.split()) for x in cur)
        # close a scene once it has enough substance for a shot (~12+ words) OR holds 2 sentences
        if words >= 12 or len(cur) >= 2:
            scenes.append(" ".join(cur)); cur = []
    if cur:
        if scenes and sum(len(x.split()) for x in cur) < 6:
            scenes[-1] = scenes[-1] + " " + " ".join(cur)   # fold a tiny trailing fragment back
        else:
            scenes.append(" ".join(cur))
    return scenes or [script.strip()]


def tag_scenes(segment_script: str, segment_index: int = 0,
               is_first_segment: bool = False, is_last_segment: bool = False) -> list[Scene]:
    """Deconstruct one segment's VO into tagged scenes."""
    chunks = _split_sentences(segment_script)
    scenes: list[Scene] = []
    n = len(chunks)
    for i, text in enumerate(chunks):
        is_first = is_first_segment and i == 0          # the true cold-open of the whole episode
        is_last = is_last_segment and i == n - 1
        scenes.append(Scene(index=i, text=text, role=_classify(text, is_first, is_last)))
    return scenes


def direct_visuals(scenes: list[Scene], spec: FormatSpec) -> list[PlannedShot]:
    """Visual-director: assign each scene a shot from the FORMAT'S catalog, with fallbacks.

    Honors the format's shot_preferences (ordered per SceneRole). Avoids repeating the same shot
    type back-to-back so the visual changes on the cut cadence (the anti-slop / variety rule).
    """
    planned: list[PlannedShot] = []
    last_type = None
    quote_count = 0
    # cap quote cards at ~40% of scenes so a claim/take-heavy script doesn't become 8 identical
    # centered-quote cards (caught on a real episode: 8/12 cards were quotes). Excess quotes rotate
    # to a designed title_card / brand_broll for visual variety.
    quote_cap = max(2, int(len(scenes) * 0.4)) if scenes else 0
    for sc in scenes:
        prefs = spec.shot_preferences.get(sc.role) or ("title_card", "brand_broll")
        # a number_card with no extractable hero stat would render an empty card — drop it from
        # the candidates for this scene so the director picks the next-best shot instead.
        if not hero_number(sc.text):
            prefs = tuple(p for p in prefs if p not in ("number_card", "data_card")) or ("title_card",)
        # pick the first preference that isn't an immediate repeat (variety = the anti-slop rule)
        primary = next((p for p in prefs if p != last_type), prefs[0])
        # QUOTE-CAP: if we've already hit the quote budget, rotate this quote to an alternative
        # designed shot so the episode doesn't read as a wall of identical quote cards.
        if primary == "quote_card" and quote_count >= quote_cap:
            primary = next((p for p in ("title_card", "brand_broll", "diagram") if p != last_type),
                           "title_card")
        if primary == "quote_card":
            quote_count += 1
        fallbacks = tuple(p for p in prefs if p != primary) + ("title_card", "brand_broll")
        planned.append(PlannedShot(
            scene_index=sc.index, role=sc.role, shot_type=primary,
            reason=f"role={sc.role.value} → {primary} (format {spec.label})",
            fallbacks=tuple(dict.fromkeys(fallbacks)),   # dedup, preserve order
        ))
        last_type = primary
    return planned


# How much of each segment is allowed to be a raw blog screenshot (the slop John flagged).
# A source_cutaway is capped to a brief "here's the source" beat, never the primary visual.
MAX_SOURCE_CUTAWAY_FRACTION = 0.15


def slop_check(planned: list[PlannedShot]) -> tuple[bool, str]:
    """Self-review gate for the visual plan. Fails if the plan is dominated by source-cutaways
    (the screenshot-a-blog slop) instead of designed/real shots."""
    if not planned:
        return False, "no shots planned"
    cutaways = sum(1 for p in planned if p.shot_type == "source_cutaway")
    frac = cutaways / len(planned)
    if frac > MAX_SOURCE_CUTAWAY_FRACTION:
        return False, f"slop: {frac:.0%} source-cutaways (max {MAX_SOURCE_CUTAWAY_FRACTION:.0%})"
    return True, "ok"
