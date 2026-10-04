"""P2: pricing is pinned to the provider catalog and request-dependent.

Grok is catalog-priced at ~$5/10 s, so a 15 s request must meter at $7.50
rather than the flat $5 that under-charges it. Wan/Pruna are per-second, and an
unknown or unpriced provider must fail closed (never meter as free). Red on
fce90bc, where grok carries a flat ``cost_per_gen_usd: 5.0`` and a missing cost
falls back to a guessed ``DEFAULT_COST_PER_GEN_USD``.
"""

import pytest

from services import generation_budget


def test_grok_priced_by_requested_duration():
    assert generation_budget.per_gen_cost_usd("grok", 10) == pytest.approx(5.0)
    assert generation_budget.per_gen_cost_usd("grok", 5) == pytest.approx(2.50)
    # The allowed 15 s request must not be under-charged as a 10 s one.
    assert generation_budget.per_gen_cost_usd("grok", 15) == pytest.approx(7.50)


def test_wan_priced_by_requested_duration():
    assert generation_budget.per_gen_cost_usd("wan-t2v", 10) == pytest.approx(0.60)
    assert generation_budget.per_gen_cost_usd("wan-i2v-fast", 15) == pytest.approx(0.90)


def test_pruna_priced_by_requested_duration():
    assert generation_budget.per_gen_cost_usd("pruna-pvideo", 15) == pytest.approx(0.30)


@pytest.mark.parametrize(
    ("provider_id", "resolution", "draft", "cost"),
    [
        ("pruna-pvideo", "720p", False, 0.12),
        ("pruna-pvideo", "720p", True, 0.03),
        ("pruna-pvideo", "1080p", False, 0.24),
        ("pruna-pvideo", "1080p", True, 0.06),
        ("pruna-pvideo-vertical", "720p", False, 0.12),
        ("pruna-pvideo-vertical", "1080p", False, 0.24),
    ],
)
def test_pvideo_cost_matches_resolution_and_draft_input(
    provider_id, resolution, draft, cost,
):
    from providers.replicate import _build_pvideo_input

    payload = _build_pvideo_input(
        "prompt", {"duration": 6, "resolution": resolution, "draft": draft},
    )
    assert payload["resolution"] == resolution
    assert payload["draft"] is draft
    assert generation_budget.per_gen_cost_usd(
        provider_id, 6, resolution=payload["resolution"], draft=payload["draft"],
    ) == pytest.approx(cost)


def test_pvideo_rejects_unpriced_resolution_or_invalid_draft_mode():
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("pruna-pvideo", 6, resolution="4k")
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("pruna-pvideo", 6, resolution="720p", draft="true")


def test_pvideo_model_id_retry_cost_uses_the_same_resolution_and_draft_rate():
    from services.moderation_retry import attempt_cost_usd

    assert attempt_cost_usd(
        "prunaai/p-video", 6, resolution="1080p", draft=False,
    ) == pytest.approx(0.24)
    assert attempt_cost_usd(
        "prunaai/p-video", 6, resolution="1080p", draft=True,
    ) == pytest.approx(0.06)


def test_flat_cost_provider_ignores_duration():
    # hailuo is flat ~$0.28/video regardless of requested seconds.
    assert generation_budget.per_gen_cost_usd("hailuo", 6) == pytest.approx(0.28)
    assert generation_budget.per_gen_cost_usd("hailuo", 15) == pytest.approx(0.28)


def test_unknown_provider_fails_closed():
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("no-such-provider", 10)


def test_per_second_provider_requires_duration():
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", None)
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", 0)
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", float("nan"))
