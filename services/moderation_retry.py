"""Bounded, class-aware retry of a confirmed provider moderation refusal.

A confirmed refusal (Replicate "failed" with a prediction id, classified
``moderation``) whose code is exactly E005 may be retried at most
``RETRIES_PER_CALL`` times with a deterministic prompt rewording. Any other
moderation-class text stays a terminal refusal, and every other failure class
keeps its existing fail-fast behaviour: retrying credit, auth, quota or unknown
faults spends money for nothing.

No alternate engine is used: swapping the engine changes the recipe's visual
contract, catalog version and cost, so the explicit allowlist stays empty.
The provider adapters expose no seed input, so none is invented here.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("control_plane")

RETRIES_PER_CALL = 2  # at most three paid attempts per requested clip
ALTERNATE_ENGINE_ALLOWLIST: tuple[str, ...] = ()
DAILY_BUDGET_ENV = "CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET"
DEFAULT_DAILY_BUDGET = 6

RETRIES_EXHAUSTED = "moderation_retries_exhausted"
BUDGET_EXHAUSTED = "moderation_retry_budget_exhausted"
AUTH_NOT_RETRIED = "moderation_provider_auth_not_retried"
NOT_E005_NOT_RETRIED = "moderation_not_e005_not_retried"

# Ordered, fixed rewordings. Variant k (attempt k >= 1) of a call is a pure
# function of the base prompt and k. A variant never introduces a subject the
# prompt did not already have: the neutral suffix applies to every prompt, and
# the person-specific wording (replacements and clause) applies only when the
# base prompt depicts people. The observed silhouette E005 trigger was the
# "featureless" / "no clothing detail" wording of embracing bodies.
#
# "Depicts people" is decided from the prompt text itself, because the
# catalog has no structured subject flag: a family's free-text fixed_subject
# carries its own negations ("no visible occupants"), and the people-free
# families state "no people" in guards or motion that the prompt appends. So a
# person term counts only when no negation precedes it within the same clause.
# Ambiguous nouns are excluded: "figure(s)" (figure-eight), "body/bodies"
# (body of water, truck body); a hyphenated compound ("man-made") never counts.
_PERSON_TERMS = re.compile(
    r"\b(?:people|persons?|man|men|woman|women|adults?|couples?|lovers?"
    r"|humans?|child(?:ren)?|girls?|boys?|dancers?|someone|everyone)\b(?!-)",
    re.I,
)
_NEGATION = re.compile(r"\b(?:no|without|zero|not\s+any|free\s+of|devoid\s+of|never)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[,.;:!?()]|\b(?:and|but|or|while|with)\b", re.I)
_NEGATION_WINDOW_WORDS = 3


def depicts_people(prompt: str) -> bool:
    """True when some person term in ``prompt`` is not negated."""
    for match in _PERSON_TERMS.finditer(prompt):
        clause = _CLAUSE_BREAK.split(prompt[:match.start()])[-1]
        window = " ".join(clause.split()[-_NEGATION_WINDOW_WORDS:])
        if not _NEGATION.search(window):
            return True
    return False


# (variant id, person-only replacements, neutral suffix, person-only clause)
PROMPT_VARIANTS: tuple[tuple[str, tuple[tuple[str, str], ...], str, str], ...] = (
    (
        "family-safe.v2",
        (),
        "Family-friendly, tasteful and non-explicit.",
        "Everyone shown is fully clothed.",
    ),
    (
        "backlit-shapes.v2",
        (
            ("pure black featureless full-body silhouettes",
             "solid black backlit silhouette shapes of fully clothed adults"),
            ("no clothing detail", "clothing shown only as solid black shape"),
            ("featureless", "solid black backlit"),
        ),
        "Calm, understated scene with no graphic detail; family-friendly and non-explicit.",
        "Render every person as a solid black backlit silhouette shape of a fully "
        "clothed adult with no body detail.",
    ),
)

# A moderation-class message that is really an authentication failure inside
# the provider's own moderation check (observed 2026-09-25: "Moderation check
# failed: Error code: 401 ... account ... deactivated") would fail again. The
# classifier now names that shape provider_auth; this stays as a second guard.
_AUTH_IN_MODERATION = re.compile(r"error code:\s*40[13]\b|deactivated", re.I)

# The ONLY retry trigger: Replicate's own content refusal code, E005 ("The
# input or output was flagged as sensitive. (E005)"; 23/23 confirmed refusals
# in the 2026-09-25 census). The broad moderation class also matches any
# "moderation|safety|nsfw|flagged" text, e.g. a 429/5xx from the provider's
# moderation dependency or a failed prediction's logs echoing a safety
# setting; retrying those spends money for nothing. A message that carries an
# embedded HTTP status is a dependency failure even if it also names E005:
# "Error code: 503" / "Error code 429" (OpenAI client), JSON or Python-repr
# "status": 500, status_code=429, "HTTP 503" / "HTTP/1.1 503", and httpx's
# "Server error '503 Service Unavailable'".
_E005 = re.compile(r"\((?:code:\s*)?E005\)")
_EMBEDDED_HTTP_STATUS = re.compile(
    r"\berror[ _]code:?\s*\d{3}\b"
    r"|['\"]?\bstatus(?:[ _]?code)?['\"]?\s*[:=]\s*\d{3}\b"
    r"|\bHTTP(?:/\d(?:\.\d)?)?\s+\d{3}\b"
    r"|\b(?:client|server) error '\d{3}\b",
    re.I,
)

# Flat per-attempt cost estimates in USD, keyed by Replicate model. A refused
# prediction is billed like a successful one, so a refused attempt records the
# same estimate. Figures are the repository's own catalog estimates (not
# re-verified against live Replicate pricing); an unlisted model records null.
ATTEMPT_COST_ESTIMATE_USD: dict[str, float] = {
    # recipes/generation/silhouette_stills.v1.json providers.flux-image
    # cost_per_gen_usd (one 2 MP FLUX.2 Pro still; catalog estimate).
    "black-forest-labs/flux-2-pro": 0.03,
    # recipes/generation/prompt_modules.v1.json providers.hailuo
    # cost_per_gen_usd, "lab-observed" 2026-08-16 (1080p/6s video).
    "minimax/hailuo-2.3": 0.28,
    # recipes/generation/prompt_modules.v1.json providers.wan-i2v-fast
    # cost_per_gen_usd, marked "best-known default", unverified.
    "wan-video/wan-2.2-i2v-fast": 0.12,
}


def variant_prompt(base_prompt: str, attempt: int) -> tuple[str, str | None]:
    """Return the prompt for ``attempt`` (0 is the plan's) and its variant id."""
    if type(attempt) is not int or not 0 <= attempt <= len(PROMPT_VARIANTS):
        raise ValueError("moderation_retry_attempt_invalid")
    if attempt == 0:
        return base_prompt, None
    variant_id, replacements, suffix, person_clause = PROMPT_VARIANTS[attempt - 1]
    prompt = base_prompt
    if depicts_people(base_prompt):
        for old, new in replacements:
            prompt = prompt.replace(old, new)
        suffix = f"{suffix} {person_clause}"
    prompt = prompt.rstrip()
    separator = " " if prompt.endswith((".", "!", "?")) else ". "
    return prompt + separator + suffix, variant_id


def attempt_cost_usd(model: str) -> float | None:
    return ATTEMPT_COST_ESTIMATE_USD.get(model)


def daily_budget() -> int:
    """Retries (never first attempts) allowed per page per UTC day."""
    raw = os.environ.get(DAILY_BUDGET_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_BUDGET
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        # Spend-safe: a malformed budget buys no retries.
        log.warning("%s=%r is not an integer >= 0; moderation retries disabled",
                    DAILY_BUDGET_ENV, raw)
        return 0
    return value


def confirmed_refusal(error_class: str, request_id: Any, detail: str) -> bool:
    """The same confirmed-refusal condition the planned-candidate skip uses."""
    return (error_class == "moderation"
            and isinstance(request_id, str) and bool(request_id.strip())
            and detail.startswith("Replicate failed:"))


def retry_blocked(detail: str) -> str | None:
    """Why a confirmed refusal must NOT be retried, or None when it may be.

    Only an exact E005 refusal is retried; everything else stays terminal.
    """
    if _AUTH_IN_MODERATION.search(detail):
        return AUTH_NOT_RETRIED
    if not _E005.search(detail) or _EMBEDDED_HTTP_STATUS.search(detail):
        return NOT_E005_NOT_RETRIED
    return None


def retries_used(store: dict[str, Any], page_id: str, utc_day: str) -> int:
    """Count reserved retries for one page on one UTC day across all jobs."""
    used = 0
    for job in store.get("jobs", {}).values():
        if not isinstance(job, dict) or job.get("pageId") != page_id:
            continue
        attempts = job.get("generationAttempts")
        if not isinstance(attempts, dict):
            continue
        for rows in attempts.values():
            for row in rows if isinstance(rows, list) else []:
                if (isinstance(row, dict) and type(row.get("attempt")) is int
                        and row["attempt"] >= 1
                        and str(row.get("at", ""))[:10] == utc_day):
                    used += 1
    return used


def retry_cost_total(attempts: dict[str, list[dict[str, Any]]]) -> float:
    total = 0.0
    for rows in attempts.values():
        for row in rows:
            cost = row.get("costUsd")
            if row.get("attempt", 0) >= 1 and isinstance(cost, (int, float)):
                total += cost
    return round(total, 4)
