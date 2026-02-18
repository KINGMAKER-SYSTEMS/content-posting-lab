"""
Smoke tests for FastAPI backend.
Verifies basic app startup and health endpoints.
"""

import pytest


@pytest.mark.asyncio
async def test_app_startup(sync_client):
    """Test that the app starts without errors."""
    response = sync_client.get("/api/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_docs(sync_client):
    """Test that OpenAPI docs are available."""
    response = sync_client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()
