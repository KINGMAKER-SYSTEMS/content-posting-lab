"""Regression guard: the `/api/drive` router is intentionally not mounted.

The Google Drive router (routers/gdrive.py) exported 8 HTTP endpoints that
had zero callers from the SPA — not even the response types defined for it
(DriveStatusResponse / DriveInventoryResponse / DriveFile) were referenced.
Drive is being superseded by R2 ("Drive (legacy)" in the roster UI), so the
dead, unreachable HTTP surface was removed from the build.

These tests pin that removal so the router can't silently creep back in:
  * the `routers.gdrive` module no longer exists
  * no route on the app lives under the `/api/drive` prefix

The underlying services.gdrive helper library is *not* removed — it is still
used as a library and is covered by tests/test_gdrive.py.
"""

import importlib

import pytest

from app import app


def test_gdrive_router_module_gone():
    """The orphaned router module should no longer be importable."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("routers.gdrive")


def test_no_api_drive_routes_mounted():
    """No HTTP route should be served under the /api/drive prefix."""
    drive_paths = [
        getattr(r, "path", "")
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/drive")
    ]
    assert drive_paths == [], f"unexpected /api/drive routes still mounted: {drive_paths}"


def test_api_drive_status_returns_404(sync_client):
    """A representative former endpoint must now 404 (not be reachable)."""
    resp = sync_client.get("/api/drive/status")
    assert resp.status_code == 404


def test_services_gdrive_still_present():
    """The Drive helper library stays — only the unreachable router was cut."""
    mod = importlib.import_module("services.gdrive")
    assert hasattr(mod, "is_configured")
