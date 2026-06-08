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
_VS_RE = re.compile(r'\b(vs\.?|versus|compared to|beats|outperforms|faster than|cheaper than|'
                    r'better than|instead of|replaces?)\b', re.I)
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
    """Split VO into sentences, then group into ~2-sentence scenes (≈20-30s of speech each)."""
    parts = re.split(r'(?<=[.!?])\s+', script.strip())
    parts = [p.strip() for p in parts if p.strip()]
    scenes, cur = [], []
    for s in parts:
        cur.append(s)
        # group ~2 sentences per scene (a scene ≈ one idea, ~20-40s)
        if len(cur) >= 2:
            scenes.append(" ".join(cur)); cur = []
    if cur:
        if scenes:
            scenes[-1] = scenes[-1] + " " + " ".join(cur)   # fold a lone trailing sentence back
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
    for sc in scenes:
        prefs = spec.shot_preferences.get(sc.role) or ("title_card", "brand_broll")
        # pick the first preference that isn't an immediate repeat (variety = the anti-slop rule)
        primary = next((p for p in prefs if p != last_type), prefs[0])
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
