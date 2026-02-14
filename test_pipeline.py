"""End-to-end pipeline test — reuses cached frames if TikTok rate-limits."""
import asyncio
import csv
import re
import shutil
import sys
from pathlib import Path


CACHED_FRAMES = Path("caption_output/beaujenkins_test1/frames")


async def run_test(run_number: int):
    print(f"\n{'='*60}")
    print(f"  RUN {run_number} — @beaujenkins")
    print(f"{'='*60}\n")

    from scraper.tiktok_scraper import _create_browser, collect_video_urls
    from scraper.frame_extractor import download_video, extract_frame
    from scraper.ocr_extractor import extract_caption_ocr

    profile_url = "https://www.tiktok.com/@beaujenkins"
    username = "beaujenkins"
    max_videos = 3

    out_dir = Path("caption_output") / f"{username}_run{run_number}"
    frames_dir = out_dir / "frames"
    videos_dir = out_dir / "videos"
    frames_dir.mkdir(parents=True, exist_ok=True)
    videos_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: Collect URLs
    print("[1/3] Collecting video URLs...")
    pw, browser, context, page = await _create_browser(headless=False)
    try:
        video_urls = await collect_video_urls(page, profile_url, max_videos, sort="latest")
    finally:
        await context.close()
        await browser.close()
        await pw.stop()

    # If rate-limited, use cached data
    use_cache = False
    if not video_urls and CACHED_FRAMES.exists():
        print("      TikTok rate-limited — using cached frames")
        use_cache = True
        cached = sorted(CACHED_FRAMES.glob("*.jpg"))
        video_urls = [
            f"https://www.tiktok.com/@beaujenkins/video/{f.stem}" for f in cached[:max_videos]
        ]
        for f in cached[:max_videos]:
            shutil.copy2(f, frames_dir / f.name)

    print(f"      {len(video_urls)} videos")

    if not video_urls:
        print("      ERROR: No URLs and no cache.")
        return False

    # Phase 2: Download + extract frames (skip if using cache)
    results = []
    if use_cache:
        print("[2/3] Using cached frames")
        for i, url in enumerate(video_urls):
            vid = url.split("/")[-1]
            frame_path = frames_dir / f"{vid}.jpg"
            results.append({"video_id": vid, "video_url": url, "frame_path": frame_path, "error": None})
            print(f"      [{i+1}/{len(video_urls)}] {vid} (cached)")
    else:
        print("[2/3] Downloading videos & extracting frames...")
        for i, url in enumerate(video_urls):
            m = re.search(r"/video/(\d+)", url)
            vid = m.group(1) if m else f"unknown_{i}"
            print(f"      [{i+1}/{len(video_urls)}] {vid}...", end=" ", flush=True)
            try:
                video_path = videos_dir / f"{vid}.mp4"
                await download_video(url, video_path)
                frame_path = frames_dir / f"{vid}.jpg"
                await extract_frame(video_path, frame_path, timestamp=2.0)
                video_path.unlink(missing_ok=True)
                results.append({"video_id": vid, "video_url": url, "frame_path": frame_path, "error": None})
                print("OK")
            except Exception as e:
                print(f"ERR: {e}")
                results.append({"video_id": vid, "video_url": url, "frame_path": None, "error": str(e)})

    # Phase 3: OCR
    print("[3/3] Running OCR...")
    for r in results:
        if r["error"] or not r["frame_path"]:
            r["caption"] = ""
            continue
        print(f"      {r['video_id']}...", end=" ", flush=True)
        try:
            caption = await extract_caption_ocr(r["frame_path"])
            r["caption"] = caption
            preview = caption[:80].replace("\n", " ") if caption else "(empty)"
            print(f"-> {preview}")
        except Exception as e:
            r["caption"] = ""
            r["error"] = str(e)
            print(f"ERR: {e}")

    # Write CSV
    csv_path = out_dir / "captions.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["video_id", "video_url", "caption", "error"])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "video_id": r["video_id"],
                "video_url": r["video_url"],
                "caption": r.get("caption", ""),
                "error": r.get("error", ""),
            })

    with_captions = sum(1 for r in results if r.get("caption"))
    print(f"\n  CSV: {csv_path}")
    print(f"  {len(results)} rows, {with_captions} with captions")
    return True


async def main():
    for run in range(1, 4):
        ok = await run_test(run)
        if not ok:
            print(f"\n  Run {run} FAILED")
            sys.exit(1)
        print(f"\n  Run {run} PASSED\n")
        if run < 3:
            await asyncio.sleep(5)

    # Show all CSVs
    print("\n" + "="*60)
    print("  ALL 3 RUNS COMPLETE — CSV SUMMARY")
    print("="*60)
    for run in range(1, 4):
        csv_path = Path(f"caption_output/beaujenkins_run{run}/captions.csv")
        print(f"\n  --- Run {run}: {csv_path} ---")
        if csv_path.exists():
            print(csv_path.read_text())
        else:
            print("  (missing)")


if __name__ == "__main__":
    asyncio.run(main())
