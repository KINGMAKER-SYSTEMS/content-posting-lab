"""Tests for crash-safe scratch writes in services/abn_factory.py.

The kinetic/snippet/tape/_readme scratch intermediates feed downstream processes
(seekable-html renderer, VHS/ffmpeg). A crash mid-write must never leave a
truncated file for the consumer to read. These pin the atomicity contract of
`_atomic_write_text`: full content lands or nothing does, and no `.tmp` litter
survives a successful write.
"""
import os

import pytest

from services import abn_factory


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
