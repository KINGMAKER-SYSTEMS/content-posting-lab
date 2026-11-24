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


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    # Keep the real attempt count; drop the waits between attempts.
    monkeypatch.setattr(
        slideshows, "_DOWNLOAD_RETRY_DELAYS_S",
        tuple(0.0 for _ in slideshows._DOWNLOAD_RETRY_DELAYS_S),
    )


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


# --- retry: a finished render is fetched again when the transfer fails --------


class _BrokenStream(httpx.AsyncByteStream):
    """Sends part of the body, then drops the connection."""

    async def __aiter__(self):
        yield b"partial-"
        raise httpx.ReadError("connection reset mid-body")


def _flaky(failures, failure):
    """Serve the 307, then fail the signed request `failures` times, then succeed."""
    state = {"signed": 0, "first": 0}

    def handler(request):
        if not request.url.query:
            state["first"] += 1
            return httpx.Response(307, headers={"location": _signed_location("out%2Emp4")})
        state["signed"] += 1
        if state["signed"] <= failures:
            return failure(request)
        return httpx.Response(200, content=b"render-bytes")

    return handler, state


def _connect_error(request):
    raise httpx.ConnectError("connect failed", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    _connect_error,
    lambda request: httpx.Response(200, stream=_BrokenStream()),
    lambda request: httpx.Response(502),
    lambda request: httpx.Response(503),
    lambda request: httpx.Response(504),
], ids=["connect-error", "dropped-mid-body", "502", "503", "504"])
async def test_download_retries_a_transient_failure_with_a_fresh_signed_url(
    runtime_env, monkeypatch, tmp_path, failure,
):
    handler, state = _flaky(1, failure)
    _patch_client(monkeypatch, handler)
    destination = tmp_path / "render.mp4"
    await download_syzygy_artifact("/render-file/out.mp4", destination)
    # Only the complete body is on disk, never the partial bytes of attempt one.
    assert destination.read_bytes() == b"render-bytes"
    # Each attempt asked Syzygy for a fresh signed URL.
    assert state == {"first": 2, "signed": 2}


@pytest.mark.asyncio
async def test_download_survives_two_failures(runtime_env, monkeypatch, tmp_path):
    handler, state = _flaky(2, _connect_error)
    _patch_client(monkeypatch, handler)
    destination = tmp_path / "ok.mp4"
    await download_syzygy_artifact("/render-file/out.mp4", destination)
    assert destination.read_bytes() == b"render-bytes"
    assert state["signed"] == 3


@pytest.mark.asyncio
async def test_download_gives_up_after_three_attempts(
    runtime_env, monkeypatch, tmp_path,
):
    handler, state = _flaky(3, _connect_error)
    _patch_client(monkeypatch, handler)
    destination = tmp_path / "never.mp4"
    with pytest.raises(SyzygyUnavailable) as exc:
        await download_syzygy_artifact("/render-file/out.mp4", destination)
    assert state["signed"] == 3
    assert not destination.exists()
    assert exc.value.__cause__ is None
    chain = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert SIGNED_TOKEN not in chain


@pytest.mark.asyncio
async def test_download_retry_waits_between_attempts_only(
    runtime_env, monkeypatch, tmp_path,
):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(slideshows, "_DOWNLOAD_RETRY_DELAYS_S", (2.0, 5.0))
    monkeypatch.setattr(slideshows.asyncio, "sleep", fake_sleep)
    handler, _ = _flaky(3, _connect_error)
    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyUnavailable):
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert waits == [2.0, 5.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_download_does_not_retry_a_contract_failure(
    runtime_env, monkeypatch, tmp_path, status,
):
    handler, state = _flaky(99, lambda request: httpx.Response(status))
    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError) as exc:
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert not isinstance(exc.value, SyzygyUnavailable)
    assert state == {"first": 1, "signed": 1}


@pytest.mark.asyncio
async def test_download_does_not_retry_a_rejected_redirect(
    runtime_env, monkeypatch, tmp_path,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": _signed_location("other%2Emp4")})

    _patch_client(monkeypatch, handler)
    with pytest.raises(SyzygyError, match="redirect rejected"):
        await download_syzygy_artifact("/render-file/out.mp4", tmp_path / "r.mp4")
    assert len(calls) == 1
