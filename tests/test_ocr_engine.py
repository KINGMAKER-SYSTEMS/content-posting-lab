"""Tests for the pluggable OCR engine (LAB_OCR_ENGINE) and its visual-admission wiring.

The default engine is tesseract and must remain byte-identical; ppocr is opt-in.
No model files or GPU are required — recognition-rule tests use a fake engine,
and routing tests import the module in a subprocess with the env set.
"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from services import ocr_engine

_REPO = str(Path(__file__).resolve().parents[1])


# ---- model verification ----

def test_models_available_is_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(ocr_engine.MODEL_DIR_ENV, str(tmp_path))
    assert ocr_engine.models_available() is False
    with pytest.raises(RuntimeError, match="ocr_model_missing"):
        ocr_engine.verify_models()


def test_verify_models_happy_path_and_bad_digest(tmp_path, monkeypatch):
    monkeypatch.setenv(ocr_engine.MODEL_DIR_ENV, str(tmp_path))
    data = b"hello ppocr"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(ocr_engine, "DET_SHA256", digest)
    monkeypatch.setattr(ocr_engine, "REC_SHA256", digest)
    monkeypatch.setattr(ocr_engine, "DICT_SHA256", digest)
    for name in (ocr_engine.DET_NAME, ocr_engine.REC_NAME, ocr_engine.DICT_NAME):
        (tmp_path / name).write_bytes(data)
    ocr_engine.verify_models()  # no raise
    assert ocr_engine.models_available() is True

    (tmp_path / ocr_engine.DET_NAME).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="sha256_mismatch"):
        ocr_engine.verify_models()


# ---- recognition rule (identical to tesseract word rule) ----

class _FakeResult:
    def __init__(self, boxes, txts, scores):
        self.boxes = boxes
        self.txts = txts
        self.scores = scores


def _fake_engine(results):
    class Engine:
        def __call__(self, image):
            return results.pop(0)
    return Engine()


def test_recognize_applies_confidence_and_alnum_rule(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_load_engine", lambda: _fake_engine([
        _FakeResult(boxes=[[None]], txts=("EXISTING TEXT", "AI", "24", "i", "@"),
                    scores=(0.95, 0.81, 0.90, 0.99, 0.99)),
    ]))
    assert ocr_engine.recognize(b"\0" * (2 * 2 * 3), 2, 2) == "EXISTING TEXT AI 24"


def test_recognize_rejects_low_confidence_and_short_runs(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_load_engine", lambda: _fake_engine([
        _FakeResult(boxes=[[None]], txts=("lowcontrast", "i", "@"),
                    scores=(0.58, 0.95, 0.95)),
    ]))
    assert ocr_engine.recognize(b"\0" * 12, 2, 2) == ""


def test_recognize_handles_empty_detection(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_load_engine", lambda: _fake_engine([
        _FakeResult(boxes=None, txts=None, scores=None),
    ]))
    assert ocr_engine.recognize(b"\0" * 12, 2, 2) == ""


def test_recognize_caps_at_500_chars(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_load_engine", lambda: _fake_engine([
        _FakeResult(boxes=[[None]], txts=("W" * 900,), scores=(0.99,)),
    ]))
    assert len(ocr_engine.recognize(b"\0" * 12, 2, 2)) == 500


def test_recognize_deadline_expired_fails_closed():
    with pytest.raises(RuntimeError, match="scan_timeout"):
        ocr_engine.recognize(b"\0" * 12, 2, 2, deadline=time.monotonic() - 1)


def test_recognize_slow_inference_is_bounded_by_deadline(monkeypatch):
    def slow(frame, width, height):
        time.sleep(0.5)
        return "LATE"

    monkeypatch.setattr(ocr_engine, "_recognize_inner", slow)
    with pytest.raises(RuntimeError, match="scan_timeout"):
        ocr_engine.recognize(b"\0" * 12, 2, 2, deadline=time.monotonic() + 0.05)


def test_recognize_with_deadline_returns_before_deadline(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_recognize_inner", lambda frame, width, height: "OK")
    assert ocr_engine.recognize(b"\0" * 12, 2, 2, deadline=time.monotonic() + 1) == "OK"


def test_missing_models_map_to_known_transient_reason(monkeypatch, tmp_path):
    monkeypatch.setenv(ocr_engine.MODEL_DIR_ENV, str(tmp_path))
    assert ocr_engine.models_available() is False
    with pytest.raises(RuntimeError, match="ocr_or_decoder_unavailable"):
        ocr_engine._load_engine()


# ---- env routing (imported fresh in a subprocess) ----

def _import_gate(env):
    code = textwrap.dedent("""
        import json
        from services import visual_admission as gate
        d = gate.pending_decision(page_id="acct:t", job_id="j", index=0, sha256="a"*64, byte_count=1)
        print(json.dumps({"engine": gate.ocr_engine.ENGINE,
                          "algorithm": gate.ALGORITHM,
                          "ocr_engine": d["ocr"]["engine"],
                          "ocr": d["ocr"]}))
    """)
    proc = subprocess.run([sys.executable, "-c", code], cwd=_REPO, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_default_engine_is_tesseract_byte_identical():
    from services import visual_admission as gate
    assert gate.ALGORITHM == "tesseract-psm12-words-vision-corroborated-v6"
    d = gate.pending_decision(page_id="acct:t", job_id="j", index=0, sha256="a" * 64, byte_count=1)
    assert d["ocr"]["engine"] == "tesseract"
    assert d["ocr"]["psm"] == 12
    assert d["ocr"]["languages"] == ["eng", "osd"]
    assert d["ocr"]["orientations"] == [0, 180]
    assert d["ocr"]["minimumWordConfidence"] == 80


def test_ppocr_env_selects_ppocr_engine_and_algorithm():
    env = {**os.environ, "LAB_OCR_ENGINE": "ppocr"}
    out = _import_gate(env)
    assert out["engine"] == "ppocr"
    assert out["algorithm"] == "pp-ocrv6-small-vision-corroborated-v1"
    assert out["ocr_engine"] == "pp-ocrv6-small"
    assert out["ocr"]["orientations"] == [0]
    assert out["ocr"]["minimumConfidence"] == 0.80
    assert "psm" not in out["ocr"]


def test_unknown_engine_fails_closed():
    env = {**os.environ, "LAB_OCR_ENGINE": "pytesseract"}
    proc = subprocess.run([sys.executable, "-c", "from services import visual_admission"],
                          cwd=_REPO, env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "LAB_OCR_ENGINE" in proc.stderr
