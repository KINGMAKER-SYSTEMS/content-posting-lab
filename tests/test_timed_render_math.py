"""Regression guard: the timed renderer's duration-derived delivery math (MED-1).

The three derivations (phrase PNG ``-t`` seconds, retry bitrate budget, expected
decoded frame count) were hard-coded to a 20s/600 shape and are now functions of
the timing master's ``durationMs``. They live in ``services/timed_render_math``
so they are runnable on the reduced review host (no PIL/pydantic/ffmpeg).

Re-hard-coding the 20s/600 shape in ``services/timed_render_math`` makes
``test_non_20s_duration_derives_non_default_values`` go RED.
"""
import ast
from pathlib import Path

from services.timed_render_math import (
    expected_decoded_frames,
    phrase_input_seconds,
    retry_bitrate_bps,
)

_POST_RENDER_SOURCE = Path(__file__).resolve().parents[1] / "services" / "post_render.py"


def _mult_int(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _mult_int(node.left) * _mult_int(node.right)
    raise AssertionError(f"MAX_FINAL_BYTES is not a compile-time int product: {ast.dump(node)}")


def _max_final_bytes() -> int:
    """Read MAX_FINAL_BYTES from post_render.py source (no PIL/pydantic import)."""
    tree = ast.parse(_POST_RENDER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "MAX_FINAL_BYTES" for t in node.targets)):
            return _mult_int(node.value)
    raise AssertionError("MAX_FINAL_BYTES not found in services/post_render.py")


# LOW-4: don't hard-code the byte cap; derive it from the production source so
# this test can never drift from the renderer's actual retry budget ceiling.
MAX_FINAL_BYTES = _max_final_bytes()


def test_shipped_20s_shape_still_derives_20s_and_600_frames():
    assert phrase_input_seconds(20_000) == 20.0
    assert expected_decoded_frames(20_000) == 600
    assert retry_bitrate_bps(MAX_FINAL_BYTES, 20_000) == int(
        MAX_FINAL_BYTES * 8 * 0.90 * 1000 / 20_000
    )


def test_non_20s_duration_derives_non_default_values():
    duration_ms = 15_000
    assert phrase_input_seconds(duration_ms) == 15.0
    assert expected_decoded_frames(duration_ms) == 450
    assert retry_bitrate_bps(MAX_FINAL_BYTES, duration_ms) == int(
        MAX_FINAL_BYTES * 8 * 0.90 * 1000 / duration_ms
    )
    # A shorter page has a higher per-second budget than the shipped 20s shape,
    # so the bitrate derivation is provably not the compiled-in 20s value.
    assert retry_bitrate_bps(MAX_FINAL_BYTES, duration_ms) > retry_bitrate_bps(MAX_FINAL_BYTES, 20_000)


def test_frame_count_truncates_at_fractional_seconds():
    assert expected_decoded_frames(20_033) == 600  # 600.99 frames -> 600
    assert expected_decoded_frames(33_333) == 999  # 999.99 frames -> 999
    assert expected_decoded_frames(1) == 0
