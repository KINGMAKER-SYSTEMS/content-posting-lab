"""
Format-type specifications — the retention spine, as enforced config.

Each VideoFormat maps to a FormatSpec that owns EVERYTHING that should differ per format:
length floor, mid-roll ad beats, cut cadence, caption strategy, scene-role → shot-catalog
preferences, hook rules, and research emphasis. The legacy monolith made every video identical
(one caption template, one shot fallback chain); these specs are how a Pulse, a Deepdive, a Lore,
and a Short stop looking the same.

Every number here is grounded in sourced 2025-2026 YouTube retention/RPM research
(see factory/FORMAT_ARCHITECTURE.md § Sources) — NOT training-data guesses:
  - mid-roll requires 8:00 hard minimum; money formats target 10-14min for 2 mid-rolls (RPM ~2-3x)
  - hook window tightened to ~7s; ~70% retention at 0:30 triggers algorithmic promotion
  - cut cadence is audience-dependent: a 25+ builder audience holds 20-40s, cuts on topic shift
  - phrase-level captions (not word-by-word bounce); one font channel-wide; don't caption everything
  - place mid-rolls after a resolution beat at ~45% and ~75%, never in a climax
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from factory.contracts.stages import VideoFormat


class SceneRole(str, Enum):
    """What the VO is doing in a scene — drives which shot type the visual-director picks."""
    HOOK = "hook"              # the 0-7s grab
    CLAIM = "claim"            # stating what happened / what it is
    MECHANISM = "mechanism"    # how it works
    NUMBER = "number"          # a stat / benchmark / price
    COMPARISON = "comparison"  # X vs Y
    TAKE = "take"              # the opinion / verdict
    TRANSITION = "transition"  # breath beat between segments (also where ad-beats live)


@dataclass(frozen=True)
class CaptionStrategy:
    """How captions behave for a format. Replaces the legacy single hardcoded lower-third.

    Phrase-level not word-by-word (word bounce reads frantic); one font channel-wide; emphasize
    meaning-bearing phrases; don't caption everything on long-form."""
    mode: str                       # "phrase" | "sparse" | "kinetic"  (never "word-by-word")
    caption_everything: bool        # False on long-form — let VO carry, emphasize key phrases only
    max_chars_per_line: int = 42    # ~5-7 words; readability bar from research
    max_lines: int = 2
    lower_third_role: str = "story_label"   # "story_label" | "term_callout" | "name_super" | "none"
    lower_third_hold_sec: float = 3.5
    lead_audio_sec: float = 0.2     # caption appears slightly BEFORE the audio


@dataclass(frozen=True)
class AdBeat:
    """A deliberate resolution/"breath" beat where a mid-roll should land (after a satisfying
    moment, never in a climax). Expressed as a fraction through the runtime."""
    at_fraction: float              # 0.45 = 45% through
    note: str = ""                  # what resolves here, for the scriptwriter to honor


@dataclass(frozen=True)
class FormatSpec:
    format: VideoFormat
    label: str                      # human name ("Pulse", "Deepdive", "Lore", "Short")
    model_reference: str            # the real channel this format's structure is modeled on

    # ── length / monetization (ENFORCED gates) ──
    min_sec: int                    # HARD floor — render shorter than this is rejected
    target_min_sec: int             # bottom of the target band
    target_max_sec: int             # top of the target band
    ad_beats: tuple[AdBeat, ...]    # where mid-rolls should land (empty = no ads, e.g. SHORT)

    # ── structure ──
    segment_count: tuple[int, int]  # (min, max) number of segments/beats/stories
    fixed_beats: tuple[str, ...] = ()   # for fixed-arc formats (lore's 6 beats); () = variable

    # ── retention mechanics ──
    hook_window_sec: float = 7.0    # establish value within this many seconds
    cut_hold_sec: tuple[float, float] = (20.0, 40.0)   # (min, max) shot hold; cut on topic shift
    reengage_every_sec: float = 150.0   # restate stakes / narrative loop cadence (~2.5min)

    # ── captions ──
    captions: CaptionStrategy = field(default_factory=lambda: CaptionStrategy("phrase", False))

    # ── scene-role → preferred shot types (visual-director picks from the catalog in order) ──
    shot_preferences: dict[SceneRole, tuple[str, ...]] = field(default_factory=dict)

    # ── research emphasis (what the research stage should optimize the brief for) ──
    research_emphasis: str = ""

    def enforces_ads(self) -> bool:
        return bool(self.ad_beats)


# Shared shot-catalog vocabulary (the visual-director resolves these to real generators):
#   screen_recording | diagram | code_walkthrough | data_card | number_card | quote_card |
#   vs_card | brand_broll | title_card | source_cutaway (≤3s blog screenshot, DEMOTED)

_PULSE_SHOTS = {
    SceneRole.HOOK:       ("number_card", "vs_card", "title_card"),
    SceneRole.CLAIM:      ("screen_recording", "source_cutaway", "title_card"),
    SceneRole.MECHANISM:  ("diagram", "screen_recording", "code_walkthrough"),
    SceneRole.NUMBER:     ("number_card", "data_card"),
    SceneRole.COMPARISON: ("vs_card", "screen_recording"),
    SceneRole.TAKE:       ("quote_card", "brand_broll"),
    SceneRole.TRANSITION: ("brand_broll", "title_card"),
}
_DEEPDIVE_SHOTS = {
    SceneRole.HOOK:       ("number_card", "screen_recording", "title_card"),
    SceneRole.CLAIM:      ("screen_recording", "source_cutaway"),
    SceneRole.MECHANISM:  ("diagram", "code_walkthrough", "screen_recording"),
    SceneRole.NUMBER:     ("data_card", "number_card"),
    SceneRole.COMPARISON: ("vs_card", "screen_recording"),
    SceneRole.TAKE:       ("quote_card", "brand_broll"),
    SceneRole.TRANSITION: ("brand_broll",),
}
_LORE_SHOTS = {
    SceneRole.HOOK:       ("quote_card", "brand_broll", "title_card"),
    SceneRole.CLAIM:      ("source_cutaway", "title_card", "brand_broll"),
    SceneRole.MECHANISM:  ("diagram", "brand_broll"),
    SceneRole.NUMBER:     ("number_card", "data_card"),
    SceneRole.COMPARISON: ("vs_card",),
    SceneRole.TAKE:       ("quote_card", "brand_broll"),
    SceneRole.TRANSITION: ("brand_broll", "title_card"),
}
_SHORT_SHOTS = {
    SceneRole.HOOK:       ("number_card", "title_card"),
    SceneRole.CLAIM:      ("screen_recording", "code_walkthrough"),
    SceneRole.MECHANISM:  ("code_walkthrough", "diagram"),
    SceneRole.NUMBER:     ("number_card",),
    SceneRole.TAKE:       ("quote_card",),
    SceneRole.TRANSITION: ("brand_broll",),
}


FORMATS: dict[VideoFormat, FormatSpec] = {
    # PULSE — the daily news driver. Segmented stories, each a re-engagement beat.
    VideoFormat.ROUNDUP: FormatSpec(
        format=VideoFormat.ROUNDUP, label="Pulse", model_reference="'This week in AI' segmented news",
        min_sec=600, target_min_sec=600, target_max_sec=720,           # 10-12 min, 2 mid-rolls
        ad_beats=(AdBeat(0.45, "after a story resolves"), AdBeat(0.75, "before the last segment")),
        segment_count=(5, 7),
        hook_window_sec=7.0, cut_hold_sec=(15.0, 25.0), reengage_every_sec=150.0,  # contrast pattern
        captions=CaptionStrategy("phrase", caption_everything=False, lower_third_role="story_label"),
        shot_preferences=_PULSE_SHOTS,
        research_emphasis="broadly-searched angle; what a broad AI-curious audience actually clicks",
    ),
    # DEEPDIVE — single tool, result-first.
    VideoFormat.DEEPDIVE: FormatSpec(
        format=VideoFormat.DEEPDIVE, label="Deepdive", model_reference="Fireship scaled to long-form",
        min_sec=600, target_min_sec=600, target_max_sec=720,           # 10-12 min (was 8-10; clear 10 floor)
        ad_beats=(AdBeat(0.50, "after the 'what it is' resolves"),),
        segment_count=(4, 6),
        hook_window_sec=7.0, cut_hold_sec=(20.0, 35.0), reengage_every_sec=150.0,
        captions=CaptionStrategy("phrase", caption_everything=False, lower_third_role="term_callout"),
        shot_preferences=_DEEPDIVE_SHOTS,
        research_emphasis="the real mechanism + the honest verdict vs the named incumbent",
    ),
    # LORE — documentary origin story. Highest RPM.
    VideoFormat.LORE: FormatSpec(
        format=VideoFormat.LORE, label="Lore", model_reference="ColdFusion documentary",
        min_sec=720, target_min_sec=720, target_max_sec=1080,          # 12-18 min, 2-3 mid-rolls
        ad_beats=(AdBeat(0.40, "after the bet pays/fails"), AdBeat(0.70, "after the conflict resolves")),
        segment_count=(6, 6),
        fixed_beats=("cold-open", "origin", "the-bet", "the-conflict", "where-it-stands", "open-question"),
        hook_window_sec=10.0, cut_hold_sec=(25.0, 45.0), reengage_every_sec=180.0,  # cinematic holds
        captions=CaptionStrategy("sparse", caption_everything=False, lower_third_role="name_super",
                                 lower_third_hold_sec=3.0),
        shot_preferences=_LORE_SHOTS,
        research_emphasis="verifiable real names / dates / events; no fabricated drama",
    ),
    # SHORT — top-of-funnel, no ads.
    VideoFormat.SHORT: FormatSpec(
        format=VideoFormat.SHORT, label="Short", model_reference="Fireship 100-second",
        min_sec=0, target_min_sec=30, target_max_sec=100,              # ≤100s, no mid-roll
        ad_beats=(),
        segment_count=(1, 1),
        hook_window_sec=3.0, cut_hold_sec=(4.0, 7.0), reengage_every_sec=999.0,   # 10-15 cuts/min
        captions=CaptionStrategy("kinetic", caption_everything=True, lower_third_role="none"),
        shot_preferences=_SHORT_SHOTS,
        research_emphasis="one punchy concept; the single most clickable fact",
    ),
}


def get_format(fmt: VideoFormat) -> FormatSpec:
    """Resolve a VideoFormat to its spec. Raises on unknown format (loud, not silent)."""
    spec = FORMATS.get(fmt)
    if spec is None:
        raise KeyError(f"no FormatSpec registered for {fmt!r}; known: {[f.value for f in FORMATS]}")
    return spec
