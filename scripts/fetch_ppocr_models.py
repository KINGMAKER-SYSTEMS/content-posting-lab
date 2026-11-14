#!/usr/bin/env python3
"""Fetch PP-OCRv6 small models (det/rec/dict) and verify pinned SHA-256.

Production never downloads at request time — this is the separate operator step
that puts the verified model files on disk before ``LAB_OCR_ENGINE=ppocr`` can
build its engine. Model URLs and SHA-256 pins come from
``services/ocr_engine.py`` (the same bytes Syzygy's oar-ocr door pinned).

Usage:
  python scripts/fetch_ppocr_models.py [target_dir]
  LAB_OCR_MODEL_DIR=/app/ocr-models python scripts/fetch_ppocr_models.py
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import ocr_engine as ocr  # noqa: E402

FILES = [
    (ocr.DET_URL, ocr.DET_NAME, ocr.DET_SHA256),
    (ocr.REC_URL, ocr.REC_NAME, ocr.REC_SHA256),
    (ocr.DICT_URL, ocr.DICT_NAME, ocr.DICT_SHA256),
]


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ocr.model_dir()
    target.mkdir(parents=True, exist_ok=True)
    for url, name, expected in FILES:
        dest = target / name
        if dest.is_file() and ocr._sha256_hex(dest) == expected:
            print(f"ok   {name} (already present, sha256 verified)")
            continue
        print(f"fetch {name} <- {url}")
        with urllib.request.urlopen(url, timeout=600) as response:
            data = response.read()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise SystemExit(f"sha256 mismatch for {name}: expected {expected}, got {actual}")
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        print(f"ok   {name}")
    print(f"models ready under {target}")


if __name__ == "__main__":
    main()
