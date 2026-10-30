"""Exercise the real HTTP adapter rather than mocking revision validation away."""

import httpx
import pytest

from services import control_plane_slideshows as slideshows


@pytest.mark.parametrize("revision", ["c7dd0fea35665706396e6ca18aa6430a72591d27", "d" * 64])
async def test_readiness_preserves_full_renderer_revision(monkeypatch, revision):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "service": "syzygy", "revision": revision})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        slideshows.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert await slideshows.syzygy_revision() == revision
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/readyz")]


@pytest.mark.parametrize("revision", [None, 42, "", "unknown", "a" * 7, "a" * 39,
                                        "a" * 41, "a" * 63, "a" * 65, "g" * 40,
                                        "a" * 40 + "\n", " " + "a" * 40])
async def test_revision_refuses_missing_partial_or_malformed_evidence(monkeypatch, revision):
    async def response(*args, **kwargs):
        return {"revision": revision}

    monkeypatch.setattr(slideshows, "_request_json", response)
    with pytest.raises(slideshows.SyzygyError, match="revision is unavailable"):
        await slideshows.syzygy_revision()
