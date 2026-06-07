"""
Typed content-factory state machine — the spine.

Every episode is a row that moves through a strict, enumerated sequence of states.
A stage NEVER fails silently: a failed transition lands the episode in FAILED with a
typed (stage, reason) record. This is the explicit replacement for the 65 `except
Exception: pass` swallows in the legacy abn_factory.py, where any stage could fail and
the pipeline would limp forward in an undefined state.

The flow follows John's named pipeline stages (research → scripting → assets →
compilation → review → refinement → produce), and the only legal transitions are:

    SCRAPED → SCORED → IDEATED → RESEARCHED → SCRIPTED → ASSETS → ASSEMBLED
            → REVIEWED → RENDERED → PUBLISHED
                  REVIEWED ⇄ REFINED          (the review/refine loop — refine re-enters review)
                              ↘ FAILED(stage, reason)   (from any state)
                              ↘ REJECTED                (SCORED/REVIEWED only — quality gate)

A state machine, not vibes: `EPISODE_TRANSITIONS` is the single source of truth for
what may follow what. `assert_transition` is the guard every stage calls before it
commits — an illegal jump raises, it does not get swallowed.
"""
from __future__ import annotations

from enum import Enum


class EpisodeState(str, Enum):
    # happy path — these ARE John's pipeline stages, made explicit
    SCRAPED = "scraped"        # raw sources pulled, not yet judged
    SCORED = "scored"          # sources ranked; top item selected
    IDEATED = "ideated"        # top item turned into a concrete video idea + format
    RESEARCHED = "researched"  # research expert deep-dug the idea into a brief (per-facet)
    SCRIPTED = "scripted"      # scripting expert wrote the full script from the brief
    ASSETS = "assets"          # asset expert produced voice + visuals (the "assets" stage)
    ASSEMBLED = "assembled"    # compilation: timeline built (visuals + audio + captions)
    REVIEWED = "reviewed"      # review gate ran (distinctness, depth, no-slop)
    REFINED = "refined"        # refinement expert addressed review notes → re-enters review
    RENDERED = "rendered"      # final mp4 produced on SSD scratch
    PUBLISHED = "published"    # final synced to R2 canon + Drive(SSD) mirror

    # terminal off-ramps — both EXPLICIT, never a swallowed exception
    REJECTED = "rejected"      # quality gate said no (thin sources / unfixable review)
    FAILED = "failed"          # a stage errored; carries (stage, reason)


# The ONLY legal transitions. Anything not in this map is an illegal jump and raises.
EPISODE_TRANSITIONS: dict[EpisodeState, set[EpisodeState]] = {
    EpisodeState.SCRAPED:    {EpisodeState.SCORED, EpisodeState.FAILED},
    EpisodeState.SCORED:     {EpisodeState.IDEATED, EpisodeState.REJECTED, EpisodeState.FAILED},
    EpisodeState.IDEATED:    {EpisodeState.RESEARCHED, EpisodeState.FAILED},
    EpisodeState.RESEARCHED: {EpisodeState.SCRIPTED, EpisodeState.FAILED},
    EpisodeState.SCRIPTED:   {EpisodeState.ASSETS, EpisodeState.FAILED},
    EpisodeState.ASSETS:     {EpisodeState.ASSEMBLED, EpisodeState.FAILED},
    EpisodeState.ASSEMBLED:  {EpisodeState.REVIEWED, EpisodeState.FAILED},
    # the review/refine loop: review either passes (→render), sends back for refinement,
    # rejects outright, or errors. Refinement always re-enters review (never skips the gate).
    EpisodeState.REVIEWED:   {EpisodeState.RENDERED, EpisodeState.REFINED,
                              EpisodeState.REJECTED, EpisodeState.FAILED},
    EpisodeState.REFINED:    {EpisodeState.REVIEWED, EpisodeState.FAILED},
    EpisodeState.RENDERED:   {EpisodeState.PUBLISHED, EpisodeState.FAILED},
    EpisodeState.PUBLISHED:  set(),   # terminal
    EpisodeState.REJECTED:   set(),   # terminal
    EpisodeState.FAILED:     set(),   # terminal (a retry creates a NEW episode, by design)
}

# Stages map 1:1 onto the transition that ENTERS each state — used for expert routing + logging.
ENTERING_STAGE: dict[EpisodeState, str] = {
    EpisodeState.SCORED:     "score",
    EpisodeState.IDEATED:    "ideate",
    EpisodeState.RESEARCHED: "research",
    EpisodeState.SCRIPTED:   "script",
    EpisodeState.ASSETS:     "assets",
    EpisodeState.ASSEMBLED:  "assemble",
    EpisodeState.REVIEWED:   "review",
    EpisodeState.REFINED:    "refine",
    EpisodeState.RENDERED:   "render",
    EpisodeState.PUBLISHED:  "publish",
}

# Guard against infinite review↔refine cycling — the runner enforces this cap and
# REJECTs an episode that can't pass review within N refinement rounds.
MAX_REFINE_ROUNDS = 3


class IllegalTransition(RuntimeError):
    """Raised when a stage attempts a transition not permitted by EPISODE_TRANSITIONS.

    This is intentionally LOUD — the whole point of the rewrite is that an undefined
    state move is a crash we can see, not a silently-swallowed pass.
    """


def can_transition(src: EpisodeState, dst: EpisodeState) -> bool:
    return dst in EPISODE_TRANSITIONS.get(src, set())


def assert_transition(src: EpisodeState, dst: EpisodeState) -> None:
    if not can_transition(src, dst):
        legal = ", ".join(sorted(s.value for s in EPISODE_TRANSITIONS.get(src, set()))) or "(none — terminal)"
        raise IllegalTransition(
            f"illegal transition {src.value} → {dst.value}. legal from {src.value}: {legal}"
        )


def is_terminal(state: EpisodeState) -> bool:
    return not EPISODE_TRANSITIONS.get(state)
