"""Directory lyric-timing catalog: data-only pages, duplicate rejection, parity.

These cover the build that replaces the single hard-coded Burnt timing file
with a scan of ``recipes/lyric-timing/*.v1.json``. A new lyric page is a data
edit; a page with no timing file is still refused exactly as before, and the
Burnt set resolves byte-for-byte identical to the base.
"""
import hashlib
import json
from pathlib import Path

import pytest

from services import lyric_timing as timing


def _base() -> dict:
    return json.loads(timing._TIMING_PATH.read_bytes())


def _synthetic(value: dict, *, set_id: str, page_id: str, account: str) -> dict:
    return {
        **value,
        "id": set_id,
        "pageId": page_id,
        "account": account,
        "soundId": "9999999999999999999",
        "notionPageId": "00000000-0000-0000-0000-000000000001",
        "sourceProgramId": "synthetic-program-v1",
        "sourceProgramSha256": "a" * 64,
        "referenceAudioSha256": "b" * 64,
    }


def _copy_burnt_bytes(tmp: Path) -> Path:
    path = tmp / "burnt-heavy-foot-chorus.v1.json"
    path.write_bytes(timing._TIMING_PATH.read_bytes())
    return path


@pytest.fixture
def mixed_dir(tmp_path, monkeypatch):
    """A tmp timing directory: Burnt's exact bytes plus one synthetic page."""
    _copy_burnt_bytes(tmp_path)
    (tmp_path / "second-page-chorus.v1.json").write_text(
        json.dumps(_synthetic(_base(), set_id="second-page-chorus-v1", page_id="acct:second-page", account="second.page"))
    )
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    return tmp_path


def test_synthetic_second_lyric_page_resolves(mixed_dir):
    options = timing.lyric_timing_catalog(page_id="acct:second-page", account="second.page")
    assert len(options) == 1
    assert options[0]["id"] == "second-page-chorus-v1"
    assert options[0]["soundId"] == "9999999999999999999"
    assert options[0]["notionPageId"] == "00000000-0000-0000-0000-000000000001"
    resolved = timing.resolve_lyric_timing(
        "second-page-chorus-v1",
        page_id="acct:second-page",
        account="second.page",
        sound_id="9999999999999999999",
        expected_sha256=options[0]["sha256"],
    )
    assert resolved["pageId"] == "acct:second-page"
    assert resolved["account"] == "second.page"
    assert len(resolved["phrases"]) == 7
    assert resolved["sha256"] == hashlib.sha256(resolved["manifestJson"].encode()).hexdigest()


def test_page_without_timing_file_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    assert timing.lyric_timing_catalog(page_id="acct:no-page", account="no.page") == []
    with pytest.raises(timing.LyricTimingError) as error:
        timing.resolve_lyric_timing(
            "burnt-heavy-foot-chorus-v1",
            page_id="acct:burnt-mill-rd",
            account="burnt.mill.rd",
            sound_id="7611482162773035025",
            expected_sha256="0" * 64,
        )
    assert error.value.code == "lyric_timing_unknown"


def test_duplicate_set_id_rejected_at_load(tmp_path, monkeypatch):
    base = _base()
    (tmp_path / "one.v1.json").write_text(json.dumps(base))
    (tmp_path / "two.v1.json").write_text(
        json.dumps({**base, "pageId": "acct:other-page", "account": "other.page"})
    )
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    with pytest.raises(timing.LyricTimingError) as error:
        timing.lyric_timing_catalog(page_id="acct:burnt-mill-rd", account="burnt.mill.rd")
    assert error.value.code == "lyric_timing_invalid"
    assert "duplicate lyric timing set id" in str(error.value)


def test_duplicate_page_id_rejected_at_load(tmp_path, monkeypatch):
    base = _base()
    (tmp_path / "one.v1.json").write_text(json.dumps(base))
    (tmp_path / "two.v1.json").write_text(json.dumps({**base, "id": "other-set-v1"}))
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    with pytest.raises(timing.LyricTimingError) as error:
        timing.lyric_timing_catalog(page_id="acct:burnt-mill-rd", account="burnt.mill.rd")
    assert error.value.code == "lyric_timing_invalid"
    assert "duplicate lyric timing page" in str(error.value)


def test_burnt_outputs_equal_before_and_after(tmp_path, monkeypatch):
    # Baseline resolved from the real shipped directory.
    timing._TIMING_DIR = Path(__file__).resolve().parents[1] / "recipes" / "lyric-timing"
    timing._reset_catalog()
    baseline = timing.resolve_lyric_timing(
        "burnt-heavy-foot-chorus-v1",
        page_id="acct:burnt-mill-rd",
        account="burnt.mill.rd",
        sound_id="7611482162773035025",
        expected_sha256=timing._TIMING_SHA256,
    )
    # Resolve again from a mixed directory holding Burnt's exact bytes + a synthetic page.
    _copy_burnt_bytes(tmp_path)
    (tmp_path / "second-page-chorus.v1.json").write_text(
        json.dumps(_synthetic(_base(), set_id="second-page-chorus-v1", page_id="acct:second-page", account="second.page"))
    )
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    after = timing.resolve_lyric_timing(
        "burnt-heavy-foot-chorus-v1",
        page_id="acct:burnt-mill-rd",
        account="burnt.mill.rd",
        sound_id="7611482162773035025",
        expected_sha256=timing._TIMING_SHA256,
    )
    assert after == baseline
    assert baseline["sha256"] == timing._TIMING_SHA256
    assert baseline["id"] == "burnt-heavy-foot-chorus-v1"
    assert baseline["referenceAudioSha256"] == "11ace1c61ab8535c87a71dddbe7f06d03ac56bd3324809561cb49a8ddf95823f"
    assert baseline["sourceProgramSha256"] == "78da457008391bd873dbc23c57fc7621f9707b8e139781e8d98cea4cdb9f65c9"
