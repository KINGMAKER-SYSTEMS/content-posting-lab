"""Pin the yt-dlp failure diagnostics in `scraper.frame_extractor.download_video`.

Regression guard for a real support incident: a YouTube link failed in the
Clipper and the surfaced error was

    "no-auth: Deprecated Feature: Support for Python version 3.10 has been
     deprecated ... HTTP Error 403: Forbidden. Cookies were tried from
     /app/projects/cookies.txt but yt-dlp still rejected them"

Three things were wrong with that message:
  1. yt-dlp's interpreter-deprecation banner rode along and read as "the
     clipper is deprecated";
  2. it reported the `no-auth` attempt (the least informative one) rather than
     the cookie-backed attempt that actually mattered;
  3. it blamed expired cookies on the strength of browser-cookie probes that
     can never succeed in a container.
"""

import asyncio
from pathlib import Path

import pytest

import scraper.frame_extractor as fe


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


def _fake_exec(script):
    """Build an exec stub that returns queued results keyed by strategy flag."""
    calls: list[list[str]] = []

    async def _exec(*cmd, **kwargs):
        calls.append(list(cmd))
        for matcher, rc, err in script:
            if matcher(cmd):
                return _FakeProc(rc, err)
        return _FakeProc(1, b"ERROR: unmatched")

    return _exec, calls


@pytest.fixture(autouse=True)
def _stub_deps(monkeypatch):
    monkeypatch.setattr(fe, "_check_deps", lambda: None)
    monkeypatch.setattr(fe, "get_cookies_path", lambda: None)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)


def test_deprecation_banner_is_stripped_from_surfaced_error(monkeypatch, tmp_path):
    """The Python-version banner must not reach the user-facing message."""
    stderr = (
        b"Deprecated Feature: Support for Python version 3.10 has been deprecated. "
        b"Please update to Python 3.11 or above\n"
        b"ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
    exec_stub, _ = _fake_exec([(lambda cmd: True, 1, stderr)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(fe.download_video("https://youtu.be/x", tmp_path / "o.mp4"))

    msg = str(exc.value)
    assert "Deprecated Feature" not in msg
    assert "deprecated" not in msg.lower()
    assert "403" in msg


def test_browser_probes_are_skipped_in_a_container(monkeypatch, tmp_path):
    """`--cookies-from-browser` can never work on Railway — don't even try."""
    exec_stub, calls = _fake_exec([(lambda cmd: True, 1, b"ERROR: nope")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    with pytest.raises(RuntimeError):
        asyncio.run(fe.download_video("https://youtu.be/x", tmp_path / "o.mp4"))

    assert not any("--cookies-from-browser" in c for c in calls)


def test_bounded_call_forwards_max_filesize_to_ytdlp(monkeypatch, tmp_path):
    exec_stub, calls = _fake_exec([(lambda cmd: True, 1, b"ERROR: unavailable")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    with pytest.raises(RuntimeError):
        asyncio.run(fe.download_video(
            "https://cdn.example.com/video.mp4",
            tmp_path / "o.mp4",
            max_filesize=123_456,
        ))

    command = calls[0]
    assert command[command.index("--max-filesize") + 1] == "123456"


def test_browser_probe_failure_does_not_blame_the_cookies_file(monkeypatch, tmp_path):
    """A missing Chrome profile is an environment fact, not an auth rejection.

    Off-container with no cookies.txt configured, the browser probes run and
    fail with "could not find chrome cookies database". Those failures used to
    set the auth flag purely because the word "cookies" appeared in them, so a
    plain 403 came back dressed up as a credentials problem. They must now be
    classified as environmental and leave the verdict to the real error.
    """
    monkeypatch.setattr(fe, "get_cookies_path", lambda: None)
    monkeypatch.setattr(fe, "_in_container", lambda: False)

    def is_browser(cmd):
        return "--cookies-from-browser" in cmd

    exec_stub, calls = _fake_exec([
        (is_browser, 1, b"ERROR: could not find chrome cookies database"),
        (lambda cmd: True, 1, b"ERROR: unable to download video data: HTTP Error 403: Forbidden"),
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(fe.download_video("https://youtu.be/x", tmp_path / "o.mp4"))

    msg = str(exc.value)
    assert any(is_browser(c) for c in calls), "off-container should still probe browsers"
    # The 403 hint wins; no credentials verdict is drawn from browser probes.
    assert "blocked the server itself" in msg
    assert "may be expired" not in msg
    assert "No cookies configured" not in msg


def test_cookie_attempt_is_reported_over_the_no_auth_attempt(monkeypatch, tmp_path):
    """The cookie-backed failure is the informative one — surface that."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: cookies)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    def is_cookie_run(cmd):
        return "--cookies" in cmd

    exec_stub, _ = _fake_exec([
        (is_cookie_run, 1, b"ERROR: Sign in to confirm you are not a bot"),
        (lambda cmd: True, 1, b"ERROR: generic no-auth noise"),
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(fe.download_video("https://youtu.be/x", tmp_path / "o.mp4"))

    msg = str(exc.value)
    assert "Sign in to confirm" in msg
    assert "generic no-auth noise" not in msg
    assert "may be expired" in msg


def test_successful_download_returns_destination(monkeypatch, tmp_path):
    dest = tmp_path / "o.mp4"

    async def _exec(*cmd, **kwargs):
        dest.write_bytes(b"video")
        return _FakeProc(0, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    assert asyncio.run(fe.download_video("https://youtu.be/x", dest)) == dest


def test_tried_strategies_are_listed_for_debugging(monkeypatch, tmp_path):
    exec_stub, _ = _fake_exec([(lambda cmd: True, 1, b"ERROR: unsupported URL")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(fe.download_video("https://example.com/x", tmp_path / "o.mp4"))

    assert "tried: no-auth" in str(exc.value)
