import asyncio
import base64
import csv
import random
import re
from pathlib import Path
from typing import Callable

from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

from scraper.caption_extractor import extract_caption

_stealth = Stealth()

# Callback signature: async fn(event: str, data: dict)
ProgressCB = Callable[[str, dict], None] | None


STORAGE_STATE_FILE = Path(__file__).parent.parent / "tiktok_auth.json"


async def _create_browser(headless: bool = True):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )

    # Load saved session if exists
    storage_state = None
    if STORAGE_STATE_FILE.exists():
        storage_state = str(STORAGE_STATE_FILE)

    context = await browser.new_context(
        viewport={"width": 430, "height": 932},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        storage_state=storage_state,
    )
    page = await context.new_page()
    await _stealth.apply_stealth_async(page)
    return pw, browser, context, page


async def login_and_save_session():
    """Open browser for manual TikTok login, then save cookies."""
    print("Opening browser - log into TikTok...")
    pw, browser, context, page = await _create_browser(headless=False)

    await page.goto("https://www.tiktok.com/login")

    # Wait until they're no longer on login page (meaning logged in)
    print("Waiting for login... (will auto-save when you're logged in)")
    for _ in range(300):  # 5 min max
        await asyncio.sleep(1)
        url = page.url
        if "/login" not in url and "tiktok.com" in url:
            await asyncio.sleep(2)  # Let cookies settle
            break

    # Save session
    await context.storage_state(path=str(STORAGE_STATE_FILE))
    print(f"Session saved to {STORAGE_STATE_FILE}")

    await context.close()
    await browser.close()
    await pw.stop()
    print("Done! You can now run the scraper.")


async def _human_delay(lo: float = 1.5, hi: float = 3.5):
    await asyncio.sleep(random.uniform(lo, hi))


async def _broadcast_frame(page: Page, on_progress: ProgressCB):
    if not on_progress:
        return
    try:
        frame_bytes = await page.screenshot(type="jpeg", quality=50)
        b64 = base64.b64encode(frame_bytes).decode()
        await on_progress("frame", {"b64": b64})
    except Exception as e:
        print(f"[frame] screenshot error: {e}")


async def _stream_loop(page: Page, on_progress: ProgressCB, stop: asyncio.Event):
    while not stop.is_set():
        await _broadcast_frame(page, on_progress)
        await asyncio.sleep(0.5)


def _extract_username(profile_url: str) -> str:
    m = re.search(r"@([\w.]+)", profile_url)
    return m.group(1) if m else "unknown"


def _normalize_profile_url(input_str: str) -> str:
    """Accept @username or full URL, return proper TikTok profile URL."""
    input_str = input_str.strip()
    if input_str.startswith("@"):
        return f"https://www.tiktok.com/{input_str}"
    if input_str.startswith("http"):
        return input_str
    # Assume it's just a username without @
    return f"https://www.tiktok.com/@{input_str}"


def _video_id(url: str) -> str:
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else "unknown"


# ── Collect video URLs ──────────────────────────────────────────────────

async def collect_video_urls(
    page: Page, profile_url: str, max_videos: int, sort: str = "latest",
) -> list[str]:
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(3)

    # Try to click the sort tab if sorting by popular
    if sort == "popular":
        try:
            # TikTok uses different selectors - try multiple approaches
            selectors = [
                'div[data-e2e="user-post-item-list"] ~ div span:text("Popular")',
                'span:text("Popular")',
                '[class*="TabItem"]:has-text("Popular")',
                'div[role="tab"]:has-text("Popular")',
            ]
            for sel in selectors:
                tab = await page.query_selector(sel)
                if tab:
                    await tab.click()
                    await asyncio.sleep(2)
                    break
        except Exception:
            pass  # fall back to default (latest)

    urls: list[str] = []
    seen: set[str] = set()
    stale_rounds = 0

    for _ in range(50):
        links = await page.query_selector_all('a[href*="/video/"]')
        for link in links:
            href = await link.get_attribute("href")
            if href and href not in seen:
                if href.startswith("/"):
                    href = "https://www.tiktok.com" + href
                seen.add(href)
                urls.append(href)

        if len(urls) >= max_videos:
            break

        prev_count = len(urls)
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        await _human_delay(1.5, 3.0)

        if len(urls) == prev_count:
            stale_rounds += 1
            if stale_rounds >= 3:
                break
        else:
            stale_rounds = 0

    return urls[:max_videos]


# ── Screenshot a single video ───────────────────────────────────────────

async def screenshot_video(page: Page, video_url: str) -> bytes:
    await page.goto(video_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(2)

    # Remove keyboard shortcuts modal from DOM entirely
    await page.evaluate("""
        document.querySelectorAll('[class*="keyboard"], [class*="Keyboard"], [class*="modal"], [class*="Modal"]').forEach(el => el.remove());
        document.querySelectorAll('div').forEach(el => {
            if (el.textContent && el.textContent.includes('keyboard shortcuts')) el.remove();
        });
    """)
    await asyncio.sleep(0.3)

    # Wait for video to load
    try:
        await page.wait_for_selector("video", timeout=10_000)
    except Exception:
        pass

    await asyncio.sleep(1)

    # Try to play and seek video
    await page.evaluate("""
        const v = document.querySelector('video');
        if (v) {
            v.muted = true;
            v.play().catch(() => {});
        }
    """)
    await asyncio.sleep(2)

    await page.evaluate("""
        const v = document.querySelector('video');
        if (v) {
            v.pause();
            v.currentTime = Math.min(2, v.duration || 2);
        }
    """)
    await asyncio.sleep(0.5)

    video_el = await page.query_selector("video")
    if video_el:
        return await video_el.screenshot(type="jpeg", quality=90)
    return await page.screenshot(type="jpeg", quality=90)


# ── Main orchestrator ────────────────────────────────────────────────────

async def scrape_profile_captions(
    profile_url: str,
    max_videos: int = 20,
    sort: str = "latest",
    output_dir: Path = Path("caption_output"),
    on_progress: ProgressCB = None,
) -> dict:
    """Scrape a TikTok profile, screenshot each video, extract captions.

    Accepts @username, username, or full URL.
    Saves screenshots to output_dir/<username>/screenshots/
    and a CSV to output_dir/<username>/captions.csv

    Returns {"folder": str, "csv": str, "results": list[dict]}
    """
    profile_url = _normalize_profile_url(profile_url)
    username = _extract_username(profile_url)
    job_dir = output_dir / username
    screenshots_dir = job_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    pw, browser, context, page = await _create_browser()
    stop_stream = asyncio.Event()

    try:
        stream_task = asyncio.create_task(
            _stream_loop(page, on_progress, stop_stream)
        )

        if on_progress:
            await on_progress("collecting", {"username": username})

        video_urls = await collect_video_urls(page, profile_url, max_videos, sort)

        if on_progress:
            await on_progress("urls_collected", {"count": len(video_urls)})

        results: list[dict] = []

        for i, url in enumerate(video_urls):
            vid = _video_id(url)
            try:
                if on_progress:
                    await on_progress("screenshotting", {
                        "index": i, "total": len(video_urls), "video_url": url,
                    })

                screenshot = await screenshot_video(page, url)

                # Save screenshot to disk
                img_path = screenshots_dir / f"{vid}.jpg"
                img_path.write_bytes(screenshot)

                await _human_delay(0.5, 1.5)

                if on_progress:
                    await on_progress("extracting", {
                        "index": i, "total": len(video_urls), "video_url": url,
                    })

                caption = await extract_caption(screenshot)

                results.append({
                    "video_url": url,
                    "video_id": vid,
                    "screenshot": str(img_path),
                    "caption": caption,
                    "error": None,
                })
            except Exception as e:
                results.append({
                    "video_url": url,
                    "video_id": vid,
                    "screenshot": None,
                    "caption": None,
                    "error": str(e),
                })

            if on_progress:
                await on_progress("video_done", {
                    "index": i, "total": len(video_urls), "result": results[-1],
                })

            await _human_delay(2.0, 4.0)

        # Write CSV
        csv_path = job_dir / "captions.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "video_id", "video_url", "caption", "screenshot", "error",
            ])
            writer.writeheader()
            writer.writerows(results)

        if on_progress:
            await on_progress("csv_written", {"path": str(csv_path)})

        return {
            "folder": str(job_dir),
            "csv": str(csv_path),
            "results": results,
        }

    finally:
        stop_stream.set()
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        await context.close()
        await browser.close()
        await pw.stop()
