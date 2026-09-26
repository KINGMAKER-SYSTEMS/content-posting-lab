"""The conftest offline guard refuses every non-loopback DNS lookup and connect.

Targets are reserved names and TEST-NET addresses, so even a broken guard
could not reach a real host.
"""

from __future__ import annotations

import socket

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
