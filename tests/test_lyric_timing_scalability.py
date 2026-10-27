"""Regression guard: lyric page duration and phrase count are page DATA.

A new lyric/caption slideshow page is a data edit (a timing manifest). These
tests prove the resolver no longer pins the shipped Burnt page's 20_000 ms
duration or its 7 phrases: a manifest with a different duration and phrase
count must resolve, and the shipped page must keep resolving byte-for-byte.
If either `20_000` or `7` is re-hard-coded into ``services/lyric_timing.py``,
``test_non_default_duration_and_phrase_count_resolve`` goes RED.
"""
import hashlib
import json
from pathlib import Path

import pytest

from services import lyric_timing as timing


def _manifest(*, duration_ms: int, phrase_texts: list[str], page_id: str,
              account: str, set_id: str) -> dict:
    """Build a structurally valid timing manifest for any duration/phrase count."""
    assert phrase_texts, "at least one phrase"
    # Contiguous, non-overlapping, half-open phrases covering [0, duration_ms].
    step = duration_ms / len(phrase_texts)
    phrases = []
    for index, text in enumerate(phrase_texts):
        start = round(index * step)
        end = round((index + 1) * step) if index + 1 < len(phrase_texts) else duration_ms
        phrases.append({"startMs": start, "endMs": end, "text": text})
    return {
        "schema": "content-lab.lyric-timing-set.v1",
        "id": set_id,
        "pageId": page_id,
        "account": account,
        "soundId": "9999999999999999999",
        "durationMs": duration_ms,
        "phrases": phrases,
        "canonicalCaption": "\n".join(phrase_texts),
        "referenceAudioSha256": "b" * 64,
        "timingProvenance": "synthetic scalability fixture",
        "intervalSemantics": "start inclusive, end exclusive",
        "sourceProgramId": "synthetic-program-v1",
        "sourceProgramSha256": "a" * 64,
        "notionPageId": "00000000-0000-0000-0000-000000000001",
    }


@pytest.fixture
def mixed_dir(tmp_path, monkeypatch):
    # The shipped page's exact bytes plus one non-default page.
    (tmp_path / "burnt-heavy-foot-chorus.v1.json").write_bytes(
        timing._TIMING_PATH.read_bytes()
    )
    non_default = _manifest(
        duration_ms=15_000,
        phrase_texts=["one", "two", "three", "four", "five", "six"],
        page_id="acct:scalable-page",
        account="scalable.page",
        set_id="scalable-page-chorus-v1",
    )
    (tmp_path / "scalable-page-chorus.v1.json").write_text(json.dumps(non_default))
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    return tmp_path


def test_non_default_duration_and_phrase_count_resolve(mixed_dir):
    # 6 phrases / 15s is deliberately NOT the shipped Burnt shape.
    options = timing.lyric_timing_catalog(page_id="acct:scalable-page", account="scalable.page")
    assert len(options) == 1
    resolved = timing.resolve_lyric_timing(
        "scalable-page-chorus-v1",
        page_id="acct:scalable-page",
        account="scalable.page",
        sound_id="9999999999999999999",
        expected_sha256=options[0]["sha256"],
    )
    assert resolved["durationMs"] == 15_000
    assert len(resolved["phrases"]) == 6


def test_shipped_burnt_page_still_resolves_identically(mixed_dir):
    resolved = timing.resolve_lyric_timing(
        "burnt-heavy-foot-chorus-v1",
        page_id="acct:burnt-mill-rd",
        account="burnt.mill.rd",
        sound_id="7611482162773035025",
        expected_sha256=timing._TIMING_SHA256,
    )
    assert resolved["durationMs"] == 20_000
    assert len(resolved["phrases"]) == 7
    assert resolved["sha256"] == timing._TIMING_SHA256
    assert resolved["canonicalCaption"] == "\n".join(
        p["text"] for p in resolved["phrases"]
    )


def test_out_of_range_duration_and_phrase_count_still_refused(tmp_path, monkeypatch):
    for bad in [
        _manifest(duration_ms=0, phrase_texts=["a"], page_id="acct:z", account="z", set_id="z1"),
        _manifest(duration_ms=61_000, phrase_texts=["a"], page_id="acct:z", account="z", set_id="z1"),
    ]:
        (tmp_path / "bad.v1.json").write_text(json.dumps(bad))
        monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
        timing._reset_catalog()
        with pytest.raises(timing.LyricTimingError):
            timing.lyric_timing_catalog(page_id="acct:z", account="z")


def test_phrase_cap_is_sized_to_v1_worker_reservation():
    # MED-2: the phrase cap is the shipped page's count (7), not the earlier
    # unchecked 50, so a timed render's peak workspace (one PNG + one JSON per
    # phrase, two cue frames per edge phrase) stays inside the unchanged v1
    # durable-worker disk reservation. Raise it only together with that
    # reservation (see test_post_render_timed_jobs.py).
    assert timing.MAX_TIMING_PHRASES == 7


def test_more_than_seven_phrases_refused(tmp_path, monkeypatch):
    too_many = _manifest(
        duration_ms=20_000,
        phrase_texts=[f"phrase {index}" for index in range(1, 9)],
        page_id="acct:too-many-phrases",
        account="too.many.phrases",
        set_id="too-many-phrases-v1",
    )
    (tmp_path / "too-many.v1.json").write_text(json.dumps(too_many))
    monkeypatch.setattr(timing, "_TIMING_DIR", tmp_path)
    timing._reset_catalog()
    with pytest.raises(timing.LyricTimingError):
        timing.lyric_timing_catalog(page_id="acct:too-many-phrases", account="too.many.phrases")
