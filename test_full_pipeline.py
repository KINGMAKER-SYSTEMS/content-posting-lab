"""Test the full caption pipeline 3x with @beaujenkins — no browser, no server needed."""
import asyncio
import csv
from pathlib import Path


async def run_once(run_num: int, max_videos: int = 3):
    from scraper.frame_extractor import list_profile_videos, download_video, extract_frame
    from scraper.caption_extractor import extract_caption
    from dotenv import load_dotenv

    load_dotenv()
    profile = "https://www.tiktok.com/@beaujenkins"
    out = Path("caption_output/beaujenkins")
    frames = out / "frames"
    videos = out / "videos"
    frames.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  RUN {run_num}")
    print(f"{'='*60}")

    # Phase 1: list URLs
    print("[1] Listing videos...")
    urls = await list_profile_videos(profile, max_videos)
    print(f"    Found {len(urls)} URLs")
    if not urls:
        print("    FAIL — 0 URLs returned (rate limited?)")
        return False

    results = []

    # Phase 2: download + frame extract
    for i, url in enumerate(urls):
        vid = url.split("/video/")[-1] if "/video/" in url else f"v{i}"
        print(f"[2] Downloading {i+1}/{len(urls)}: {vid}")
        vpath = videos / f"{vid}.mp4"
        fpath = frames / f"{vid}.jpg"

        try:
            await download_video(url, vpath)
            await extract_frame(vpath, fpath, timestamp=2.0)
            results.append({"video_id": vid, "video_url": url, "frame": fpath})
            vpath.unlink(missing_ok=True)
        except Exception as e:
            print(f"    ERROR: {e}")
            results.append({"video_id": vid, "video_url": url, "frame": None, "error": str(e)})

    # Phase 3: GPT-4o vision
    for i, r in enumerate(results):
        if not r.get("frame"):
            r["caption"] = ""
            continue
        print(f"[3] GPT-4o caption {i+1}/{len(results)}: {r['video_id']}")
        try:
            caption = await extract_caption(r["frame"].read_bytes())
            r["caption"] = caption
            print(f"    Caption: {caption[:80]}{'...' if len(caption)>80 else ''}")
        except Exception as e:
            r["caption"] = ""
            r["error"] = str(e)
            print(f"    ERROR: {e}")

    # Write CSV
    csv_path = out / f"captions_run{run_num}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["video_id", "video_url", "caption"])
        w.writeheader()
        for r in results:
            w.writerow({"video_id": r["video_id"], "video_url": r["video_url"], "caption": r.get("caption", "")})

    print(f"\n    CSV written: {csv_path}")
    print(f"    RUN {run_num} — SUCCESS ({len(results)} videos)")
    return True


async def main():
    successes = 0
    for run in range(1, 4):
        ok = await run_once(run)
        if ok:
            successes += 1
        if run < 3:
            print("\n    Waiting 5s between runs...")
            await asyncio.sleep(5)

    print(f"\n{'='*60}")
    print(f"  RESULTS: {successes}/3 runs succeeded")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
