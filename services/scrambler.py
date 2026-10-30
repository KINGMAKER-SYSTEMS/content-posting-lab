"""The Scrambler (Eric 2026-10-28/29): per-cut variety draws for source-video pages.

Spec: MASTER-ITERATION-STRATEGY-1029.md sections 3, 7a and 9 (canonical) as
mapped by CONTENT-LAB-CANONICAL.md section 3, updated by USER-UPDATE.txt
(2026-10-29: add faster speeds 1.1/1.2/1.3 with the same final 5-9 s and
variety strategy; reserve the FULL origin span before window selection).
Every cut drawn here:

1. draws a branch - slow 0.3x-0.8x on the 0.05 grid, or fast 1.1x/1.2x/1.3x - as the EFFECTIVE speed the media is rendered at (default
   share 0 % fast / 100 % slow, configurable globally and per page; the fast
   branch is opt-in via ``SCRAMBLER_FAST_SHARE``). Only (speed, length)
   pairs whose origin window the live Worker admits (5-10 s, see
   ``worker_admits_window``) are drawable, so on Worker main the slow branch
   draws 0.6x-0.8x;
2. draws a final output length of 5-9 s on the 0.5 s grid, never the same
   length (to the 0.5 s grid) as the page's previous cut;
3. draws exactly one treatment - plain, split-swap (back half to the front,
   LOOP9-style), slow camera move, or a darker-only grade jitter (offered only
   to page looks at or under the 0.8 brightness ceiling; the applied
   darkening is bounded by the Worker's existing 0.04 lower admission band
   and stays a private manifest value - the receipt keeps the recipe look) -
   never the same treatment as the previous cut. Reverse is the
   LOWEST-frequency treatment: a deterministic per-slot 1-in-15 gate selects
   reverse DIRECTLY, and a CUMULATIVE cap inside the reservation lock proves
   the page's committed reverses never exceed floor(cuts / 15) at every
   prefix - a probability alone cannot prove that - plus the one-reverse-per-
   ET-day (America/New_York) cap. Mirror is drawn as its own on/off axis
   (share 1/3), so it can ride with any treatment;
4. draws a sub-second start offset inside the planned window (0.1 s steps).

The full combination (speed bucket, length, treatment, mirror) never repeats
on a page within its rotation history, and the canonical offset combination
(speed bucket, length, treatment, mirror, offset) never repeats either. These
rules are HARD: there is no relaxation path. When no legal draw remains the
draw raises ``ScramblerExhausted`` by name and job creation refuses - it never
silently reuses a combo, repeats the previous length, or unlocks reused
footage.

Rule #1 (originality) is untouched: the Scrambler is for VARIETY ONLY. The
draw happens BEFORE the window is planned and fixes the exact source span
``offset + round(length x effective_speed)`` (a 7.5 s output at 1.3x reserves
9.75 s of source BEFORE the window is chosen - never a slow-sized window sped
up afterwards), so the reservation is truthful to the render and to the
origin accounting: ``origin_seconds = output_seconds x effective_speed``,
counted once, never underreported.

Worker wire safety: the Scrambler writes ONLY Lab-side manifest records
(``clipTransforms`` like Layer B, plus a private ``scrambler`` record). The
treatment receipt (``visualTreatment``) keeps the recipe's exact clipSpeed /
clipCrop / filters - byte-identical to the Scrambler-OFF build - because the
Worker admits a receipt only when it equals the recipe exactly. The effective
speed lives in the manifest-only block, and scrambler fields never enter any
library-id/version/catalogVersion hash.

Switches (read per call, so a rollback needs no restart): ``SCRAMBLER_ENABLED``
(global kill switch, default 0 = OFF) AND ``SCRAMBLER_PAGES`` (explicit comma
allow-list, default empty = OFF) enable a page; ``SCRAMBLER_DENY_PAGES``
(comma deny-list) wins over both. The switches are re-read at EXECUTION too:
a job whose cuts carry Scrambler records stops safely when its page is no
longer enabled (named failure), and a legacy job never becomes a Scrambler
job because the gate turned on after enqueue. Footage eligibility is the
material contract, never an account or storage-key prefix: only a resolved
``source_library`` / ``source_dna_recut`` recipe (``resolve_source_recipe``)
reaches this draw at all, and that resolver already refuses client-campaign
material. pov-club is NOT excluded: its 9.0 s delivered contract is exactly
the Scrambler's own 5-9 s range, and its existing speed/duration 409 still
guards an incompatible saved speed.

Reservations (one row per cut, keyed by the stable operation identity
``(page, job, cutIndex)``) are persisted in a private ledger beside the
durable job store, independently of Layer A (clip_variation), with the state
machine ``reserved -> rendered``: the draw is committed atomically at job
creation (before the render), enriched with the planned master/origin window,
and flipped to ``rendered`` only after the finished clip exists. Replaying
the same operation returns the stored reservation and never redraws, whatever
intervening history, date or configuration changes happened. A refused plan
or a failed job releases the job's reservations. Every persisted row is
validated on load (schema, types, ranges, timestamps); a corrupt or
wrong-schema ledger fails closed with a named error, and a ledger that
disappeared after it was first created fails closed as lost (the creation
marker distinguishes bootstrap from loss). The daily report counts RENDERED
cuts only and never fakes a ``posted_at`` for an unposted draw.
"""
from __future__ import annotations

import hashlib
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from services.generation_recovery import store_lock
from services.json_store import atomic_save

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ranges (spec section 7a + USER-UPDATE 2026-10-29; these win over earlier briefs)
# ---------------------------------------------------------------------------

# Slow branch: any value 0.3-0.8x on a 0.05 grid (11 buckets). All 11 stay in
# the vocabulary (validation, replay); the Worker window guard
# (``worker_admits_window``) leaves only 0.6-0.8x drawable.
SLOW_SPEED_MIN = 0.3
SLOW_SPEED_MAX = 0.8
SLOW_SPEED_STEP = 0.05
SLOW_SPEEDS = tuple(
    round(SLOW_SPEED_MIN + SLOW_SPEED_STEP * i, 2)
    for i in range(int(round((SLOW_SPEED_MAX - SLOW_SPEED_MIN) / SLOW_SPEED_STEP)) + 1)
)
# Fast branch (latest USER steering, which overrides every earlier speed
# note): Eric caps fastest at 1.3x. 1.1/1.2/1.3 ONLY - 1.4 is retired from the
# production vocabulary (its media benchmark stays historical helper evidence),
# and a persisted 1.4 row fails closed at validation even on replay or corrupt
# state. The Worker window guard caps 1.3x at 7.5 s (9.75 origin seconds).
FAST_SPEEDS = (1.1, 1.2, 1.3)
SPEEDS = tuple(sorted({*SLOW_SPEEDS, *FAST_SPEEDS}))

# Output length 5-9 s on a 0.5 s grid, so "equal to 0.5 s rounding" is an exact
# grid comparison. 9.0 s stays the Lab maximum (the Worker's admission bound).
LENGTH_MIN_MS = 5_000
LENGTH_MAX_MS = 9_000
LENGTH_STEP_MS = 500
LENGTHS = tuple(range(LENGTH_MIN_MS, LENGTH_MAX_MS + 1, LENGTH_STEP_MS))

# Sub-second start offset inside the planned window, 0.1 s steps.
OFFSET_STEP_MS = 100
OFFSET_MAX_MS = 900
OFFSETS = tuple(range(0, OFFSET_MAX_MS + 1, OFFSET_STEP_MS))

# Treatments (spec 7a step 3). "none" = post it plain; "split_swap" = LOOP9
# back-half-first; "camera" = slow push-in; "grade" = darker-only grade jitter.
# "reverse" is drawn only through the explicit eligibility gate below.
# Mirror is a separate on/off axis, not a treatment.
TREATMENTS = ("none", "split_swap", "camera", "grade", "reverse")
NON_REVERSE_TREATMENTS = ("none", "split_swap", "camera", "grade")

# Default branch/treatment weights. Explicitly configurable (frozen into each
# reservation for replay determinism): SCRAMBLER_FAST_SHARE (global, default
# 0.0 = 100% slow / 0% fast, matching the canonical slow-only §7a step 1), the
# fast branch is opt-in; SCRAMBLER_PAGE_FAST_SHARE
# ("page:share,page:share" per-page wins) and SCRAMBLER_TREATMENT_WEIGHTS
# ("none:3,split_swap:3,camera:3,grade:2").
DEFAULT_FAST_SHARE = 0.0
DEFAULT_TREATMENT_WEIGHTS = {"none": 3, "split_swap": 3, "camera": 3, "grade": 2}
MIRROR_SHARE = 1 / 3

# Reverse is the LOWEST-frequency treatment, ~1 in 15 (Eric 2026-10-28: "the
# least used one, but in rotation"). The per-slot gate is deterministic from
# the operation identity; when it passes AND the page still has its ET-day
# reverse AND the previous treatment was not reverse, the draw IS reverse.
# Long-run frequency is at or under 1/15 by construction: slots that fail the
# gate can never draw reverse, whatever else the anti-repeat rules exclude.
REVERSE_GATE = 1 / 15

# Brightness ceiling for the darker-grade treatment (the live Worker's
# BRIGHTNESS_CEILING): a graded cut never sits above it, so the treatment is
# offered only when the page look itself is at or under the ceiling.
GRADE_BRIGHTNESS_CEILING = 0.8
# The applied darker-only jitter, quantized to 0.001 downward, stays inside
# the Worker's existing 0.04 lower admission band (the same band Layer A's
# per-cut brightness jitter lives in), so the delivered pixels never leave
# what admission already tolerates while the receipt keeps the recipe look.
GRADE_DELTA_MIN = 0.004
GRADE_DELTA_MAX = 0.040
GRADE_DELTA_STEP = 0.001

# Reverse is capped cumulatively as well as probabilistically: a page's
# committed reverses may never exceed floor(cuts / 15) at ANY prefix. The
# per-slot gate keeps the frequency NEAR 1/15; this denominator is the proof.
REVERSE_CAP_DENOMINATOR = 15

# A Scrambler cut is drawn 5-9 s and held to 9.0 s after the re-encode.
SCRAMBLER_MAX_OUTPUT_S = 9.0
SCRAMBLER_MIN_OUTPUT_S = 5.0

# What the live Worker admits for a delivered sourced clip, read from Worker
# main 91f4e1a (2026-10-29): the clip's ORIGIN window
# (cutWindow.originalEndMs - originalStartMs) must be 5000..10000 ms
# (src/domain/sourceVideoBounds.js SOURCE_VIDEO_BOUNDS, enforced by
# src/ops/sourceWindowAdmission.js reserveSourceWindow and the D1 CHECK of
# migration 0091). A Scrambler cut shows window_ms() of origin at the drawn
# speed, so a draw whose window is outside that band would be encoded, marked
# rendered and then turned away at delivery. Such a draw is never legal here.
# The receipt is NOT where the speed goes: the same Worker requires the
# receipt's visualTreatment to equal the page recipe's look exactly
# (src/domain/sourceTreatment.js sourcePreparationBlocker) with clipSpeed in
# 0.5..2 (src/domain/renderTreatment.js), so the receipt keeps the recipe's
# values and the drawn speed stays in the manifest-only record.
WORKER_SOURCE_WINDOW_MIN_MS = 5_000
WORKER_SOURCE_WINDOW_MAX_MS = 10_000


def worker_admits_window(speed: float, length_ms: int) -> bool:
    """Whether the Worker admits the origin window of this (speed, length)."""
    window = int(round(length_ms * speed))
    return WORKER_SOURCE_WINDOW_MIN_MS <= window <= WORKER_SOURCE_WINDOW_MAX_MS


MAX_TRIES = 64

# ---------------------------------------------------------------------------
# Switches (read per call; deny wins; Warner excluded by contract)
# ---------------------------------------------------------------------------

SCRAMBLER_ENABLED_ENV = "SCRAMBLER_ENABLED"
SCRAMBLER_PAGES_ENV = "SCRAMBLER_PAGES"
SCRAMBLER_DENY_PAGES_ENV = "SCRAMBLER_DENY_PAGES"
SCRAMBLER_FAST_SHARE_ENV = "SCRAMBLER_FAST_SHARE"
SCRAMBLER_PAGE_FAST_SHARE_ENV = "SCRAMBLER_PAGE_FAST_SHARE"
SCRAMBLER_TREATMENT_WEIGHTS_ENV = "SCRAMBLER_TREATMENT_WEIGHTS"
# Release dependency (Worker main 91f4e1a, see docs/SCRAMBLER.md "What the
# live Worker admits"): the Worker's delivery admission requires the origin
# window of a delivered cut to be 5000..10000 ms and the receipt to carry the
# recipe's exact look. The draw offers only (speed, length) pairs whose window
# is inside that range (``worker_admits_window``: slow 0.6-0.8x, fast
# 1.1-1.3x limited by length, windows 5.1-9.9 s) and the receipt keeps the
# recipe look, so no drawn cut breaks either rule. The cut bounds are NEVER
# falsified to fit. ON still refuses until an operator sets this explicit
# acknowledgment that the deployed Worker enforces those rules. Drawing
# 0.3-0.55x again (windows under 5 s) needs a Worker release first.
SCRAMBLER_WORKER_COMPAT_ENV = "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT"

# No account/storage-prefix footage exclusion exists here on purpose: a
# ``tt-warner-`` storage scope is NOT client material (the BTL v2 library is
# Eric-approved sourced footage that MUST be scrambled). The only footage
# eligibility is the material contract itself - ``resolve_source_recipe``
# already confines Scrambler draws to ``source_library``/``source_dna_recut``
# recipes and refuses client-campaign material.


def _page_list(raw: str | None) -> frozenset[str]:
    return frozenset(part.strip() for part in (raw or "").split(",") if part.strip())


def scrambler_gate(page_id: str) -> tuple[bool, str]:
    """(enabled, reason) for one page; every branch is named for the log."""
    if not isinstance(page_id, str) or not page_id:
        return False, "page_id_invalid"
    enabled = str(os.environ.get(SCRAMBLER_ENABLED_ENV, "0")).strip()
    if enabled != "1":
        return False, "scrambler_disabled"
    if page_id in _page_list(os.environ.get(SCRAMBLER_DENY_PAGES_ENV)):
        return False, "scrambler_deny_listed"
    if page_id not in _page_list(os.environ.get(SCRAMBLER_PAGES_ENV)):
        return False, "scrambler_not_listed"
    if str(os.environ.get(SCRAMBLER_WORKER_COMPAT_ENV, "")).strip() != "1":
        return False, "scrambler_worker_source_window_compat_required"
    return True, "scrambler_on"


def scrambler_enabled(page_id: str) -> bool:
    """Whether ``page_id`` may draw a Scrambler combination (switches only)."""
    return scrambler_gate(page_id)[0]


def grade_allowed_for_look(brightness) -> bool:
    """The darker-grade treatment is offered only at/below the ceiling."""
    try:
        return float(brightness) <= GRADE_BRIGHTNESS_CEILING
    except (TypeError, ValueError):
        return False


def _parse_share(value, *, name: str) -> float:
    try:
        share = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}_invalid") from None
    if not 0.0 <= share <= 1.0:
        raise ValueError(f"{name}_invalid")
    return share


def _page_fast_share_entries(raw) -> list[tuple[str, str]]:
    """(page, share text) for each ``SCRAMBLER_PAGE_FAST_SHARE`` entry.

    Split at the LAST colon (see ``fast_share_for``); an entry with no colon or
    an empty share is ignored. The share text is NOT parsed here: the enqueue
    (``fast_share_for``) and preflight both parse it with ``_parse_share``.
    """
    entries = []
    for part in str(raw or "").split(","):
        page, sep, share = part.strip().rpartition(":")
        if sep and share.strip():
            entries.append((page, share.strip()))
    return entries


def fast_share_for(page_id: str) -> float:
    """The page's fast-branch share (per-page override, then global, then 0.0).

    Read per call so an operator can retune the pilot without a restart; the
    value actually used is frozen into the reservation at draw time, so a
    later change never disturbs an existing operation's replay. Page ids may
    themselves contain colons (``acct:<uuid>``, ``tt-<handle>`` scopes), so
    the override splits at the LAST colon - a first-colon split would read
    ``acct`` as the page and silently drop every real override.
    """
    for page, share in _page_fast_share_entries(os.environ.get(SCRAMBLER_PAGE_FAST_SHARE_ENV)):
        if page == page_id:
            return _parse_share(share, name="scrambler_page_fast_share")
    raw_global = os.environ.get(SCRAMBLER_FAST_SHARE_ENV)
    if raw_global is None or not str(raw_global).strip():
        return DEFAULT_FAST_SHARE
    return _parse_share(str(raw_global).strip(), name="scrambler_fast_share")


def treatment_weights() -> dict[str, int]:
    """The non-reverse treatment weights (explicit, deterministic, overridable).

    Weights are soft preferences. Zero normally removes a treatment from
    weighted selection, but strict no-repeat takes precedence: when all
    remaining legal weights are zero, selection uses the default weights.
    These settings do not disable treatments.
    """
    raw = os.environ.get(SCRAMBLER_TREATMENT_WEIGHTS_ENV, "")
    weights = dict(DEFAULT_TREATMENT_WEIGHTS)
    for part in raw.split(","):
        name, sep, value = part.strip().partition(":")
        if sep and name in DEFAULT_TREATMENT_WEIGHTS and value.strip().isdigit():
            weights[name] = int(value.strip())
    return weights


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")


def speed_bucket(speed: float) -> float:
    """The canonical speed bucket for the anti-repeat combos (the grid value)."""
    return round(float(speed), 2)


@dataclass(frozen=True)
class ScramblerDraw:
    treatment: str
    speed: float
    length_ms: int
    offset_ms: int
    mirror: bool = False
    # The darker-only grade jitter (0 for every non-grade treatment): the
    # executor subtracts this from the page look's brightness, quantized down
    # to 0.001, never below look - 0.04. Private manifest value only.
    grade_brightness_delta: float = 0.0

    def combo(self) -> tuple[float, int, str, bool]:
        """The (speed bucket, length, treatment, mirror) key that never repeats."""
        return (speed_bucket(self.speed), self.length_ms, self.treatment, bool(self.mirror))

    def offset_combo(self) -> tuple[float, int, str, bool, int]:
        """The canonical offset combo (adds the offset); never repeats either."""
        return (*self.combo(), self.offset_ms)

    def window_ms(self) -> int:
        """The origin window that delivers ``length_ms`` at ``speed``.

        Delivered length = window / speed, so the window is ``length x speed``
        (a whole millisecond count on these grids). This is the exact origin
        window covered: ``origin_seconds = output_seconds x speed``, counted
        once, never underreported - including the fast branch, where a 7.5 s
        output at 1.3x covers 9.75 s of origin.
        """
        return int(round(self.length_ms * self.speed))

    def span_ms(self) -> int:
        """The FULL source span the planner must reserve: offset + window.

        Reserved BEFORE the window is selected (draw-before-plan), so a fast
        cut can never be handed a slow-sized window to speed up afterwards.
        """
        return self.offset_ms + self.window_ms()

    def is_fast(self) -> bool:
        return speed_bucket(self.speed) in FAST_SPEEDS

    def manifest(self) -> dict:
        """The Lab-private per-cut manifest record (never a receipt or wire field)."""
        record = {
            "schema": SCRAMBLER_CUT_SCHEMA,
            "treatment": self.treatment,
            "mirror": bool(self.mirror),
            "speed": speed_bucket(self.speed),
            "branch": "fast" if self.is_fast() else "slow",
            "lengthMs": self.length_ms,
            "offsetMs": self.offset_ms,
            "windowMs": self.window_ms(),
            "spanMs": self.span_ms(),
            # origin_seconds = output_seconds x effective speed (rule #1).
            "originSeconds": self.window_ms() / 1000.0,
        }
        if self.treatment == "grade" and self.grade_brightness_delta:
            record["gradeBrightnessDelta"] = round(float(self.grade_brightness_delta), 3)
        return record

    def transforms_manifest(self) -> dict:
        """The Layer B ``clipTransforms`` record for this draw (manifest-only).

        split_swap is applied by this module (services.scrambler
        apply_split_swap); mirror/camera/reverse reuse services.clip_transforms.
        """
        motion = "push_in" if self.treatment == "camera" else None
        return {
            "mirror": bool(self.mirror),
            "motion": motion,
            "reverse": self.treatment == "reverse",
            "split_swap": self.treatment == "split_swap",
        }

    def applies_transforms(self) -> bool:
        return bool(self.mirror) or self.treatment in {"split_swap", "camera", "reverse"}


def _unit(seed: str, part: str) -> float:
    digest = hashlib.sha256(f"{seed}\0{part}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _grade_delta(seed: str) -> float:
    """A deterministic darker-only brightness delta in [0.004, 0.040].

    Quantized to 0.001 and bounded by the Worker's 0.04 lower admission band,
    so applying look - delta keeps the delivered pixels inside what admission
    already tolerates for a per-cut brightness jitter.
    """
    steps = int(round((GRADE_DELTA_MAX - GRADE_DELTA_MIN) / GRADE_DELTA_STEP))  # 36
    value = GRADE_DELTA_MIN + GRADE_DELTA_STEP * int(_unit(seed, "grade-delta") * (steps + 1))
    return round(min(value, GRADE_DELTA_MAX), 3)


def _weighted_treatment(seed: str, choices: tuple[str, ...], weights: dict[str, int]) -> str:
    """A seeded weighted treatment pick honoring explicit zeros.

    A zero-weighted treatment is retired only WHILE that is legal: the hard
    no-consecutive-treatment rule outranks the weights, so when every legal
    choice is zero-weighted the DEFAULT weights decide that draw (named log).
    The hard rule is never relaxed by configuration.
    """
    weighted = [(name, max(0, weights.get(name, 1))) for name in choices]
    total = sum(weight for _name, weight in weighted)
    if not total:
        _log_once(
            "treatment-weights-empty:" + seed.split("\0")[0],
            "every legal treatment choice for this draw is zero-weighted; "
            "using the default weights so the no-consecutive-treatment rule "
            "stays hard",
        )
        weighted = [(name, DEFAULT_TREATMENT_WEIGHTS.get(name, 1)) for name in choices]
        total = sum(weight for _name, weight in weighted)
    unit = _unit(seed, "treatment")
    try:
        # Preserve the original float-product/index semantics for ordinary
        # configurations, without expanding a list by the numeric weights.
        roll = int(unit * total)
    except OverflowError:
        # Extremely large Python integers still need only bounded intervals;
        # exact float-ratio arithmetic avoids converting the total to float.
        numerator, denominator = unit.as_integer_ratio()
        roll = numerator * total // denominator
    cumulative = 0
    for name, weight in weighted:
        cumulative += weight
        if roll < cumulative:
            return name
    raise ValueError("scrambler_treatment_weights_empty")


def _candidate(seed: str, *, previous: ScramblerDraw | None, reverse_eligible: bool,
               grade_allowed: bool, fast_share: float,
               weights: dict[str, int]) -> ScramblerDraw | None:
    """One seeded candidate under every hard rule.

    Branch first (fast vs slow), then speed inside the branch, then treatment
    (reverse selected DIRECTLY when the gate made this slot eligible), then a
    length that is not the previous cut's length, then offset and mirror.
    """
    branch = FAST_SPEEDS if _unit(seed, "branch") < fast_share else SLOW_SPEEDS
    # Only speeds with at least one Worker-admissible length are drawable.
    branch = tuple(s for s in branch if any(worker_admits_window(s, ms) for ms in LENGTHS))
    if not branch:
        return None
    speed = branch[int(_unit(seed, "speed") * len(branch))]
    if reverse_eligible:
        treatment = "reverse"
    else:
        excluded = previous.treatment if previous is not None else None
        choices = tuple(
            name for name in NON_REVERSE_TREATMENTS
            if name != excluded and (name != "grade" or grade_allowed)
        )
        if not choices:
            return None
        treatment = _weighted_treatment(seed, choices, weights)
    lengths = tuple(
        ms for ms in LENGTHS
        if (previous is None or ms != previous.length_ms) and worker_admits_window(speed, ms)
    )
    if not lengths:
        return None
    length_ms = lengths[int(_unit(seed, "length") * len(lengths))]
    offset_ms = OFFSETS[int(_unit(seed, "offset") * len(OFFSETS))]
    return ScramblerDraw(
        treatment=treatment,
        speed=speed,
        length_ms=length_ms,
        offset_ms=offset_ms,
        mirror=_unit(seed, "mirror") < MIRROR_SHARE,
        grade_brightness_delta=_grade_delta(seed) if treatment == "grade" else 0.0,
    )


def _rule_violations(candidate: ScramblerDraw, previous: ScramblerDraw | None,
                     used_combos: frozenset, used_offset_combos: frozenset) -> list[str]:
    """The hard rules a candidate breaks (empty = a legal draw). No relaxation."""
    violations = []
    if not worker_admits_window(candidate.speed, candidate.length_ms):
        violations.append("worker_source_window")
    if previous is not None:
        if candidate.treatment == previous.treatment:
            violations.append("previous_treatment")
        if candidate.length_ms == previous.length_ms:
            violations.append("previous_length")
    if candidate.combo() in used_combos:
        violations.append("combo_repeat")
    if candidate.offset_combo() in used_offset_combos:
        violations.append("offset_combo_repeat")
    return violations


def _walk_exhaustive(previous: ScramblerDraw | None, *, reverse_eligible: bool,
                     grade_allowed: bool, weights: dict[str, int],
                     used_combos: frozenset, used_offset_combos: frozenset,
                     fast_share: float = DEFAULT_FAST_SHARE) -> ScramblerDraw | None:
    """Deterministic exhaustive backstop: the first legal candidate in a fixed
    weight-then-vocabulary order. The seeded draw normally decides; this only
    runs when the seed collided with the rules MAX_TRIES times. Strict rules
    only - it never returns a candidate that breaks one, and there is no
    relaxation path anywhere. The backstop honors the page's branch mix: an
    all-fast page (share 1) only walks fast speeds, an all-slow page (share 0)
    only slow ones - the configured bias is a claim, not a hint."""
    if fast_share >= 1.0:
        speeds: tuple[float, ...] = FAST_SPEEDS
    elif fast_share <= 0.0:
        speeds = SLOW_SPEEDS
    else:
        speeds = SLOW_SPEEDS + FAST_SPEEDS
    order = sorted(NON_REVERSE_TREATMENTS, key=lambda name: (-weights.get(name, 1), name))
    treatments: tuple[str, ...] = (*order, "reverse") if reverse_eligible else order
    excluded = previous.treatment if previous is not None else None
    for treatment in treatments:
        if treatment == excluded:
            continue
        if treatment == "grade" and not grade_allowed:
            continue
        for speed in speeds:
            for length_ms in LENGTHS:
                if previous is not None and length_ms == previous.length_ms:
                    continue
                for mirror in (False, True):
                    for offset_ms in OFFSETS:
                        candidate = ScramblerDraw(
                            treatment, speed, length_ms, offset_ms, mirror,
                            grade_brightness_delta=(
                                _grade_delta(f"{speed}\0{length_ms}\0{offset_ms}\0{mirror}")
                                if treatment == "grade" else 0.0),
                        )
                        if not _rule_violations(candidate, previous, used_combos, used_offset_combos):
                            return candidate
    return None


class ScramblerExhausted(ValueError):
    """No legal draw remains under the hard rules. Job creation must refuse.

    A named, structured condition - never a silent rule relaxation. The
    rotation history horizon limits how far BACK the anti-repeat looks; an
    exhausted page needs new variety configuration or a fresh combo horizon,
    not a reused combination.
    """


class ScramblerHistoryCorrupt(RuntimeError):
    """The private scrambler ledger is unreadable, wrong-schema or has a bad row."""


class ScramblerHistoryLost(RuntimeError):
    """The ledger vanished after it was first created (creation marker present)."""


_logged_once: set[str] = set()
_logged_once_guard = threading.Lock()


def _log_once(key: str, message: str, *args, level: int = logging.WARNING) -> None:
    """Every fallback or refusal logs once by name (pristine checklist #1).

    The key rides as the first %-argument so the formatted line carries it.
    """
    with _logged_once_guard:
        if key in _logged_once:
            return
        _logged_once.add(key)
    log.log(level, "content_lab.scrambler.%s " + message, key, *args)


def et_day(value) -> str:
    """The ET (America/New_York) calendar day of an ISO timestamp or now.

    Zoneinfo handles DST: the spring-forward 23 h and fall-back 25 h days are
    real calendar days on this clock, and the reverse day-cap binds to the
    reservation's stored ET day, not a sliding 86 400-second window.
    """
    moment = None
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            moment = None
    if moment is None:
        moment = datetime.now(ET)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ET).strftime("%Y-%m-%d")


_ET_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_epoch(value) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _validate_iso(value, *, field: str) -> None:
    if not isinstance(value, str) or not value or _iso_epoch(value) == 0.0:
        raise ScramblerHistoryCorrupt(f"scrambler_row_{field}_invalid")


# ---------------------------------------------------------------------------
# The persisted reservation ledger
# ---------------------------------------------------------------------------

SCRAMBLER_HISTORY_SCHEMA = "content-lab.scrambler-history.v3"
SCRAMBLER_CUT_SCHEMA = "content-lab.scrambler-cut.v2"
SCRAMBLER_CREATED_SCHEMA = "content-lab.scrambler-created.v1"
SCRAMBLER_HISTORY_NAME = "scrambler-history.json"
# Active combos rotate within 64 rows. After the Worker window guard and the
# previous length/treatment exclusions, the worst case is 60 eligible
# nonreverse choices on the slow branch with no grade (15 speed/length pairs x
# 2 treatments x mirror) and 76 on the fast branch with no grade. 60 is under
# the 64-row rotation, so a slow-only/no-grade page can in principle exhaust;
# a 9,000-draw simulation (30 pages x 300 cuts, 2026-10-30) hit none.
# Exhaustion refuses by name (ScramblerExhausted); no rule is relaxed. The
# durable ledger is separate: retain EVERY operation identity, fail closed at
# capacity.
ACTIVE_ROTATION_ROWS = 64
SCRAMBLER_HISTORY_HORIZON = 4000  # maximum rows/page, not a pruning horizon
SCRAMBLER_HISTORY_MAX_ROWS = 20000
SCRAMBLER_HISTORY_MAX_PAGES = 256
SCRAMBLER_HISTORY_MAX_BYTES = 32 * 1024 * 1024
RESERVED = "reserved"
RENDERED = "rendered"
# Tombstone: the row keeps its immutable operation identity (jobId, cutIndex,
# slotId, combo) so valid cleanup can never trip the duplicate/noncontiguity
# guards, but it is no longer an active commitment - it holds no combo, no
# ET-day reverse and no counter credit.
RELEASED = "released"


def scrambler_history_path() -> Path:
    override = os.environ.get("SCRAMBLER_HISTORY_PATH")
    if override:
        if not Path(override).is_absolute():
            raise ScramblerHistoryCorrupt("scrambler_history_path_invalid")
        return Path(override)
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if (not mount or not Path(mount).is_absolute() or not Path(mount).is_dir()
            or not os.access(mount, os.W_OK)):
        raise ScramblerHistoryCorrupt("scrambler_durable_volume_unavailable")
    return Path(mount) / SCRAMBLER_HISTORY_NAME


def _combo_of(row: dict):
    try:
        return (speed_bucket(float(row["speed"])), int(row["lengthMs"]),
                str(row["treatment"]), bool(row["mirror"]))
    except (KeyError, TypeError, ValueError):
        return None


def _offset_combo_of(row: dict):
    combo = _combo_of(row)
    if combo is None:
        return None
    try:
        return (*combo, int(row["offsetMs"]))
    except (KeyError, TypeError, ValueError):
        return None


def _draw_from_row(row: dict) -> ScramblerDraw | None:
    combo = _combo_of(row)
    if combo is None:
        return None
    try:
        offset_ms = int(row["offsetMs"])
    except (KeyError, TypeError, ValueError):
        return None
    delta = row.get("gradeBrightnessDelta") or 0.0
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        return None
    return ScramblerDraw(treatment=combo[2], speed=combo[0], length_ms=combo[1],
                         offset_ms=offset_ms, mirror=combo[3],
                         grade_brightness_delta=delta)


def _posting_page_valid(value) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256
            and value.isprintable())


def _validate_row(row, *, index: int) -> dict:
    """Validate one persisted row completely, or fail closed.

    Types, ranges, finiteness, timestamps and schema are all checked: a
    corrupt row could hide a repeat, a double reverse or a lost day-cap, so
    drawing with it is refused rather than best-efforted.
    """
    where = f"row[{index}]"
    if not isinstance(row, dict):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_not_an_object")
    if row.get("schema") != SCRAMBLER_CUT_SCHEMA:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_schema_invalid")
    if not isinstance(row.get("jobId"), str) or not row["jobId"]:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_jobId_invalid")
    cut_index = row.get("cutIndex")
    if isinstance(cut_index, bool) or not isinstance(cut_index, int) or cut_index < 0:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_cutIndex_invalid")
    if not isinstance(row.get("slotId"), str) or not row["slotId"]:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_slotId_invalid")
    if row.get("state") not in (RESERVED, RENDERED, RELEASED):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_state_invalid")
    if row.get("state") == RELEASED:
        # A tombstone must prove its release; its rendered/usage truth stays
        # as reserved drew it (nothing was rendered).
        if not isinstance(row.get("releasedAt"), str) or not row["releasedAt"]:
            raise ScramblerHistoryCorrupt(f"scrambler_{where}_releasedAt_invalid")
    _validate_iso(row.get("drawnAt"), field=f"{where}_drawnAt")
    if not isinstance(row.get("etDay"), str) or not _ET_DAY_RE.match(row["etDay"]):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_etDay_invalid")
    treatment = row.get("treatment")
    if treatment not in TREATMENTS:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_treatment_invalid")
    if not isinstance(row.get("mirror"), bool):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_mirror_invalid")
    speed = row.get("speed")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_speed_invalid")
    speed = float(speed)
    # EXACT vocabulary membership, not bucket-rounded: an off-grid 0.301 must
    # fail closed here, never be silently normalized to 0.3 on replay.
    if speed != speed or speed in (float("inf"), float("-inf")) or speed not in SPEEDS:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_speed_out_of_vocabulary")
    length_ms = row.get("lengthMs")
    if isinstance(length_ms, bool) or not isinstance(length_ms, int) or length_ms not in LENGTHS:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_lengthMs_invalid")
    offset_ms = row.get("offsetMs")
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int) or offset_ms not in OFFSETS:
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_offsetMs_invalid")
    delta = row.get("gradeBrightnessDelta", 0.0)
    if treatment == "grade":
        ok = (
            isinstance(delta, (int, float)) and not isinstance(delta, bool)
            and GRADE_DELTA_MIN <= float(delta) <= GRADE_DELTA_MAX
            and abs(float(delta) * 1000 - round(float(delta) * 1000)) < 1e-6
        )
        if not ok:
            raise ScramblerHistoryCorrupt(f"scrambler_{where}_gradeBrightnessDelta_invalid")
    elif delta not in (None, 0, 0.0):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_gradeBrightnessDelta_invalid")
    if row.get("state") == RENDERED:
        _validate_iso(row.get("renderedAt"), field=f"{where}_renderedAt")
        if not isinstance(row.get("assetSha256"), str) or not row["assetSha256"]:
            raise ScramblerHistoryCorrupt(f"scrambler_{where}_assetSha256_invalid")
    if "renderOrder" in row and (
            row.get("state") != RENDERED or isinstance(row["renderOrder"], bool)
            or not isinstance(row["renderOrder"], int) or row["renderOrder"] <= 0):
        raise ScramblerHistoryCorrupt("scrambler_render_order_invalid")
    for optional_int in ("windowMs", "spanMs", "libraryStartMs", "originStartMs", "originEndMs"):
        value = row.get(optional_int)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ScramblerHistoryCorrupt(f"scrambler_{where}_{optional_int}_invalid")
    if "originKind" in row and (
            any(not isinstance(row.get(key), int) for key in
                ("originStartMs", "originEndMs", "libraryStartMs", "windowMs"))
            or row["originEndMs"] <= row["originStartMs"]
            or row["originEndMs"] - row["originStartMs"] != row["windowMs"]
            or row["windowMs"] != _draw_from_row(row).window_ms()):
        raise ScramblerHistoryCorrupt("scrambler_origin_geometry_invalid")
    if "originKind" in row or "originId" in row:
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ("originKind", "originId")):
            raise ScramblerHistoryCorrupt("scrambler_origin_identity_invalid")
    hold_release = row.get("holdRelease")
    if hold_release is not None:
        if (not isinstance(hold_release, dict) or row.get("state") != RENDERED
                or set(hold_release) != {"at", "reason"}
                or hold_release.get("reason") != UNDELIVERED_HOLD_REASON):
            raise ScramblerHistoryCorrupt(f"scrambler_{where}_holdRelease_invalid")
        _validate_iso(hold_release.get("at"), field=f"{where}_holdRelease_at")
    observation = row.get("postingObservation")
    if observation is not None:
        if (not isinstance(observation, dict) or row.get("state") != RENDERED
                or observation.get("assetSha256") != row.get("assetSha256")
                or not _posting_page_valid(observation.get("page"))):
            raise ScramblerHistoryCorrupt("scrambler_posting_observation_invalid")
        _validate_iso(observation.get("posted_at"), field="posting_observation_posted_at")
    usage = row.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ScramblerHistoryCorrupt(f"scrambler_{where}_usage_invalid")
    return row


def _validate_history(data, *, path: Path) -> dict:
    if not isinstance(data, dict) or data.get("schema") != SCRAMBLER_HISTORY_SCHEMA \
            or not isinstance(data.get("pages"), dict):
        # Fail closed: drawing with unknown history could repeat combos or a
        # day's reverse. The job terminalizes with this named error instead.
        raise ScramblerHistoryCorrupt("scrambler_history_schema_invalid")
    if (len(data["pages"]) > SCRAMBLER_HISTORY_MAX_PAGES
            or sum(len(rows) for rows in data["pages"].values() if isinstance(rows, list))
            > SCRAMBLER_HISTORY_MAX_ROWS):
        raise ScramblerHistoryCorrupt("scrambler_history_capacity_exceeded")
    _validate_iso(data.get("createdAt"), field="history_createdAt")
    counters = data.get("counters")
    if not isinstance(counters, dict):
        raise ScramblerHistoryCorrupt("scrambler_history_counters_invalid")
    for page_id, counter in counters.items():
        if not isinstance(page_id, str) or not page_id or not isinstance(counter, dict):
            raise ScramblerHistoryCorrupt("scrambler_history_counters_invalid")
        cuts = counter.get("cuts")
        reverses = counter.get("reverses")
        if (isinstance(cuts, bool) or not isinstance(cuts, int) or cuts < 0
                or isinstance(reverses, bool) or not isinstance(reverses, int)
                or reverses < 0 or reverses > cuts):
            raise ScramblerHistoryCorrupt("scrambler_history_counters_invalid")
    for page_id, rows in data["pages"].items():
        if not isinstance(page_id, str) or not page_id or not isinstance(rows, list):
            raise ScramblerHistoryCorrupt("scrambler_history_pages_invalid")
        if len(rows) > SCRAMBLER_HISTORY_HORIZON:
            raise ScramblerHistoryCorrupt("scrambler_history_page_rows_exceeded")
        # Every job's cut identities are unique and contiguous from 0: two
        # rows for one (job, cutIndex) would replay as one cut twice, and a
        # gap would let a replay return fewer cuts than were reserved.
        # Indices are collected as a LIST: a set would collapse a duplicated
        # cutIndex and silently pass the contiguity check.
        per_job: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            _validate_row(row, index=index)
            if row["slotId"] != f"{row['jobId']}#cut-{row['cutIndex']}":
                raise ScramblerHistoryCorrupt(f"scrambler_row[{index}]_slotId_key_mismatch")
            per_job.setdefault(row["jobId"], []).append(row["cutIndex"])
        for job_id, indices in per_job.items():
            if sorted(indices) != list(range(len(indices))):
                raise ScramblerHistoryCorrupt(
                    f"scrambler_history_job_cuts_invalid: job {job_id} of page "
                    f"{page_id} holds duplicated or non-contiguous cut indices"
                )
        counter = counters.get(page_id)
        # Counters track LIVE commitments (reserved + rendered); tombstones
        # hold no credit. counters.cuts must cover every live row.
        live_rows = [row for row in rows if row.get("state") != RELEASED]
        if counter is None or counter.get("cuts", 0) < len(live_rows):
            raise ScramblerHistoryCorrupt(
                f"scrambler_history_counters_invalid: page {page_id} counters "
                "do not cover its persisted live rows"
            )
    return data


def _created_marker_path(path: Path) -> Path:
    return path.with_name(path.name + ".created")


def _load_history_at(path: Path) -> dict:
    """Load and fully validate the ledger; distinguish bootstrap from loss.

    First enable (no ledger, no marker) CREATES both, marker first, and starts
    an empty ledger with a named INFO log. Because the marker lands before the
    ledger, any later disappearance of the ledger while the marker remains is
    LOSS: the anti-repeat history is unknown, so every draw fails closed with
    ``ScramblerHistoryLost`` until an operator restores the file. The reserve
    caller also supplies existing Scrambler job IDs from its authoritative
    job store, so loss of BOTH files cannot masquerade as bootstrap while
    those jobs remain. No implicit history reset is supported.
    """
    marker = _created_marker_path(path)
    if not path.exists():
        if marker.exists():
            raise ScramblerHistoryLost("scrambler_history_lost")
        _log_once(
            "history-bootstrap",
            "scrambler history %s does not exist; creating an empty ledger "
            "(bootstrap: first enable of the Scrambler)",
            str(path), level=logging.INFO,
        )
        empty = {"schema": SCRAMBLER_HISTORY_SCHEMA,
                 "createdAt": datetime.now(timezone.utc).isoformat(),
                 "pages": {}, "counters": {}}
        _save_history_at(path, empty)
        return empty
    try:
        # Bound the actual read even if an external writer grows the file.
        with path.open("rb") as stream:
            raw = stream.read(SCRAMBLER_HISTORY_MAX_BYTES + 1)
        if len(raw) > SCRAMBLER_HISTORY_MAX_BYTES:
            raise ScramblerHistoryCorrupt("scrambler_history_bytes_exceeded")
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError) as error:
        raise ScramblerHistoryCorrupt(f"scrambler_history_unreadable: {error}") from error
    return _validate_history(data, path=path)


def _save_history_at(path: Path, history: dict) -> None:
    """Persist the ledger; on first creation the marker lands FIRST.

    Marker-first is what makes loss detectable: if the process dies after the
    marker but before the ledger write, the next load sees the marker without
    the ledger and fails closed as LOSS instead of quietly bootstrapping a
    fresh history that would silently unlock every used combo.
    """
    _validate_history(history, path=path)
    if len(json.dumps(history, ensure_ascii=True).encode("utf-8")) > SCRAMBLER_HISTORY_MAX_BYTES:
        raise ScramblerHistoryCorrupt("scrambler_history_bytes_exceeded")
    marker = _created_marker_path(path)
    if not marker.exists():
        atomic_save(marker, {
            "schema": SCRAMBLER_CREATED_SCHEMA,
            "createdAt": history.get("createdAt"),
            "history": path.name,
        })
    atomic_save(path, history)


def _page_rows(history: dict, page_id: str) -> list[dict]:
    rows = history["pages"].get(page_id)
    return list(rows) if isinstance(rows, list) else []


def _usage_row(page_id: str, draw: ScramblerDraw, *, origin_id: str | None,
               origin_start_ms: int | None) -> dict:
    """An honest originality-usage row in the real usage.jsonl shape.

    ``posted_at`` stays null: the Lab draws and renders, it never posts, and a
    report must never fake a posting timestamp for an unposted draw.
    ``asset_sha`` is filled when the render exists. The planner/reservation
    scopes below stay authoritative for ownership; this row is the projection
    originality's tooling can consume.
    """
    window_ms = draw.window_ms()
    return {
        "page": page_id,
        "posted_at": None,
        "video_id": None,
        "final_sha": None,
        "asset_sha": None,
        "origin_kind": "content_lab_source",
        "origin_id": origin_id,
        "origin_start_s": round(origin_start_ms / 1000.0, 3) if origin_start_ms is not None else None,
        "origin_end_s": (round((origin_start_ms + window_ms) / 1000.0, 3)
                         if origin_start_ms is not None else None),
        "recipe": {
            "speed": speed_bucket(draw.speed),
            "reverse": draw.treatment == "reverse",
            "loop_split": draw.treatment == "split_swap",
            "crop": None,
            "hflip": bool(draw.mirror),
        },
    }


def _record_row(page_id: str, job_id: str, cut_index: int, draw: ScramblerDraw, *,
                drawn_at: str, et_day_value: str, fast_share: float,
                weights: dict[str, int]) -> dict:
    """One reserved ledger row (the Lab-private manifest plus ledger state)."""
    row = {
        "schema": SCRAMBLER_CUT_SCHEMA,
        "jobId": job_id,
        "cutIndex": cut_index,
        "slotId": f"{job_id}#cut-{cut_index}",
        "state": RESERVED,
        "drawnAt": drawn_at,
        "etDay": et_day_value,
        "config": {"fastShare": fast_share, "weights": dict(weights)},
        # Enriched after planning (master + origin window), before job commit.
        "masterSha256": None,
        "masterSourceId": None,
        "libraryStartMs": None,
        "originStartMs": None,
        "originEndMs": None,
        "usage": _usage_row(page_id, draw, origin_id=None, origin_start_ms=None),
    }
    row.update(draw.manifest())
    return row


def _draw_strict(page_id: str, job_id: str, cut_index: int, rows: list[dict], *,
                 grade_allowed: bool, fast_share: float, weights: dict[str, int],
                 now: str, page_cuts: int, page_reverses: int) -> ScramblerDraw:
    """The draw for one cut against the page's committed LIVE rows. Strict rules only.

    ``rows`` are the page's durable rows (most-recent-last), already including
    this job's earlier cuts, so same-job anti-repeat and the one-reverse-per-
    ET-day cap hold inside a job as well as across jobs - in-flight jobs
    included, because reservations commit at draw time. Tombstones (RELEASED
    rows) hold no commitment: they never provide a previous treatment, a
    day-cap reverse or a consumed combo. The combo anti-repeat covers the
    newest ACTIVE_ROTATION_ROWS live rows - the advertised rotation - so a
    page can never brick on a vocabulary smaller than its history.

    ``page_cuts`` is the page's DURABLE rendered-cut count and
    ``page_reverses`` every live reverse (reserved or rendered): a reverse
    additionally requires ``(reverses + 1) x 15 <= rendered_cuts + 1``. The
    denominator is durable RENDERED support only - releasing pending
    non-reverse rows can never retroactively push an already-rendered reverse
    over the cap - while a pending reverse occupies the numerator until it
    renders or releases. The reverse share is provably at or under 1/15 at
    EVERY prefix of rendered cuts, not merely in expectation.
    """
    live = [row for row in rows if row.get("state") != RELEASED]
    previous: ScramblerDraw | None = None
    if live:
        previous = _draw_from_row(live[-1])
    rotation = live[-ACTIVE_ROTATION_ROWS:]
    used_combos = frozenset(filter(None, (_combo_of(row) for row in rotation)))
    used_offset_combos = frozenset(
        filter(None, (_offset_combo_of(row) for row in rotation)))
    today = et_day(now)
    reverse_day_used = any(
        row.get("treatment") == "reverse" and (
            row.get("etDay") == today
            or (row.get("state") == RENDERED
                and et_day(row["renderedAt"]) == today))
        for row in live
    )
    seed = f"{page_id}\0{job_id}\0cut-{cut_index}\0scrambler-v3"
    # The explicit reverse eligibility gate: roughly 1 in 15 slots are even
    # eligible, independent of which treatment the anti-repeat rule excludes.
    # When it passes (and the CUMULATIVE cap and the day cap and the
    # previous-treatment rule allow), the draw IS reverse - direct selection,
    # never a renormalised weight. The cumulative cap is the proof the
    # long-run rate never exceeds 1/15; the gate only keeps it near 1/15.
    gate_passes = _unit(seed, "reverse-gate") < REVERSE_GATE
    cap_allows = (
        (page_reverses + 1) * REVERSE_CAP_DENOMINATOR <= page_cuts + 1
    )
    reverse_eligible = (
        gate_passes and cap_allows and not reverse_day_used
        and (previous is None or previous.treatment != "reverse")
    )
    if gate_passes and reverse_day_used:
        _log_once(f"reverse-day-cap:{page_id}:{today}",
                  "reverse gate passed but page %s already used its one reverse "
                  "for ET day %s; drawing a non-reverse treatment",
                  page_id, today)
    elif gate_passes and not cap_allows:
        _log_once(f"reverse-cap:{page_id}",
                  "reverse gate passed but page %s already holds %d reverses in "
                  "%d committed cuts (cumulative 1-in-%d cap); drawing a "
                  "non-reverse treatment",
                  page_id, page_reverses, page_cuts, REVERSE_CAP_DENOMINATOR)
    for attempt in range(MAX_TRIES):
        candidate = _candidate(
            seed if attempt == 0 else f"{seed}#{attempt}",
            previous=previous, reverse_eligible=reverse_eligible,
            grade_allowed=grade_allowed, fast_share=fast_share, weights=weights,
        )
        if candidate is not None and not _rule_violations(
                candidate, previous, used_combos, used_offset_combos):
            return candidate
    backstop = _walk_exhaustive(
        previous, reverse_eligible=reverse_eligible, grade_allowed=grade_allowed,
        weights=weights, used_combos=used_combos,
        used_offset_combos=used_offset_combos, fast_share=fast_share)
    if backstop is not None:
        return backstop
    # Named exhaustion, never a silent relaxation: the hard rules stay hard.
    _log_once(f"exhausted:{page_id}",
              "no legal scrambler draw remains for page %s (%d combos in the "
              "rotation horizon); refusing the job",
              page_id, len(used_combos))
    raise ScramblerExhausted(
        f"scrambler_combo_space_exhausted: page {page_id} has no legal "
        f"(speed, length, treatment, mirror, offset) combination left in its "
        f"rotation history"
    )


def _draws_from_job(rows: list[dict], job_id: str) -> list[ScramblerDraw]:
    job_rows = sorted(
        (row for row in rows if row.get("jobId") == job_id),
        key=lambda row: row["cutIndex"],
    )
    return [_draw_from_row(row) for row in job_rows]


def reserve_job_cuts(
    page_id: str,
    job_id: str,
    quantity: int,
    *,
    grade_allowed: bool = True,
    now: str | None = None,
    path: Path | None = None,
    existing_job_ids: Iterable[str] | None = None,
) -> list[ScramblerDraw]:
    """Draw and atomically reserve one job's cuts (draw-before-plan).

    One cross-process ``store_lock`` section covers load -> validate -> draw
    each cut (each sees the previous cut's committed row) -> append -> save,
    so the reverse/day cap and the combo anti-repeat include every other draw
    this process or another has already committed - in-flight jobs included -
    and a concurrent same-page job cannot slip a repeat between the read and
    the write. Rows commit at draw time (before the render or even the plan).

    Replaying the same operation (same page, job and quantity) returns the
    STORED reservations and never redraws, whatever intervening history, date
    or configuration changes happened; the stored config snapshot records
    what the draw actually used. The returned draws' ``span_ms()`` values are
    the spans the caller must hand the planner.
    """
    if not isinstance(page_id, str) or not page_id:
        raise ValueError("scrambler reservation requires a page id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("scrambler reservation requires a job id")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("scrambler reservation requires a positive quantity")
    path = path or scrambler_history_path()
    with store_lock(path):
        assert_existing_jobs(existing_job_ids or (), path=path)
        history = _load_history_at(path)
        rows = _page_rows(history, page_id)
        # Replaying the same operation (same page, job and quantity) returns
        # the STORED reservations and never redraws, whatever intervening
        # history, date or configuration changes happened; the stored config
        # snapshot records what the draw actually used. A released operation
        # is permanently terminal; its identity can never draw fresh.
        if any(row.get("jobId") == job_id and row.get("state") == RELEASED for row in rows):
            raise ScramblerHistoryCorrupt("scrambler_replay_already_released")
        existing = _draws_from_job(
            [row for row in rows if row.get("state") != RELEASED], job_id)
        if existing:
            # Replay FIRST, before any configuration is even read: the stored
            # rows are the operation's truth, and no intervening config or
            # date change may break or alter the promised replay. The
            # immutable arguments are the operation identity itself (page,
            # job, quantity).
            if len(existing) != quantity or any(draw is None for draw in existing):
                raise ScramblerHistoryCorrupt(
                    f"scrambler_replay_mismatch: job {job_id} holds "
                    f"{len(existing)} live reservations for {quantity} requested cuts"
                )
            return existing
        # Configuration is parsed only for NEW draws, and the draw timestamp
        # is validated BEFORE anything is committed: a caller-supplied invalid
        # ``now`` must refuse, never persist a row its own loader rejects.
        if now is not None and _iso_epoch(now) == 0.0:
            raise ValueError("scrambler_now_invalid")
        drawn_at = now or datetime.now(timezone.utc).isoformat()
        fast_share = fast_share_for(page_id)
        weights = treatment_weights()
        counters = history.setdefault("counters", {}).setdefault(
            page_id, {"cuts": 0, "reverses": 0})
        # DURABLE cap support: the denominator counts only RENDERED cuts
        # (releases cannot retroactively strand an over-cap rendered reverse);
        # the numerator counts every LIVE reverse (pending occupies it).
        page_cuts = sum(1 for row in rows if row.get("state") == RENDERED)
        page_reverses = sum(
            1 for row in rows
            if row.get("state") != RELEASED and row.get("treatment") == "reverse"
        )
        live_cuts = int(counters.get("cuts", 0))
        live_reverses = int(counters.get("reverses", 0))
        draws: list[ScramblerDraw] = []
        for cut_index in range(quantity):
            draw = _draw_strict(
                page_id, job_id, cut_index, rows,
                grade_allowed=grade_allowed, fast_share=fast_share,
                weights=weights, now=drawn_at,
                page_cuts=page_cuts, page_reverses=page_reverses,
            )
            rows.append(_record_row(
                page_id, job_id, cut_index, draw, drawn_at=drawn_at,
                et_day_value=et_day(drawn_at), fast_share=fast_share, weights=weights,
            ))
            draws.append(draw)
            live_cuts += 1
            if draw.treatment == "reverse":
                live_reverses += 1
                page_reverses += 1  # a pending reverse occupies the numerator
        counters["cuts"] = live_cuts
        counters["reverses"] = live_reverses
        history["pages"][page_id] = _prune_durable_rows(rows)
        _save_history_at(path, history)
    return draws


def _prune_durable_rows(rows: list[dict]) -> list[dict]:
    """Retain immutable identities; refuse capacity rather than lose history."""
    if len(rows) > SCRAMBLER_HISTORY_HORIZON:
        raise ScramblerHistoryCorrupt("scrambler_history_page_rows_exceeded")
    return rows


def enrich_job_reservations(
    page_id: str,
    job_id: str,
    reservations: list[dict],
    *,
    path: Path | None = None,
) -> None:
    """Record each cut's planned master/origin window on its reserved row.

    Called after the planner bound the drawn spans to concrete windows and
    before the job is committed, so a row always proves WHICH origin span its
    reservation covers. ``reservations[i]`` carries ``cutIndex``,
    ``masterSha256``, ``masterSourceId``, ``libraryStartMs``, ``originStartMs``
    and ``originEndMs`` for cut i. The supplied cut indices must be EXACTLY
    0..len(reservations)-1 and every one of them must have a reserved row: a
    partial enrichment is a named failure, never a silent success that leaves
    cuts without origin truth.
    """
    path = path or scrambler_history_path()
    for position, item in enumerate(reservations):
        if not isinstance(item, dict):
            raise ScramblerHistoryCorrupt(
                f"scrambler_enrich_item_invalid: reservation {position}")
        if item.get("cutIndex") != position:
            raise ScramblerHistoryCorrupt(
                "scrambler_enrich_item_invalid: reservations must list cuts "
                f"0..{len(reservations) - 1} in order; position {position} "
                f"carries cutIndex {item.get('cutIndex')!r}")
        if not isinstance(item.get("masterSha256"), str) or not item["masterSha256"]:
            raise ScramblerHistoryCorrupt(
                f"scrambler_enrich_item_invalid: reservation {position} masterSha256")
        if "originKind" in item or "originId" in item:
            if any(not isinstance(item.get(key), str) or not item[key].strip()
                   for key in ("originKind", "originId")):
                raise ScramblerHistoryCorrupt("scrambler_origin_identity_invalid")
        for field in ("libraryStartMs", "originStartMs", "originEndMs"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScramblerHistoryCorrupt(
                    f"scrambler_enrich_item_invalid: reservation {position} {field}")
    with store_lock(path):
        history = _load_history_at(path)
        rows = _page_rows(history, page_id)
        by_index = {}
        for row in rows:
            if row.get("jobId") == job_id:
                by_index[row["cutIndex"]] = row
        # All-or-nothing: the supplied items must cover EXACTLY the job's
        # reserved rows. A partial enrichment (2-cut job, 1 item) would leave
        # a cut with null origin truth - a named refusal, never a quiet gap.
        job_indices = set(by_index)
        supplied = set(range(len(reservations)))
        missing = sorted(job_indices - supplied)
        if missing:
            raise ScramblerHistoryCorrupt(
                f"scrambler_enrich_missing_rows: job {job_id} lacks enrichment "
                f"for its reserved cuts {missing}"
            )
        unknown = sorted(supplied - job_indices)
        if unknown:
            raise ScramblerHistoryCorrupt(
                f"scrambler_enrich_missing_rows: job {job_id} has no reserved "
                f"rows for the supplied cuts {unknown}"
            )
        for item in reservations:
            index = item["cutIndex"]
            row = by_index[index]
            if row.get("state") != RESERVED:
                raise ScramblerHistoryCorrupt("scrambler_enrich_not_reserved")
            if item["originEndMs"] <= item["originStartMs"]:
                raise ScramblerHistoryCorrupt("scrambler_enrich_origin_window_invalid")
            fields = ("masterSha256", "masterSourceId", "libraryStartMs", "originStartMs", "originEndMs", "originKind", "originId")
            if row.get("masterSha256") is not None and any(row.get(key) != item.get(key) for key in fields):
                raise ScramblerHistoryCorrupt("scrambler_enrich_binding_changed")
            row["masterSha256"] = item["masterSha256"]
            row["masterSourceId"] = item.get("masterSourceId")
            row["libraryStartMs"] = item["libraryStartMs"]
            row["originStartMs"] = item["originStartMs"]
            row["originEndMs"] = item["originEndMs"]
            if "originKind" in item:
                row["originKind"] = item["originKind"]
                row["originId"] = item["originId"]
            draw = _draw_from_row(row)
            row["usage"] = _usage_row(
                page_id, draw, origin_id=item.get("masterSourceId"),
                origin_start_ms=item["originStartMs"],
            )
        _save_history_at(path, history)


def _assert_render_sequence(rows, row):
    rendered = [other for other in rows if other.get("state") == RENDERED]
    if rendered:
        last = max(rendered, key=lambda other: (
            other.get("renderOrder", 0), _iso_epoch(other["renderedAt"])))
        if row["lengthMs"] == last["lengthMs"]:
            raise ScramblerExhausted("scrambler_render_consecutive_length")
        if row["treatment"] == last["treatment"]:
            raise ScramblerExhausted("scrambler_render_consecutive_treatment")
    return rendered


def _assert_reverse_render_day(rows, row, moment, page_id):
    """The actual ET render day is checked before media AND at commit."""
    if row.get("treatment") != "reverse":
        return
    render_day = et_day(moment)
    for other in rows:
        if (other is row or other.get("treatment") != "reverse"
                or other.get("state") != RENDERED):
            continue
        if et_day(other.get("renderedAt") or "") == render_day:
            raise ScramblerExhausted(
                "scrambler_reverse_render_day_exhausted: page "
                f"{page_id} already rendered a reverse on ET day {render_day}")


def assert_cut_renderable(page_id: str, job_id: str, cut_index: int, *,
                          rendered_at: str | None = None, path: Path | None = None) -> None:
    """Check a reserved cut BEFORE media work, without mutating its draw.

    The actual rendered-state commit rechecks under the same durable lock.
    The caller serializes same-page executions across preflight and encoding.
    """
    if rendered_at is not None and _iso_epoch(rendered_at) == 0.0:
        raise ScramblerHistoryCorrupt("scrambler_render_renderedAt_invalid")
    path = path or scrambler_history_path()
    with store_lock(path):
        rows = _page_rows(_load_history_at(path), page_id)
        moment = rendered_at or datetime.now(timezone.utc).isoformat()
        for row in rows:
            if row.get("jobId") == job_id and row.get("cutIndex") == cut_index:
                if row.get("state") != RESERVED:
                    raise ScramblerHistoryCorrupt("scrambler_render_not_reserved")
                _assert_render_sequence(rows, row)
                _assert_reverse_render_day(rows, row, moment, page_id)
                return
        raise ScramblerHistoryCorrupt("scrambler_render_unknown_row")


def mark_cut_rendered(
    page_id: str,
    job_id: str,
    cut_index: int,
    *,
    asset_sha256: str,
    rendered_at: str | None = None,
    path: Path | None = None,
) -> None:
    """Flip one reservation to ``rendered`` once the finished clip exists.

    Inputs are validated BEFORE anything is committed (a public mutation must
    never write a row its own loader rejects), and an already-rendered cut is
    idempotent only for the same asset bytes: a second, different asset for
    one reservation is a named corruption, not a silent overwrite. A failed
    encode never reaches this; the release path removes the job's unrendered
    reservations instead.
    """
    if not isinstance(asset_sha256, str) or not asset_sha256.strip():
        raise ScramblerHistoryCorrupt("scrambler_render_asset_sha256_invalid")
    if rendered_at is not None and _iso_epoch(rendered_at) == 0.0:
        raise ScramblerHistoryCorrupt("scrambler_render_renderedAt_invalid")
    path = path or scrambler_history_path()
    moment = rendered_at or datetime.now(timezone.utc).isoformat()
    with store_lock(path):
        history = _load_history_at(path)
        rows = _page_rows(history, page_id)
        for row in rows:
            if row.get("jobId") == job_id and row.get("cutIndex") == cut_index:
                if row.get("state") == RELEASED:
                    # A released reservation cannot come back as rendered: the
                    # job's failure path already tombstoned it.
                    raise ScramblerHistoryCorrupt(
                        f"scrambler_render_already_released: job {job_id} cut "
                        f"{cut_index} was released before this render"
                    )
                if row.get("state") == RENDERED and row.get("assetSha256") != asset_sha256:
                    raise ScramblerHistoryCorrupt(
                        f"scrambler_render_already_rendered: job {job_id} cut "
                        f"{cut_index} is rendered with a different asset"
                    )
                if row.get("state") == RENDERED:
                    # Same bytes replay the original event; do not move its
                    # render day, report clock or any copied posting evidence.
                    return
                rendered = _assert_render_sequence(rows, row)
                # Recheck after media work: ET midnight or another actual
                # commit can invalidate the preflight day before this point.
                _assert_reverse_render_day(rows, row, moment, page_id)
                row["state"] = RENDERED
                row["renderOrder"] = max((other.get("renderOrder", 0) for other in rendered), default=0) + 1
                row["renderedAt"] = moment
                row["assetSha256"] = asset_sha256
                if isinstance(row.get("usage"), dict):
                    row["usage"]["asset_sha"] = asset_sha256
                _save_history_at(path, history)
                return
        raise ScramblerHistoryCorrupt(
            f"scrambler_render_unknown_row: no reservation for {job_id} cut {cut_index}"
        )


def release_job_reservations(
    page_id: str,
    job_id: str,
    *,
    reason: str,
    path: Path | None = None,
    cut_index: int | None = None,
) -> None:
    """Release a job's UNRENDERED reservations (refused plan, failed job).

    With ``cut_index`` only that one cut's reservation is released (a cut the
    executor refused and skipped); the job's other cuts are untouched.

    RENDERED rows stay committed: a finished cut's combo remains consumed,
    it still counts in the daily report, and its reverse still holds the ET
    day cap - a later failure of the same job can never unlock footage that
    already rendered, and a retried operation can never redraw it. Only the
    still-reserved rows go, returning their combos to the pool; the lifetime
    counters step back with them, so the cumulative reverse cap tracks the
    page's standing commitments. The job's source-window reservations release
    through their own existing rules. Named, best-effort on a lost ledger (the
    draw already failed closed upstream).
    """
    path = path or scrambler_history_path()
    try:
        with store_lock(path):
            history = _load_history_at(path)
            rows = _page_rows(history, page_id)
            released_at = datetime.now(timezone.utc).isoformat()
            removed = removed_reverses = 0
            for row in rows:
                if (row.get("jobId") == job_id and row.get("state") == RESERVED
                        and (cut_index is None or row.get("cutIndex") == cut_index)):
                    # TOMBSTONE, never a deletion: the row keeps its immutable
                    # identity (cutIndex/slotId/combo) so the page's next read
                    # cannot trip the duplicate/noncontiguity guards, but it
                    # stops holding a combo, an ET-day reverse or counter
                    # credit. RENDERED rows stay fully committed.
                    row["state"] = RELEASED
                    row["releasedAt"] = released_at
                    row["releaseReason"] = str(reason)[:200]
                    removed += 1
                    if row.get("treatment") == "reverse":
                        removed_reverses += 1
            if removed:
                counter = history.setdefault("counters", {}).setdefault(
                    page_id, {"cuts": 0, "reverses": 0})
                counter["cuts"] = max(0, int(counter.get("cuts", 0)) - removed)
                counter["reverses"] = max(0, int(counter.get("reverses", 0)) - removed_reverses)
                history["pages"][page_id] = _prune_durable_rows(rows)
                _save_history_at(path, history)
            _log_once(
                f"released:{job_id}" + ("" if cut_index is None else f":{cut_index}"),
                "released %d unrendered scrambler reservations of job %s (%s); "
                "rendered cuts stay committed",
                removed, job_id, reason, level=logging.INFO,
            )
    except (ScramblerHistoryCorrupt, ScramblerHistoryLost, OSError, ValueError) as error:
        _log_once(f"release-failed:{job_id}",
                  "could not release the scrambler reservations of job %s (%s)",
                  job_id, error)


# A RENDERED cut whose job ended without delivering it (the job failed, or
# the job store no longer knows it) can never be posted, yet its origin window
# stays held until posting evidence arrives - which never will. After this
# grace period (time for an operator recovery) the hold is released: the row
# stays RENDERED (its combo, render order and reverse credit are unchanged, so
# the variety and reverse-cap proofs are untouched) and only stops holding its
# origin window (services/control_plane_sources.scrambler_pending_windows).
UNDELIVERED_HOLD_TIMEOUT_S = 24 * 3600.0
UNDELIVERED_HOLD_REASON = "rendered_never_delivered_timeout"


def release_undelivered_holds(
    job_status: Callable[[str], str | None],
    *,
    now: str | None = None,
    older_than_s: float | None = None,
    path: Path | None = None,
) -> int:
    """Release the origin-window hold of rendered-but-never-delivered cuts.

    ``job_status(job_id)`` returns the job's stored status, or None when the
    job store does not know it. Only a terminal ``failed`` job counts as never
    delivered. Every other status keeps the hold, and so does an unknown job:
    job-store compaction archives COMPLETED jobs after 30 days (its first pass
    runs on every Lab start), so "unknown" can be a delivered cut that has not
    posted yet and must keep its origin window (fail closed). Rows that carry
    posting evidence are never touched. Returns the number of holds released.
    """
    older_than_s = UNDELIVERED_HOLD_TIMEOUT_S if older_than_s is None else older_than_s
    moment = now or datetime.now(timezone.utc).isoformat()
    current = _iso_epoch(moment)
    if current == 0.0:
        raise ValueError("scrambler_now_invalid")
    path = path or scrambler_history_path()
    released = 0
    with store_lock(path):
        history = _load_history_at(path)
        for page_id, rows in history["pages"].items():
            for row in rows:
                if (row.get("state") != RENDERED or row.get("postingObservation") is not None
                        or row.get("holdRelease") is not None
                        or current - _iso_epoch(row["renderedAt"]) < older_than_s
                        or job_status(row["jobId"]) != "failed"):
                    continue
                row["holdRelease"] = {"at": moment, "reason": UNDELIVERED_HOLD_REASON}
                released += 1
                _log_once(f"hold-released:{row['jobId']}:{row['cutIndex']}",
                          "released the origin-window hold of rendered scrambler cut "
                          "%s#%s on page %s: its job never delivered it",
                          row["jobId"], row["cutIndex"], page_id, level=logging.INFO)
        if released:
            _save_history_at(path, history)
    return released


def scrambler_history_rows(page_id: str, *, path: Path | None = None) -> list[dict]:
    """The page's committed rows, including RELEASED tombstones (read-only).

    Tombstones stay readable so validation and replay keep
    their immutable operation identity; callers that need live commitments
    only filter `state != RELEASED` themselves.

    Takes the same cross-process lock as the writers: a first read on a fresh
    deployment BOOTSTRAPS the ledger (creating the marker and empty history),
    and that creation must never race a concurrent first draw.
    """
    path = path or scrambler_history_path()
    with store_lock(path):
        return _page_rows(_load_history_at(path), page_id)


def assert_existing_jobs(existing_job_ids: Iterable[str], *, path: Path | None = None) -> None:
    """Refuse unknown/lost job history BEFORE any bootstrap-capable reader.

    The router calls this under its jobs lock with IDs of persisted jobs that
    actually carry private Scrambler cuts (legacy jobs are excluded).
    """
    known_jobs = set()
    for known_id in existing_job_ids:
        if not isinstance(known_id, str) or not known_id:
            raise ScramblerHistoryCorrupt("scrambler_existing_job_id_invalid")
        known_jobs.add(known_id)
        if len(known_jobs) > SCRAMBLER_HISTORY_MAX_ROWS:
            raise ScramblerHistoryCorrupt("scrambler_existing_jobs_capacity_exceeded")
    if not known_jobs:
        return
    path = path or scrambler_history_path()
    with store_lock(path):
        if known_jobs and not path.exists():
            raise ScramblerHistoryLost("scrambler_history_existing_jobs_missing")
        history = _load_history_at(path)
        ledger_jobs = {row["jobId"] for rows in history["pages"].values() for row in rows}
        if known_jobs - ledger_jobs:
            raise ScramblerHistoryLost("scrambler_history_existing_jobs_missing")


def live_reservation_rows(*, path: Path | None = None) -> list[dict]:
    """Fresh private copies of ALL pages' reserved/rendered commitments."""
    path = path or scrambler_history_path()
    with store_lock(path):
        history = _load_history_at(path)
        return [dict(copy.deepcopy(row), pageId=page)
                for page, rows in history["pages"].items() for row in rows
                if row.get("state") in (RESERVED, RENDERED)]


def mark_posting_observed(page_id: str, job_id: str, cut_index: int, *,
                          asset_sha256: str, posted_at: str, page_handle: str,
                          path: Path | None = None) -> None:
    """Persist copied authoritative posting evidence, never render time."""
    _validate_iso(posted_at, field="posting_observation_posted_at")
    if not _posting_page_valid(page_handle):
        raise ScramblerHistoryCorrupt("scrambler_posting_page_invalid")
    path = path or scrambler_history_path()
    with store_lock(path):
        history = _load_history_at(path)
        for row in _page_rows(history, page_id):
            if row.get("jobId") == job_id and row.get("cutIndex") == cut_index:
                if (row.get("state") != RENDERED or not asset_sha256
                        or row.get("assetSha256") != asset_sha256):
                    raise ScramblerHistoryCorrupt("scrambler_posting_asset_mismatch")
                observation = {"assetSha256": asset_sha256, "posted_at": posted_at,
                               "page": page_handle}
                if row.get("postingObservation") not in (None, observation):
                    raise ScramblerHistoryCorrupt("scrambler_posting_observation_conflict")
                row["postingObservation"] = observation
                _save_history_at(path, history)
                return
        raise ScramblerHistoryCorrupt("scrambler_posting_unknown_row")


# ---------------------------------------------------------------------------
# Daily report (strategy section 7a / 9: distinct counts, repeats, alerts)
# ---------------------------------------------------------------------------

def scrambler_daily_report(rows: list[dict], *, now: str | None = None,
                           window_s: float = 86_400.0) -> dict:
    """Distinct speed/length/treatment/combo counts and repeats in 24 h.

    Only RENDERED cuts count: a reserved-but-unrendered draw is not a variety
    cut that happened, and an unposted draw never carries a faked
    ``posted_at``. A row whose ``renderedAt`` lies in the FUTURE is excluded:
    a corrupt future timestamp must not inflate today's counts. Target: 0
    repeats. ``zeroVarietyCuts`` flags a page with the Scrambler on and no
    rendered scrambler cut in the window.
    """
    now_ts = _iso_epoch(now) if now else datetime.now(timezone.utc).timestamp()
    accepted = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("state") != RENDERED:
            continue
        draw = _draw_from_row(row)
        if draw is None:
            continue
        rendered_ts = _iso_epoch(row.get("renderedAt") or row.get("drawnAt"))
        age = now_ts - rendered_ts
        if age < 0 or age > window_s:
            continue
        accepted.append((row.get("renderOrder") or 0, rendered_ts, index, draw))
    # Reservations can finish out of order or never render. Actual commit
    # order drives consecutive-repeat reporting; timestamps/index break ties
    # deterministically for compatible older rows without renderOrder.
    accepted.sort(key=lambda item: item[:3])
    cuts = [item[3] for item in accepted]
    combos = [draw.combo() for draw in cuts]
    counts: dict[tuple, int] = {}
    for combo in combos:
        counts[combo] = counts.get(combo, 0) + 1
    return {
        "cuts": len(cuts),
        "distinctSpeed": len({speed_bucket(draw.speed) for draw in cuts}),
        "distinctLength": len({draw.length_ms for draw in cuts}),
        "distinctTreatment": len({draw.treatment for draw in cuts}),
        "distinctCombo": len(counts),
        "fastCuts": sum(1 for draw in cuts if draw.is_fast()),
        "repeatCombos": sorted(list(combo) for combo, count in counts.items() if count > 1),
        "consecutiveLengthRepeats": sum(
            1 for a, b in zip(cuts, cuts[1:]) if a.length_ms == b.length_ms),
        "consecutiveTreatmentRepeats": sum(
            1 for a, b in zip(cuts, cuts[1:]) if a.treatment == b.treatment),
        "reverseCuts": sum(1 for draw in cuts if draw.treatment == "reverse"),
        "zeroVarietyCuts": not cuts,
    }


# ---------------------------------------------------------------------------
# Split-swap (LOOP9-style back half first) on a finished clip
# ---------------------------------------------------------------------------

FFMPEG_TIMEOUT_S = 600


def split_swap_filter(duration_s: float) -> str:
    """The filtergraph that plays the second half of a clip before its first
    (video only)."""
    half = duration_s / 2.0
    return (
        f"[0:v]trim=0:{half:.6f},setpts=PTS-STARTPTS[v1];"
        f"[0:v]trim={half:.6f},setpts=PTS-STARTPTS[v2];"
        f"[v2][v1]concat=n=2:v=1:a=0[v]"
    )


def split_swap_filter_with_audio(duration_s: float) -> str:
    """Split-swap with audio swapped in lockstep, re-encoded in one pass so the
    join carries no per-piece audio priming gap."""
    half = duration_s / 2.0
    return (
        f"[0:v]trim=0:{half:.6f},setpts=PTS-STARTPTS[v1];"
        f"[0:v]trim={half:.6f},setpts=PTS-STARTPTS[v2];"
        f"[0:a]atrim=0:{half:.6f},asetpts=PTS-STARTPTS[a1];"
        f"[0:a]atrim={half:.6f},asetpts=PTS-STARTPTS[a2];"
        f"[v2][v1]concat=n=2:v=1:a=0[v];"
        f"[a2][a1]concat=n=2:v=0:a=1[a]"
    )


def _probe_duration_audio(path: Path) -> tuple[float, bool]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    data = json.loads(completed.stdout)
    has_audio = any(stream.get("codec_type") == "audio" for stream in data.get("streams", []))
    return float(data["format"]["duration"]), has_audio


def apply_split_swap(path: Path, encode_args: list[str]) -> None:
    """Rewrite a finished cut in place with its halves swapped.

    Raises if probing or encoding fails. Runs under the caller's encode gate
    like every other full 1080x1920 re-encode.
    """
    path = Path(path)
    duration_s, has_audio = _probe_duration_audio(path)
    work = path.parent / f".{path.stem}-splitswap"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    swapped = work / "swapped.mp4"
    graph = (split_swap_filter_with_audio(duration_s) if has_audio
             else split_swap_filter(duration_s))
    maps = ["-map", "[v]"] + (["-map", "[a]"] if has_audio else ["-an"])
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(path),
             "-filter_complex", graph, *maps, *encode_args,
             "-movflags", "+faststart", str(swapped)],
            check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_S,
        )
        swapped.replace(path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


__all__ = [
    "ET", "FAST_SPEEDS", "GRADE_BRIGHTNESS_CEILING", "GRADE_DELTA_MAX",
    "GRADE_DELTA_MIN", "LENGTHS", "LENGTH_MAX_MS", "LENGTH_MIN_MS", "OFFSETS",
    "OFFSET_MAX_MS", "OFFSET_STEP_MS", "REVERSE_CAP_DENOMINATOR", "REVERSE_GATE",
    "SCRAMBLER_CUT_SCHEMA", "SCRAMBLER_HISTORY_HORIZON", "SCRAMBLER_HISTORY_SCHEMA",
    "SCRAMBLER_MAX_OUTPUT_S", "SCRAMBLER_MIN_OUTPUT_S", "SLOW_SPEEDS", "SPEEDS",
    "TREATMENTS", "ScramblerDraw", "ScramblerExhausted", "ScramblerHistoryCorrupt",
    "ScramblerHistoryLost", "apply_split_swap", "enrich_job_reservations",
    "et_day", "fast_share_for", "grade_allowed_for_look", "mark_cut_rendered",
    "release_job_reservations", "reserve_job_cuts", "scrambler_daily_report",
    "scrambler_enabled", "scrambler_gate", "scrambler_history_path",
    "scrambler_history_rows", "speed_bucket", "split_swap_filter",
    "split_swap_filter_with_audio", "treatment_weights",
]
