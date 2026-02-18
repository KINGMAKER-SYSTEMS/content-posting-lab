"""
Smoke tests for frontend with Playwright.
Verifies basic page load and component rendering.
"""

import pytest


@pytest.mark.asyncio
async def test_frontend_loads(page):
    """Test that the frontend loads without errors."""
    await page.goto("http://localhost:5173")
    await page.wait_for_load_state("networkidle")

    # Check that the page title is set
    title = await page.title()
    assert title is not None
    assert len(title) > 0


@pytest.mark.asyncio
async def test_frontend_has_content(page):
    """Test that the frontend renders content."""
    await page.goto("http://localhost:5173")
    await page.wait_for_load_state("networkidle")

    # Check that body has content
    body = await page.locator("body")
    assert body is not None
