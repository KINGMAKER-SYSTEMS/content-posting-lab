"""Tests for providers.replicate.remove_text."""

import asyncio

import pytest

from providers.replicate import remove_text


def test_remove_text_rejects_empty_image():
    """remove_text raises ValueError when given an empty data URI."""
    with pytest.raises(ValueError, match="image"):
        asyncio.run(remove_text("", None))


def test_remove_text_rejects_none_image():
    """remove_text raises ValueError when given None."""
    with pytest.raises(ValueError, match="image"):
        asyncio.run(remove_text(None, None))  # type: ignore[arg-type]
