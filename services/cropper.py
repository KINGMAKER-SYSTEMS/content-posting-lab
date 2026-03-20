from pathlib import Path
from PIL import Image

TARGET_RATIO = 9 / 16


def crop_to_916(input_path: Path, output_path: Path) -> Path:
    """Center-crop an image to 9:16 aspect ratio and scale to 1080x1920."""
    img = Image.open(input_path)
    w, h = img.size

    if w / h > TARGET_RATIO:
        new_w = int(h * TARGET_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / TARGET_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    img = img.resize((1080, 1920), Image.LANCZOS)
    img.save(output_path, quality=92)
    return output_path
