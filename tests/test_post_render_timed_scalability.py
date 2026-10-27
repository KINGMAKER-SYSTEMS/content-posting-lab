"""Regression guard: the timed lyric renderer no longer pins a 7-phrase shape.

These run only in the full-suite environment (``services.post_render_timed``
imports PIL/pydantic, unavailable on the reduced review host). If ``!= 7`` is
re-hard-coded into ``services/post_render_timed.py:_validate_phrases``,
``test_non_seven_phrase_count_validates`` goes RED.
"""
import pytest

from services.post_render_timed import _validate_phrases, PostRenderError


def _phrases(count: int, duration_ms: int) -> list[dict]:
    step = duration_ms / count
    out = []
    for index in range(count):
        start = round(index * step)
        end = round((index + 1) * step) if index + 1 < count else duration_ms
        out.append({"startMs": start, "endMs": end, "text": f"phrase {index + 1}"})
    return out


def test_non_seven_phrase_count_validates():
    validated = _validate_phrases(_phrases(6, 15_000), 15_000)
    assert len(validated) == 6
    assert validated[-1]["endMs"] == 15_000


def test_shipped_seven_phrase_shape_still_validates():
    validated = _validate_phrases(_phrases(7, 20_000), 20_000)
    assert len(validated) == 7
    assert validated[-1]["endMs"] == 20_000


def test_empty_phrases_still_refused():
    with pytest.raises(PostRenderError):
        _validate_phrases([], 20_000)
