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
async def test_api_docs_not_served(sync_client):
    """The Swagger/OpenAPI routes are off: they are not on the keyless allowlist."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = sync_client.get(path)
        assert "swagger" not in response.text.lower(), path
        assert '"openapi"' not in response.text, path
