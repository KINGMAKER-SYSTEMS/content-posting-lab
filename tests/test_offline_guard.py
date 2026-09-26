"""The conftest offline guard refuses every non-loopback DNS lookup and connect.

Targets are reserved names and TEST-NET addresses, so even a broken guard
could not reach a real host.
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest


def test_guard_refuses_non_loopback_dns(network_guard_attempts):
    with pytest.raises(OSError, match="test network guard"):
        socket.getaddrinfo("hub.invalid", 443)
    with pytest.raises(OSError, match="test network guard"):
        socket.gethostbyname("hub.invalid")
    assert network_guard_attempts == [
        "socket.getaddrinfo 'hub.invalid'", "socket.gethostbyname 'hub.invalid'",
    ]
    network_guard_attempts.clear()


def test_guard_refuses_non_loopback_connect(network_guard_attempts):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(OSError, match="test network guard"):
            sock.connect(("192.0.2.1", 443))
    assert network_guard_attempts == ["socket.connect '192.0.2.1'"]
    network_guard_attempts.clear()


def test_guard_catches_a_real_httpx_client(network_guard_attempts):
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://hub.invalid/api/campaigns", timeout=1)
    assert network_guard_attempts == ["socket.getaddrinfo 'hub.invalid'"]
    network_guard_attempts.clear()


def test_guard_allows_loopback(network_guard_attempts):
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            pass
    assert network_guard_attempts == []


async def test_guard_records_an_async_httpx_lookup(network_guard_attempts):
    """anyio hands the resolver a bytes host; the attempt must still be recorded
    even when the caller swallows the failure (as the Hub's background sync does)."""
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            await client.get("https://hub.invalid/api/campaigns")
    except Exception:
        pass
    assert network_guard_attempts == ["socket.getaddrinfo 'hub.invalid'"]
    network_guard_attempts.clear()


def test_guard_allows_a_bytes_loopback_host(network_guard_attempts):
    assert socket.getaddrinfo(b"localhost", 80)
    assert network_guard_attempts == []


pytest_plugins = ["pytester"]


def _offline_guard_source() -> str:
    source = (Path(__file__).with_name("conftest.py")).read_text(encoding="utf-8")
    start = source.index("# ── Offline guard")
    end = source.index("# ── End offline guard")
    return "import ipaddress\nimport socket\nimport sys\nimport threading\n\nimport pytest\n\n" + source[start:end]


def test_a_swallowed_async_attempt_still_fails_the_test_at_teardown(pytester):
    # A separate interpreter: audit hooks cannot be removed once installed.
    pytester.makeconftest(_offline_guard_source())
    pytester.makeini("[pytest]\nasyncio_mode = auto\n")
    pytester.makepyfile(test_swallow='''
import httpx


async def test_swallows_the_refusal():
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            await client.get("https://hub.invalid/api/campaigns")
    except Exception:
        pass
''')
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*test attempted real network access: socket.getaddrinfo 'hub.invalid'*"])


def test_guard_refuses_reverse_lookups_and_sendmsg(network_guard_attempts):
    with pytest.raises(OSError, match="test network guard"):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(OSError, match="test network guard"):
        socket.getnameinfo(("192.0.2.1", 443), 0)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="test network guard"):
            sock.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
    assert network_guard_attempts == [
        "socket.gethostbyaddr '192.0.2.1'",
        "socket.getnameinfo '192.0.2.1'",
        "socket.sendmsg '192.0.2.1'",
    ]
    network_guard_attempts.clear()
    socket.getnameinfo(("127.0.0.1", 80), 0)
    assert network_guard_attempts == []
