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
