"""Regression tests for factory.formats.cards._run — it must NOT use shell=True.

The card generators compose magick command strings from shlex.quote()'d tokens and hand them
to _run(). The hazard (the reason this ticket exists): if _run ever shells out, a value that
drifts past the quoting — or a future param added without quoting — becomes a shell-injection
vector through the magick/card pipeline. _run() must split the string itself (shlex.split) and
invoke subprocess with shell=False so a malicious value can only ever be a single argv token.
"""
from __future__ import annotations

import inspect
import shutil

import pytest

from factory.formats import cards


def test_run_does_not_use_shell():
    """The source of _run must call subprocess with shell=False (or no shell kwarg)."""
    src = inspect.getsource(cards._run)
    assert "shell=True" not in src, "_run must never pass shell=True (injection hazard)"
    assert "shlex.split" in src, "_run must shlex.split the composed command and run shell=False"


def test_injection_value_cannot_escape(monkeypatch, tmp_path):
    """A value containing shell metacharacters must arrive as ONE literal argv token, not as a
    second command. We intercept subprocess.run inside _run and assert on the argv it receives."""
    captured = {}

    def fake_run(args, *a, **kw):
        captured["args"] = args
        captured["shell"] = kw.get("shell", False)
        # make _run think the command succeeded and produced the file
        out = tmp_path / "evil_number.png"
        out.write_bytes(b"\x89PNG\r\n")  # minimal PNG-ish marker so out.exists() is True

        class R:
            returncode = 0
            stderr = ""
            stdout = ""
        return R()

    monkeypatch.setattr(cards.subprocess, "run", fake_run)
    monkeypatch.setattr(cards, "_font", lambda *_a, **_k: "/tmp/fake.ttf")

    payload = '; touch /tmp/PWNED #'
    cards.number_card(payload, "label", "evil", tmp_path, tmp_path)

    assert captured["shell"] is False, "must run with shell=False"
    args = captured["args"]
    assert isinstance(args, list), "argv must be a list, not a shell string"
    # the dangerous value survives as a single argv token (cleaned/truncated but never split into
    # a 'touch' command). Crucially: no token is the standalone shell separator ';' or 'touch'.
    assert ";" not in args, "';' must not be its own argv token"
    assert "touch" not in args, "'touch' must not be its own argv token"
    # the metachar payload rides inside ONE argv element (here cleaned to '; touch'); under
    # shell=True this string would have been TWO commands. As argv it is a single literal token.
    assert "; touch" in args, "payload must be carried as a single literal token, not split"


@pytest.mark.skipif(shutil.which("magick") is None, reason="ImageMagick not installed")
def test_real_render_still_works(tmp_path):
    """End-to-end: with shell=False the generator must still produce a valid PNG."""
    out = cards.number_card("4,096 tokens", "context window", "ctx", tmp_path, tmp_path)
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "output must be a real PNG"
