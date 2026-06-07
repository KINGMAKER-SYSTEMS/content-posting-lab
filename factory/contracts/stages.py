"""
Per-stage typed I/O contracts.

Each pipeline stage is a pure function (typed_in) -> (typed_out). The expert that
executes the stage runs as a `pi -p --mode json` call whose raw JSON is validated
against the stage's *Out model here. A validation failure is an explicit typed error
(the runner transitions the episode to FAILED with the pydantic error as the reason) —
it is never swallowed. This file is the single contract both the runner and the pi
experts agree on; the expert system prompts are generated from these schemas so the
model is told exactly what JSON shape to return.

Stage map (mirrors factory.contracts.state.EpisodeState):
    scrape   : ()                         -> ScrapeOut   (raw sources)
    score    : ScrapeOut                  -> ScoreOut    (ranked + top pick)
    ideate   : ScoreOut                   -> IdeaOut     (video idea + format)
    research : IdeaOut                    -> ResearchOut (per-facet briefs)
    script   : ResearchOut                -> ScriptOut   (segments)
    assets   : ScriptOut                  -> AssetsOut   (voice + visual refs)
    assemble : AssetsOut                  -> AssembleOut (timeline)
    review   : AssembleOut                -> ReviewOut   (verdict + notes)
    refine   : (ScriptOut, ReviewOut)     -> ScriptOut   (revised — re-enters review)
    render   : AssembleOut                -> RenderOut    (final mp4 path on scratch)
    publish  : RenderOut                  -> PublishOut   (R2 + Drive locations)
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────── shared value types ───────────────────────────
class VideoFormat(str, Enum):
    ROUNDUP = "roundup"      # multi-story news brief
    DEEPDIVE = "deepdive"    # single tool, 3 facets
    LORE = "lore"            # single-subject origin story, 6 beats


class Source(BaseModel):
    title: str
    url: str = ""
    source_signal: str = ""          # where it came from (HN, arxiv, repo, etc.)
    raw_score: float = 0.0


class Facet(BaseModel):
    """One angle/beat of an episode — the unit research and scripting operate on."""
    label: str                       # e.g. "Under the hood" / "Origin" beat
    angle: str = ""                  # the specific thing this facet must cover
    brief: str = ""                  # research output for THIS facet (kept distinct)


class Segment(BaseModel):
    index: int
    title: str
    script: str
    facet_label: str = ""            # which facet this segment realizes (lore/deepdive)

    @field_validator("script")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("segment script must be non-empty")
        return v


# ─────────────────────────── stage outputs ───────────────────────────
class ScrapeOut(BaseModel):
    sources: list[Source] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _min_sources(cls, v: list[Source]) -> list[Source]:
        # the runner decides the floor per-format; here we just forbid an empty scrape
        # masquerading as success (legacy code returned None and limped on).
        return v


class ScoreOut(BaseModel):
    ranked: list[Source]
    top: Source                       # the selected lead item
    format: VideoFormat               # chosen format for this episode


class IdeaOut(BaseModel):
    title: str                        # the working video title (SEO refines later)
    thesis: str                       # the one-line angle that connects everything
    format: VideoFormat
    facets: list[Facet]               # 1 (roundup lead) / 3 (deepdive) / 6 (lore)

    @field_validator("facets")
    @classmethod
    def _has_facets(cls, v: list[Facet]) -> list[Facet]:
        if not v:
            raise ValueError("an idea must define at least one facet")
        return v


class ResearchOut(BaseModel):
    facets: list[Facet]               # same facets, now each with a DISTINCT brief
    sources_used: list[str] = Field(default_factory=list)

    @field_validator("facets")
    @classmethod
    def _briefs_present(cls, v: list[Facet]) -> list[Facet]:
        if any(not f.brief.strip() for f in v):
            raise ValueError("every facet must carry a non-empty research brief")
        return v


class ScriptOut(BaseModel):
    cold_open: str = ""               # narrative hook (roundup/deepdive); lore beat-0 is its own open
    segments: list[Segment]

    @field_validator("segments")
    @classmethod
    def _has_segments(cls, v: list[Segment]) -> list[Segment]:
        if not v:
            raise ValueError("script must have at least one segment")
        return v


class VoiceClip(BaseModel):
    segment_index: int
    audio_path: str                   # path on scratch store
    duration_s: float


class VisualRef(BaseModel):
    segment_index: int
    kind: str                         # broll | demo | diagram | title-card
    ref: str                          # path or generation spec


class AssetsOut(BaseModel):
    voice: list[VoiceClip]
    visuals: list[VisualRef]


class AssembleOut(BaseModel):
    timeline_path: str                # the timeline.json the renderer consumes
    duration_s: float
    segment_count: int


class ReviewVerdict(str, Enum):
    PASS = "pass"                     # → render
    REFINE = "refine"                # → refinement, then re-review
    REJECT = "reject"                # → terminal reject


class ReviewNote(BaseModel):
    segment_index: Optional[int] = None
    issue: str
    fix: str = ""


class ReviewOut(BaseModel):
    verdict: ReviewVerdict
    notes: list[ReviewNote] = Field(default_factory=list)
    distinctness_ok: bool = True      # facets don't repeat each other
    depth_ok: bool = True             # real names/numbers, not generic filler
    slop_ok: bool = True              # no blank/CAPTCHA/placeholder visuals

    @field_validator("notes")
    @classmethod
    def _refine_needs_notes(cls, v, info):
        verdict = info.data.get("verdict")
        if verdict in (ReviewVerdict.REFINE, ReviewVerdict.REJECT) and not v:
            raise ValueError(f"verdict={verdict} requires at least one note explaining why")
        return v


class RenderOut(BaseModel):
    mp4_path: str                     # final on scratch store
    duration_s: float
    size_bytes: int


class PublishOut(BaseModel):
    r2_key: str                       # canonical location
    r2_url: str = ""
    drive_path: str = ""              # team mirror on the SSD
    published: bool = True


# stage name → output model, so the runner can validate any stage generically
STAGE_OUTPUT: dict[str, type[BaseModel]] = {
    "scrape": ScrapeOut,
    "score": ScoreOut,
    "ideate": IdeaOut,
    "research": ResearchOut,
    "script": ScriptOut,
    "assets": AssetsOut,
    "assemble": AssembleOut,
    "review": ReviewOut,
    "refine": ScriptOut,
    "render": RenderOut,
    "publish": PublishOut,
}
