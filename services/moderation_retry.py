"""Bounded, class-aware retry of a confirmed provider moderation refusal.

A confirmed refusal (Replicate "failed" with a prediction id, classified
``moderation``, e.g. E005) may be retried at most ``RETRIES_PER_CALL`` times
with a deterministic prompt rewording. Every other failure class keeps its
existing fail-fast behaviour: retrying credit, auth, quota or unknown faults
spends money for nothing.

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

# Ordered, fixed rewordings. Variant k (attempt k >= 1) of a call is a pure
# function of the base prompt and k. Replacements apply only where the phrase
# occurs; the observed silhouette E005 trigger was the "featureless" / "no
# clothing detail" wording of unclothed-looking embracing bodies.
PROMPT_VARIANTS: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    (
        "clothed-family-safe.v1",
        (),
        "Fully clothed figures, family-friendly, non-explicit.",
    ),
    (
        "backlit-silhouette-shapes.v1",
        (
            ("pure black featureless full-body silhouettes",
             "solid black backlit silhouette shapes of fully clothed adults"),
            ("no clothing detail", "clothing shown only as solid black shape"),
            ("featureless", "solid black backlit"),
        ),
        "Render every person as a solid black backlit silhouette shape of a fully "
        "clothed adult with no body detail; family-friendly and non-explicit.",
    ),
)

# A moderation-class message that is really an authentication failure inside
# the provider's own moderation check (observed 2026-09-25: "Moderation check
# failed: Error code: 401 ... account ... deactivated") would fail again.
_AUTH_IN_MODERATION = re.compile(r"error code:\s*40[13]\b|deactivated", re.I)

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
    variant_id, replacements, suffix = PROMPT_VARIANTS[attempt - 1]
    prompt = base_prompt
    for old, new in replacements:
        prompt = prompt.replace(old, new)
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


def retryable_refusal(detail: str) -> bool:
    return not _AUTH_IN_MODERATION.search(detail)


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
