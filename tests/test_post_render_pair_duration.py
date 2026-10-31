"""Admission of separately encoded clean/burned pairs without rerendering."""
import json

import pytest

from tests.test_post_render_dual_dedup import dual_render, payload
from tests.test_post_render_jobs import service


def pair_renderer(*, clean_ms=867, burned_ms=866, clean_frames=26,
                  burned_frames=26, corrupt=None):
    def render(source, output, request, **kwargs):
        dual_render(source, output, request, **kwargs)
        for directory, duration, frames in (
            (output, clean_ms, clean_frames),
            (output / "burned", burned_ms, burned_frames),
        ):
            receipt = json.loads((directory / "receipt.json").read_text())
            receipt["duration_ms"] = duration
            (directory / "receipt.json").write_text(json.dumps(receipt))
            decode = json.loads((directory / "decode.json").read_text())
            decode["final_probe"]["duration_ms"] = duration
            decode["decoded_video_frames"] = frames
            if directory == output / "burned" and corrupt:
                corrupt(decode)
            (directory / "decode.json").write_text(json.dumps(decode))
    return render


@pytest.mark.parametrize("burned_ms", [866, 867, 868])
def test_equal_frame_pair_preserves_clean_duration_and_both_artifacts(tmp_path, burned_ms):
    worker = service(tmp_path, renderer=pair_renderer(burned_ms=burned_ms))
    job = worker.enqueue(payload("slot:pair", "caption"), "request:pair")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "succeeded"
    receipt_bytes = worker.artifact(job["id"], "receipt").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["duration_ms"] == 867
    assert worker.artifact(job["id"], "burned_final").read_bytes() == b"burned:caption"
    # Restart reads the same bound receipt and retained decode evidence.
    resumed = service(tmp_path, renderer=lambda *_a, **_k: pytest.fail("must not rerender"))
    assert resumed.status(job["id"])["state"] == "succeeded"
    assert resumed.artifact(job["id"], "receipt").read_bytes() == receipt_bytes


def test_exact_duration_keeps_existing_independent_positive_frame_counts(tmp_path):
    worker = service(tmp_path, renderer=pair_renderer(burned_ms=867, burned_frames=27))
    job = worker.enqueue(payload("slot:exact", "caption"), "request:exact")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "succeeded"


@pytest.mark.parametrize("overrides", [
    {"burned_ms": 865}, {"burned_ms": 869},
    {"clean_ms": 1, "burned_ms": True},
    {"clean_ms": float("inf"), "burned_ms": float("inf")},
    {"clean_frames": float("inf"), "burned_frames": float("inf")},
    *({"burned_ms": value} for value in (None, "866", True, 0, -1, float("nan"), float("inf"))),
    *({"clean_ms": value} for value in (None, "867", True, 0, -1, float("nan"), float("inf"))),
    {"burned_frames": 25}, {"clean_frames": 25},
    *({field: value} for field in ("burned_frames", "clean_frames")
      for value in (None, "26", True, 0, -1, float("nan"), float("inf"))),
])
def test_quantization_exception_refuses_unproved_duration_or_frames(tmp_path, overrides):
    worker = service(tmp_path, renderer=pair_renderer(**overrides))
    job = worker.enqueue(payload("slot:bad", "caption"), "request:bad")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("final_sha256", "a" * 64), ("qa_frame_sha256", "b" * 64),
    ("width", 720), ("video_codec", "hevc"),
])
def test_one_millisecond_pair_still_requires_bound_bytes_and_geometry(tmp_path, field, value):
    def corrupt(decode):
        target = decode["final_probe"] if field in ("width", "video_codec") else decode
        target[field] = value
    worker = service(tmp_path, renderer=pair_renderer(corrupt=corrupt))
    job = worker.enqueue(payload("slot:corrupt", "caption"), "request:corrupt")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "failed"
