"""Relay mode: forward every request on this deployment to a Content Lab running elsewhere.

When ``CONTENT_LAB_RELAY_ORIGIN`` is set, ``python main.py`` runs this instead of the Lab.
Nothing of the Lab starts: no bot, no workers, no schedulers, no writes to the volume. The
old public address keeps answering, so its callers need no change while the Lab itself runs
on another machine. Remove the variable and redeploy to run the Lab here again.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host",
})
_WS_FORWARD = frozenset({"authorization", "cookie", "origin", "sec-websocket-protocol"})
_SESSION = web.AppKey("session", aiohttp.ClientSession)
_ORIGIN = web.AppKey("origin", str)


def relay_origin(value: str) -> str:
    """Return the bare upstream origin, or raise ValueError for anything else."""
    parts = urlsplit(value.strip())
    local = parts.hostname in {"127.0.0.1", "localhost"}
    if (parts.scheme != "https" and not (parts.scheme == "http" and local)) or not parts.hostname:
        raise ValueError("CONTENT_LAB_RELAY_ORIGIN must be an https origin")
    if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("CONTENT_LAB_RELAY_ORIGIN must be a bare origin")
    return f"{parts.scheme}://{parts.netloc}"


def _forwardable(headers) -> CIMultiDict:
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in _HOP_BY_HOP)


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    downstream = web.WebSocketResponse()
    await downstream.prepare(request)
    origin = request.app[_ORIGIN]
    url = ("wss://" if origin.startswith("https://") else "ws://") + origin.split("://", 1)[1] + str(request.rel_url)
    headers = {k: v for k, v in request.headers.items() if k.lower() in _WS_FORWARD}
    try:
        async with request.app[_SESSION].ws_connect(url, headers=headers) as upstream:
            async def pump(source, sink):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await sink.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await sink.send_bytes(message.data)
                    else:
                        break
            tasks = [asyncio.create_task(pump(downstream, upstream)),
                     asyncio.create_task(pump(upstream, downstream))]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    await downstream.close()
    return downstream


async def _handle(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _websocket(request)
    headers = _forwardable(request.headers)
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", request.scheme)
    try:
        async with request.app[_SESSION].request(
            request.method, request.app[_ORIGIN] + str(request.rel_url), headers=headers,
            data=request.content if request.body_exists else None, allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                          headers=_forwardable(upstream.headers))
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(1 << 16):
                await response.write(chunk)
            await response.write_eof()
            return response
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        return web.Response(status=502, text=f"content lab relay: upstream unreachable ({type(error).__name__})")


async def build_app(origin: str) -> web.Application:
    app = web.Application(client_max_size=0)
    app[_ORIGIN] = relay_origin(origin)
    # auto_decompress off: bodies and their Content-Length/Encoding pass through untouched.
    app[_SESSION] = aiohttp.ClientSession(
        auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=900))

    async def close(app):
        await app[_SESSION].close()
    app.on_cleanup.append(close)
    app.router.add_route("*", "/{tail:.*}", _handle)
    return app


def run(origin: str, port: int) -> None:
    relay_origin(origin)  # refuse a bad value before binding the port
    web.run_app(build_app(origin), host="0.0.0.0", port=port, access_log=None)
