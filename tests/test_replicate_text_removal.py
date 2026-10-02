"""Tests for providers.replicate.remove_text."""

import asyncio

import pytest

from providers.replicate import (
    _build_flux_2_pro_input,
    _build_wan_i2v_fast_input,
    remove_text,
)


def test_remove_text_rejects_empty_image():
    """remove_text raises ValueError when given an empty data URI."""
    with pytest.raises(ValueError, match="image"):
        asyncio.run(remove_text("", None))


def test_remove_text_rejects_none_image():
    """remove_text raises ValueError when given None."""
    with pytest.raises(ValueError, match="image"):
        asyncio.run(remove_text(None, None))  # type: ignore[arg-type]


def test_remove_text_meters_the_lama_submission(monkeypatch):
    """P1: the recreate text-removal LaMa submission is reserved against the
    daily meter, idempotently keyed by image content, before the prediction."""
    import hashlib
    from providers import replicate
    from services import generation_budget

    monkeypatch.setitem(replicate.API_KEYS, "replicate", "test-tok")
    debits = []

    def fake_debit(path, amount, debit, now=None):
        debits.append((amount, debit))
        return True

    monkeypatch.setattr(generation_budget, "debit_generation_spend_at", fake_debit)
    monkeypatch.setattr(generation_budget, "per_gen_cost_usd_by_model",
                        lambda m: 0.02)
    monkeypatch.setattr(replicate, "_generate_text_mask",
                        lambda uri: "data:image/png;base64,mask")

    class _FakeClient:
        async def post(self, *a, **k):
            raise RuntimeError("post-done")

        async def aclose(self):
            pass

    with pytest.raises(RuntimeError, match="post-done"):
        asyncio.run(remove_text("data:image/png;base64,image", _FakeClient()))
    assert len(debits) == 1
    amount, debit = debits[0]
    assert amount == 0.02
    assert debit == "recreate:" + hashlib.sha256(b"data:image/png;base64,image").hexdigest()[:32]


def test_remove_text_refuses_when_budget_exhausted(monkeypatch):
    """P1: a LaMa inpainting that cannot reserve spend must not submit."""
    from providers import replicate
    from services import generation_budget

    monkeypatch.setitem(replicate.API_KEYS, "replicate", "test-tok")
    monkeypatch.setattr(replicate, "_generate_text_mask",
                        lambda uri: "data:image/png;base64,mask")
    monkeypatch.setattr(generation_budget, "debit_generation_spend_at",
                        lambda *a, **k: False)
    monkeypatch.setattr(generation_budget, "per_gen_cost_usd_by_model",
                        lambda m: 0.02)
    with pytest.raises(RuntimeError, match="generation_daily_budget_reached"):
        asyncio.run(remove_text("data:image/png;base64,image", None))


def test_wan_i2v_fast_includes_negative_prompt_when_supplied():
    payload = _build_wan_i2v_fast_input(
        "Locked-off parked truck shot.",
        {
            "image_data_uri": "data:image/png;base64,abc",
            "negative_prompt": "driving, tire rotation, camera pan",
        },
    )

    assert payload["negative_prompt"] == "driving, tire rotation, camera pan"


def test_flux_2_pro_builds_the_locked_portrait_still_request():
    payload = _build_flux_2_pro_input(
        "Two lovers embrace beside a pickup in a field.",
        {
            "aspect_ratio": "9:16",
            "image_resolution": "2 MP",
            "output_format": "jpg",
            "output_quality": 95,
        },
    )

    assert payload == {
        "prompt": "Two lovers embrace beside a pickup in a field.",
        "aspect_ratio": "9:16",
        "resolution": "2 MP",
        "output_format": "jpg",
        "output_quality": 95,
    }


def test_flux_2_pro_rejects_non_portrait_requests():
    with pytest.raises(ValueError, match="9:16"):
        _build_flux_2_pro_input("scene", {"aspect_ratio": "16:9"})


def test_flux_2_pro_sends_an_explicit_safety_tolerance_only_when_opted_in():
    params = {"aspect_ratio": "9:16", "image_resolution": "2 MP"}

    assert "safety_tolerance" not in _build_flux_2_pro_input("scene", params)
    assert _build_flux_2_pro_input(
        "scene", {**params, "safety_tolerance": 3},
    )["safety_tolerance"] == 3


@pytest.mark.parametrize("value", [0, 6, -1, "3", 3.0, True, False])
def test_flux_2_pro_rejects_safety_tolerance_outside_the_provider_range(value):
    with pytest.raises(ValueError, match="safety_tolerance"):
        _build_flux_2_pro_input(
            "scene", {"aspect_ratio": "9:16", "safety_tolerance": value},
        )
