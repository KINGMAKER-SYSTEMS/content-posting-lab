"""
Pytest configuration and fixtures for Content Posting Lab.
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def temp_project_dir():
    """Temporary directory for test artifacts."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
async def async_client():
    """AsyncClient for testing FastAPI endpoints."""
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
def sync_client():
    """Synchronous TestClient for testing FastAPI endpoints."""
    return TestClient(app)
