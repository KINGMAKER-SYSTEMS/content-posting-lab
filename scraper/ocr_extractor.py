"""Local OCR caption extraction using Tesseract — no API keys or PyTorch needed."""

import asyncio
import functools
import re
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps
import pytesseract


def _crop_caption_region(img: Image.Image) -> Image.Image:
    """Crop to the center region where TikTok burned-in captions appear.

    Captions sit roughly in the middle 60% of the frame vertically,
    avoiding the top status bar and bottom UI (username, sounds, etc).
    """
    w, h = img.size
    top = int(h * 0.15)
    bottom = int(h * 0.75)
    return img.crop((0, top, w, bottom))


def _preprocess(img: Image.Image) -> Image.Image:
    """Enhance image for better OCR on burned-in TikTok captions.

    TikTok captions are typically large white text with dark outlines/shadows
    on a busy video background. We isolate bright white text aggressively.
    """
    # Crop to caption region first
    img = _crop_caption_region(img)

    # Convert to grayscale
    gray = ImageOps.grayscale(img)

    # Scale up — Tesseract works better at higher res
    w, h = gray.size
    if w < 1500:
        scale = 2000 / w
        gray = gray.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Sharpen to crisp up text edges
    gray = gray.filter(ImageFilter.SHARPEN)
    gray = gray.filter(ImageFilter.SHARPEN)

    # Aggressive binarize: only keep very bright pixels (white caption text)
    # Threshold 210 is high enough to reject most background but keep white text
    gray = gray.point(lambda p: 255 if p > 210 else 0)

    # Median filter to remove salt-and-pepper noise
    gray = gray.filter(ImageFilter.MedianFilter(size=3))

    return gray


def _clean_ocr_text(text: str) -> str:
    """Clean up Tesseract output: remove noise lines, fix common artifacts."""
    lines = []
    for line in text.strip().splitlines():
        line = line.strip()
        # Skip empty / very short lines (noise)
        if len(line) < 3:
            continue
        # Skip lines that are mostly punctuation/symbols (background noise)
        alpha_chars = sum(1 for c in line if c.isalpha())
        if alpha_chars < len(line) * 0.4:
            continue
        # Skip lines that are just repeated characters
        if len(set(line.replace(" ", ""))) < 3:
            continue
        lines.append(line)

    result = "\n".join(lines)

    # Collapse multiple spaces
    result = re.sub(r"  +", " ", result)

    return result.strip()


def _ocr_sync(image_path: str) -> str:
    """Run Tesseract OCR on a single image. Returns extracted text."""
    img = Image.open(image_path)
    processed = _preprocess(img)

    # PSM 6 = assume uniform block of text (good for captions)
    # OEM 3 = default LSTM engine
    config = "--psm 6 --oem 3"
    text = pytesseract.image_to_string(processed, config=config)

    return _clean_ocr_text(text)


async def extract_caption_ocr(image_path: Path) -> str:
    """Async wrapper — runs Tesseract in a thread to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(_ocr_sync, str(image_path)),
    )
