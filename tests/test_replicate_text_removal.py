"""Tests for providers.replicate.remove_text."""

import asyncio

import pytest

from providers.replicate import _build_wan_i2v_fast_input, remove_text


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
