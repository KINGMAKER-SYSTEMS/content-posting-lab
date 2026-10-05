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
@pytest.mark.parametrize("corrupt", [False, True])
async def test_public_health_only_exposes_budget_integrity(
    sync_client, monkeypatch, corrupt
):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "generation_budget_status",
        lambda: {
            "day": "2026-10-04",
            "budgetUsd": 175.0,
            "spentUsd": 12.5,
            "remainingUsd": 162.5,
            "resetsAt": "2026-10-05T00:00:00Z",
            "corrupt": corrupt,
        },
    )

    response = sync_client.get("/api/health")

    assert response.status_code == 200
    budget = response.json()["generation_budget"]
    assert budget == {"corrupt": corrupt}
    assert not {"day", "budgetUsd", "spentUsd", "remainingUsd", "resetsAt"} & set(
        budget
    )


@pytest.mark.asyncio
async def test_api_docs(sync_client):
    """Test that OpenAPI docs are available."""
    response = sync_client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()
