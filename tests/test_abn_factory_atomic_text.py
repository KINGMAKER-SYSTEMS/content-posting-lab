"""Tests for safe scratch text writes in services/abn_factory.py.

The kinetic/snippet/tape/_readme scratch intermediates feed downstream processes
(seekable-html renderer, VHS/ffmpeg). A crash mid-write must never leave a
truncated file for the consumer to read. These pin the atomicity contract of
`_atomic_write_text`: full content lands or nothing does, and no `.tmp` litter
survives a successful write.
Kinetic parameters must also remain exact data inside the written HTML script.
"""
import asyncio
import json
import os
from html.parser import HTMLParser

import pytest

from services import abn_assets, abn_factory


def test_atomic_write_text_writes_full_content(tmp_path):
    dest = tmp_path / "sub" / "kinetic.html"  # parent does not exist yet
    payload = "<html>" + "x" * 100_000 + "</html>"
    abn_factory._atomic_write_text(dest, payload)
    assert dest.read_text() == payload
    # no temp litter left behind
    assert not (dest.parent / (dest.name + ".tmp")).exists()


def test_atomic_write_text_crash_leaves_no_partial(tmp_path, monkeypatch):
    """If the process dies after the tmp write but before the rename, the
    destination must be absent/untouched — never a truncated half-file."""
    dest = tmp_path / "tape.txt"

    def boom(src, dst):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(abn_factory.os, "replace", boom)
    with pytest.raises(RuntimeError):
        abn_factory._atomic_write_text(dest, "downstream-would-read-this")
    # the consumer sees no file at all, rather than a corrupt one
    assert not dest.exists()


def test_atomic_write_text_overwrite_is_all_or_nothing(tmp_path, monkeypatch):
    """An overwrite that crashes mid-rename leaves the OLD good file intact."""
    dest = tmp_path / "snippet.py"
    dest.write_text("old-good-content\n")

    monkeypatch.setattr(
        abn_factory.os, "replace",
        lambda src, dst: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError):
        abn_factory._atomic_write_text(dest, "new-but-never-committed")
    assert dest.read_text() == "old-good-content\n"


@pytest.mark.parametrize("value", [
    "ordinary typography parameters",
    "</script><script>/* inert fixture */</script>",
    "</ScRiPt ><ScRiPt>/* inert fixture */</ScRiPt>",
    "<!--<script></script> & <b>builder news</b>",
    r'path\1\g<1>\u003c and "quoted" text',
    "Mon Rovîa — 東京\nline separator:\u2028paragraph separator:\u2029",
])
def test_kinetic_params_remain_exact_data_in_one_html_script(tmp_path, monkeypatch, value):
    template = tmp_path / "templates" / "stat-shrine"
    template.mkdir(parents=True)
    (template / "index.html").write_text(
        '<html><script>const P = {claim: "base"}; window.fixture = P;</script></html>',
        encoding="utf-8",
    )
    params = {"claim": value, "statValue": 42, "items": [value, "plain"], "source": "SRC: EXAMPLE.TEST"}
    monkeypatch.setattr(abn_factory, "_KINETIC_DIR", template.parent)
    monkeypatch.setattr(abn_factory, "_kinetic_params", lambda *_: ("stat-shrine", params))
    monkeypatch.setattr(abn_factory, "ASSETS", tmp_path / "assets")
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", tmp_path / "assets")
    captured = []

    async def capture_html_without_rendering(command, timeout):
        source = abn_factory.scratch_path("ep_c0ffee_s0", "ep_c0ffee_s0_kinetic.html")
        captured.append(source.read_text(encoding="utf-8"))
        return 1, "fixture stops before browser or media execution"

    monkeypatch.setattr(abn_factory, "_sh", capture_html_without_rendering)
    assert asyncio.run(abn_factory._kinetic_insert("fixture", "fixture", "https://example.test", "ep_c0ffee_s0")) is None

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.scripts = []
            self.in_script = False

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.in_script = True
                self.scripts.append("")

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False

        def handle_data(self, data):
            if self.in_script:
                self.scripts[-1] += data

    parsed = Scripts()
    parsed.feed(captured[0])
    assert len(parsed.scripts) == 1, "parameter data cannot terminate the template's script"
    script = parsed.scripts[0]
    assert "<" not in script
    remaining = script.removeprefix("const P = {").removesuffix("}; window.fixture = P;")
    decoded = {}
    for index, key in enumerate(params):
        prefix = ("," if index else "") + key + ": "
        assert remaining.startswith(prefix)
        remaining = remaining[len(prefix):]
        decoded[key], consumed = json.JSONDecoder().raw_decode(remaining)
        remaining = remaining[consumed:]
    assert remaining == ""
    assert decoded == params, "script escaping preserves the original JSON values"
    assert not abn_factory.scratch_path("ep_c0ffee_s0", "ep_c0ffee_s0_kinetic.html").exists()
