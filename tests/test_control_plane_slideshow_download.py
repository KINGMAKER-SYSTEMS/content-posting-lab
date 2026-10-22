"""Syzygy signed render-file redirect contract for download_syzygy_artifact.

Syzygy GET /render-file/{name} with an API key does not stream bytes; it mints a
307 (or 302/303/308) redirect to the same path carrying a short-lived signed
``token`` query. Content Lab must follow exactly one such same-origin redirect,
drop the Authorization header on the signed request, and fail closed on every
other target shape.
"""

import httpx
import pytest
import traceback

import services.control_plane_slideshows as slideshows
from services.control_plane_slideshows import (
    SyzygyError,
    SyzygyUnavailable,
    download_syzygy_artifact,
)


BASE = "https://syzygy.example"
SIGNED_TOKEN = "abc123-token"


@pytest.fixture
def runtime_env(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", BASE)
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")


def _patch_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        slideshows.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(transport=httpx.MockTransport(handler)),
    )


def _recording(handler):
    """Wrap a handler to record (url, headers) of every request it serves."""
    calls = []

    def wrapped(request):
        calls.append((str(request.url), dict(request.headers)))
        return handler(request)

    return wrapped, calls


def _signed_location(name: str = "out%2Emp4", query: str = f"token={SIGNED_TOKEN}") -> str:
    return f"/render-file/{name}?{query}"


@pytest.mark.asyncio
async def test_download_follows_signed_same_origin_redirect(
    runtime_env, monkeypatch, tmp_path,
):
    """RED->GREEN: the current 307-to-signed-URL download must stream bytes."""
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.query:
            return httpx.Response(200, content=b"render-bytes")
        return httpx.Response(307, headers={"location": _signed_location("out%2Emp4")})

    _patch_client(monkeypatch, handler)
    destination = tmp_path / "render.mp4"
    await download_syzygy_artifact("/render-file/out.mp4", destination)
    assert destination.read_bytes() == b"render-bytes"
    # The first request carries the API key; the signed request must not.
    assert len(calls) == 2
    assert calls[0].headers.get("authorization") == "Bearer syz_test_key"
    assert "authorization" not in calls[1].headers


@pytest.mark.asyncio
async def test_download_rejects_cross_origin_location(
    runtime_env, monkeypatch, tmp_path,
):
    def handler(request):
        return httpx.Response(
            307,
            headers={"location": f"https://evil.example/render-file/out%2Emp4?token={SIGNED_TOKEN}"},
        )

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected") as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert SIGNED_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_download_rejects_different_filename(
    runtime_env, monkeypatch, tmp_path,
):
    def handler(request):
        return httpx.Response(
            307,
            headers={"location": _signed_location("other%2Emp4")},
        )

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected"):
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")


@pytest.mark.asyncio
async def test_download_rejects_extra_query_keys(
    runtime_env, monkeypatch, tmp_path,
):
    def handler(request):
        return httpx.Response(
            307,
            headers={"location": _signed_location(query=f"token={SIGNED_TOKEN}&foo=1")},
        )

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected") as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert SIGNED_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_download_rejects_missing_token(
    runtime_env, monkeypatch, tmp_path,
):
    def handler(request):
        return httpx.Response(307, headers={"location": "/render-file/out%2Emp4"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected"):
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")


@pytest.mark.asyncio
async def test_download_rejects_double_redirect(
    runtime_env, monkeypatch, tmp_path,
):
    def handler(request):
        return httpx.Response(307, headers={"location": _signed_location("out%2Emp4")})

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected") as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert SIGNED_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_download_error_never_leaks_token(
    runtime_env, monkeypatch, tmp_path,
):
    # A signed URL that 404s must name the status without echoing the token.
    def handler(request):
        if request.url.query:
            return httpx.Response(404)
        return httpx.Response(307, headers={"location": _signed_location("out%2Emp4")})

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError) as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert SIGNED_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_download_signed_transport_error_drops_cause_and_token(
    runtime_env, monkeypatch, tmp_path,
):
    # A transport error on the SIGNED request must not retain the underlying
    # exception (whose str() embeds the token-bearing URL) as __cause__.
    def handler(request):
        if request.url.query:
            raise httpx.ConnectError(f"connect failed to {request.url}", request=request)
        return httpx.Response(307, headers={"location": _signed_location("out%2Emp4")})

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyUnavailable) as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert exc.value.__cause__ is None
    chain = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert SIGNED_TOKEN not in chain
