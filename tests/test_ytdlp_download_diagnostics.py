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


def test_source_import_mode_verifies_tls_and_starts_an_owned_process_group(
    monkeypatch, tmp_path,
):
    calls = []

    async def _exec(*cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return _FakeProc(1, b"ERROR: unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("stale")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: cookies)

    with pytest.raises(RuntimeError):
        asyncio.run(fe.download_video(
            "https://cdn.example.com/video.mp4",
            tmp_path / "o.mp4",
            source_import_mode=True,
        ))

    command, options = calls[0]
    assert "--no-check-certificates" not in command
    assert "--cookies" not in command
    assert "--cookies-from-browser" not in command
    assert command[command.index("-f") + 1].startswith("source/")
    assert options["start_new_session"] is True


def test_source_import_cancellation_kills_the_complete_process_group(
    monkeypatch, tmp_path,
):
    killed = []

    class BlockingProc:
        pid = 4321
        returncode = None
        calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            self.returncode = -9
            return b"", b""

    process = BlockingProc()

    async def _exec(*_cmd, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    monkeypatch.setattr(fe.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    async def run():
        task = asyncio.create_task(fe.download_video(
            "https://cdn.example.com/video.mp4",
            tmp_path / "o.mp4",
            source_import_mode=True,
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert killed == [(4321, fe.signal.SIGKILL)]


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


_BOT_CHECK = (
    b"ERROR: [youtube] bxkIIQKC8aA: Sign in to confirm you\xe2\x80\x99re not a bot. "
    b"Use --cookies-from-browser or --cookies for the authentication."
)


def _source_import_exec(results):
    calls = []

    async def _exec(*cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        rc, err = results[min(len(calls), len(results)) - 1]
        if rc == 0:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"mp4")
        return _FakeProc(rc, err)

    return _exec, calls


def test_youtube_source_import_falls_back_to_cookies_after_the_bot_check(
    monkeypatch, tmp_path,
):
    """Live 2026-09-25: every YouTube page import failed `no-auth` on Railway."""
    exec_stub, calls = _source_import_exec([(1, _BOT_CHECK), (0, b"")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: cookies)

    out = asyncio.run(fe.download_video(
        "https://www.youtube.com/watch?v=bxkIIQKC8aA",
        tmp_path / "o.mp4",
        source_import_mode=True,
    ))

    assert out == tmp_path / "o.mp4"
    (public, public_options), (retry, retry_options) = calls
    assert "--cookies" not in public
    private = Path(retry[retry.index("--cookies") + 1])
    assert private != cookies and private.parent == fe._private_jar_dir()
    assert private.parent != tmp_path, "the copy must never land in the import workspace"
    assert not private.exists()
    for command, options in ((public, public_options), (retry, retry_options)):
        assert "--no-check-certificates" not in command
        assert "--cookies-from-browser" not in command
        assert options["start_new_session"] is True


def test_non_youtube_source_import_never_receives_the_cookie_jar(monkeypatch, tmp_path):
    exec_stub, calls = _source_import_exec([(1, _BOT_CHECK)])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: cookies)

    with pytest.raises(RuntimeError):
        asyncio.run(fe.download_video(
            "https://youtube.com.example.net/watch?v=x",
            tmp_path / "o.mp4",
            source_import_mode=True,
        ))

    assert len(calls) == 1
    assert "--cookies" not in calls[0][0]


def test_public_youtube_source_import_succeeds_without_touching_cookies(
    monkeypatch, tmp_path,
):
    exec_stub, calls = _source_import_exec([(0, b"")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_stub)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: cookies)

    asyncio.run(fe.download_video(
        "https://youtu.be/bxkIIQKC8aA", tmp_path / "o.mp4", source_import_mode=True,
    ))

    assert len(calls) == 1
    assert "--cookies" not in calls[0][0]


def test_youtube_source_import_never_writes_the_shared_cookie_jar(monkeypatch, tmp_path):
    """yt-dlp rewrites its --cookies file on exit; imports run concurrently."""
    seen = []

    async def _exec(*cmd, **kwargs):
        cmd = list(cmd)
        if "--cookies" in cmd:
            jar = Path(cmd[cmd.index("--cookies") + 1])
            seen.append(jar.read_text())
            jar.write_text("# rewritten by yt-dlp\n")
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"mp4")
            return _FakeProc(0, b"")
        return _FakeProc(1, _BOT_CHECK)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(fe, "_in_container", lambda: True)
    shared = tmp_path / "shared" / "cookies.txt"
    shared.parent.mkdir()
    shared.write_text("# Netscape HTTP Cookie File\nshared\n")
    monkeypatch.setattr(fe, "get_cookies_path", lambda: shared)
    work = tmp_path / "work"
    work.mkdir()

    asyncio.run(fe.download_video(
        "https://www.youtube.com/watch?v=bxkIIQKC8aA", work / "o.mp4", source_import_mode=True,
    ))

    assert seen == ["# Netscape HTTP Cookie File\nshared\n"]
    assert shared.read_text() == "# Netscape HTTP Cookie File\nshared\n"
    assert sorted(p.name for p in work.iterdir()) == ["o.mp4"]


def test_private_jar_copies_left_by_a_hard_kill_are_swept_at_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.tempfile, "gettempdir", lambda: str(tmp_path))
    jars = fe._private_jar_dir()
    (jars / ".ytdlp-cookies-abc.txt").write_text("# jar")
    (jars / ".ytdlp-cookies-def.txt").write_text("# jar")
    (jars / "unrelated.txt").write_text("keep")

    assert fe.sweep_private_cookie_jars() == 2
    assert sorted(p.name for p in jars.iterdir()) == ["unrelated.txt"]
    assert oct(jars.stat().st_mode & 0o777) == "0o700"
