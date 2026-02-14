"""Re-run OCR only on already-extracted frames to test preprocessing changes."""
import asyncio
from pathlib import Path
from scraper.ocr_extractor import extract_caption_ocr


async def main():
    frames_dir = Path("caption_output/beaujenkins_test1/frames")
    frames = sorted(frames_dir.glob("*.jpg"))

    if not frames:
        print("No frames found. Run test_pipeline.py first.")
        return

    print(f"Found {len(frames)} frames. Re-running OCR...\n")

    for f in frames:
        print(f"── {f.name} ──")
        caption = await extract_caption_ocr(f)
        if caption:
            print(caption)
        else:
            print("(no caption detected)")
        print()


if __name__ == "__main__":
    asyncio.run(main())
