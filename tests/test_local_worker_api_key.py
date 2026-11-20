"""The worker routes stay reachable when the browser API key is switched on.

The other worker tests run with APP_API_KEY unset, so the API-key middleware
never runs in them. In production it is set, and only the ``_AUTH_SKIP`` entry
for /api/local-worker/ lets the worker in with its own X-Worker-Key.
"""

import app as app_module
from services import local_worker_queue as queue

BROWSER_KEY = "browser-facing-key"
WORKER_KEY = "worker-secret"


def test_worker_routes_pass_the_browser_api_key_but_other_routes_do_not(sync_client, monkeypatch):
    monkeypatch.setattr(app_module, "_APP_API_KEY", BROWSER_KEY)
    monkeypatch.setenv("APP_API_KEY", BROWSER_KEY)
    monkeypatch.setenv("LOCAL_WORKER_KEY", WORKER_KEY)
    queue.reset()
    try:
        ok = sync_client.post("/api/local-worker/claim", headers={"X-Worker-Key": WORKER_KEY})
        assert ok.status_code == 200
        assert ok.json() == {"job": None}

        # A wrong worker key is refused by the worker router itself, not the middleware.
        bad = sync_client.post("/api/local-worker/claim", headers={"X-Worker-Key": "wrong"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "invalid or missing worker key"

        # The browser key does not stand in for the worker key.
        browser = sync_client.post("/api/local-worker/claim", headers={"X-API-Key": BROWSER_KEY})
        assert browser.status_code == 401
        assert browser.json()["detail"] == "invalid or missing worker key"

        # Other API routes still need the browser key, and the worker key does not open them.
        assert sync_client.get("/api/video/providers").status_code == 401
        assert sync_client.get(
            "/api/video/providers", headers={"X-Worker-Key": WORKER_KEY}
        ).status_code == 401
        # Positive control: the route exists and the browser key opens it.
        assert sync_client.get(
            "/api/video/providers", headers={"X-API-Key": BROWSER_KEY}
        ).status_code == 200
    finally:
        queue.reset()
