"""Pluggable OCR engine for pre-caption visual admission.

Default engine is ``tesseract`` — the existing subprocess path, byte-identical to
before. Setting ``LAB_OCR_ENGINE=ppocr`` selects PP-OCRv6 small (ONNX) via
``rapidocr`` + ``onnxruntime``, using model files verified against pinned SHA-256
and never downloaded at request time.

Models are fetched/verified separately (``scripts/fetch_ppocr_models.py``), or
pointed at an existing copy with ``LAB_OCR_MODEL_DIR``. The pins are the exact
bytes Syzygy's ``oar-ocr`` door verified
(``syzygy-adm/crates/visual-admission/src/models.rs``), which on our labelled
frames beat Tesseract (71/72 vs 38/72, 0 vs 6 false alarms) — see LABOCR.md.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

_ALLOWED = {"tesseract", "ppocr"}
ENGINE = os.environ.get("LAB_OCR_ENGINE", "tesseract").strip().lower()
if ENGINE not in _ALLOWED:
    raise ValueError(f"LAB_OCR_ENGINE must be one of {sorted(_ALLOWED)}, got {ENGINE!r}")

# Same text rule as the Lab's tesseract word rule (visual_admission.py): a
# recognized region is positive evidence when its confidence is >= 80 (%) and
# its string has >= 2 alphanumeric characters.
OCR_WORD_CONFIDENCE = 80
OCR_MIN_ALNUM = 2

# PP-OCRv6 small ONNX model pins (Syzygy visual-admission crate models.rs).
DET_NAME = "pp-ocrv6_small_det.onnx"
REC_NAME = "pp-ocrv6_small_rec.onnx"
DICT_NAME = "ppocrv6_dict.txt"
DET_SHA256 = "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e"
REC_SHA256 = "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634"
DICT_SHA256 = "b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d"

MODEL_DIR_ENV = "LAB_OCR_MODEL_DIR"

DET_URL = "https://github.com/GreatV/oar-ocr/releases/download/v0.7.0/pp-ocrv6_small_det.onnx"
REC_URL = "https://github.com/GreatV/oar-ocr/releases/download/v0.7.0/pp-ocrv6_small_rec.onnx"
DICT_URL = "https://github.com/GreatV/oar-ocr/releases/download/v0.7.0/ppocrv6_dict.txt"


def model_dir() -> Path:
    configured = os.environ.get(MODEL_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".lab-ocr-models"


def det_path() -> Path:
    return model_dir() / DET_NAME


def rec_path() -> Path:
    return model_dir() / REC_NAME


def dict_path() -> Path:
    return model_dir() / DICT_NAME


def _sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_models() -> None:
    """Raise if any pinned model file is missing or hashes differently."""
    for path, expected in (
        (det_path(), DET_SHA256),
        (rec_path(), REC_SHA256),
        (dict_path(), DICT_SHA256),
    ):
        if not path.is_file():
            raise RuntimeError(f"ocr_model_missing {path.name}")
        if _sha256_hex(path) != expected:
            raise RuntimeError(f"ocr_model_sha256_mismatch {path.name}")


def models_available() -> bool:
    try:
        verify_models()
        return True
    except RuntimeError:
        return False


_engine = None
_engine_lock = threading.Lock()


def _load_engine():
    """Build the PP-OCRv6 small engine once, verified against pinned SHA-256.

    Model loading and first-inference session init are paid here (under the lock)
    so the per-frame path carries no lazy-load surprise. A missing/mismatched
    model maps to the Lab's known transient reason, not a raw traceback.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    verify_models()
                except RuntimeError:
                    raise RuntimeError("ocr_or_decoder_unavailable") from None
                try:
                    import numpy as np
                    from rapidocr import RapidOCR
                except ImportError:
                    raise RuntimeError("ocr_or_decoder_unavailable") from None
                _engine = RapidOCR(params={
                    "Global.use_cls": False,
                    "Det.model_path": str(det_path()),
                    "Rec.model_path": str(rec_path()),
                    "Rec.rec_keys_path": str(dict_path()),
                    # Tuned detector thresholds: defaults (0.3/0.5) miss one
                    # low-contrast positive the Tesseract baseline catches.
                    "Det.thresh": 0.2,
                    "Det.box_thresh": 0.3,
                })
                # Burn first-inference model/session init on a tiny image.
                _engine(np.zeros((64, 64, 3), dtype=np.uint8))
    return _engine


def _alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def recognize(frame: bytes, width: int, height: int) -> str:
    """Return positive-evidence text for one decoded RGB frame, or "".

    Mirrors the tesseract word rule exactly: a region is positive evidence when
    its confidence >= OCR_WORD_CONFIDENCE and its string has >= OCR_MIN_ALNUM
    alphanumerics. Positive regions are joined into one space-separated string
    capped at 500 chars (same shape as ``_recognized_words``).
    """
    import numpy as np
    from PIL import Image

    engine = _load_engine()
    image = np.asarray(Image.frombytes("RGB", (width, height), frame))
    result = engine(image)
    words = []
    if result.boxes is not None and result.txts is not None and result.scores is not None:
        for text, confidence in zip(result.txts, result.scores):
            text = text or ""
            if float(confidence) * 100 >= OCR_WORD_CONFIDENCE and _alnum_count(text) >= OCR_MIN_ALNUM:
                words.append(text)
    return " ".join(words)[:500]
