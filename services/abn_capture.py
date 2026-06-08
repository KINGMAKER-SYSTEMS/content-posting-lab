"""
AgenticBuilderNews — DYNAMIC UI CAPTURE.

Playwright opens a real URL (a repo, a tool's page, docs) and RECORDS it being
navigated — scrolling, panning, the page coming alive — instead of a dead
screenshot. This is the 'demo it live' moat shot.

Output: a 1920x1080 webm/mp4 of the page being scrolled, saved to the assets dir.
Runs in a thread (sync Playwright) so it never blocks the event loop.
"""
from __future__ import annotations
import os
from pathlib import Path

_VOL = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
_BASE = Path(_VOL) if _VOL and Path(_VOL).exists() else Path(__file__).resolve().parent.parent
ASSETS = _BASE / "agenticnews_assets"
ASSETS.mkdir(parents=True, exist_ok=True)


def capture_sync(url: str, name: str, seconds: float = 8.0) -> str | None:
    """Open url, scroll it smoothly top→bottom while recording. Returns /agenticnews-assets/<name>_ui.mp4 or None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    rec_dir = ASSETS / f"_rec_{name}"
    rec_dir.mkdir(exist_ok=True)
    out_mp4 = ASSETS / f"{name}_ui.mp4"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--hide-scrollbars", "--disable-gpu"])
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                record_video_dir=str(rec_dir),
                record_video_size={"width": 1920, "height": 1080},
                device_scale_factor=1,
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1200)  # let it settle / images load
            # bot-wall detection — Cloudflare/captcha pages are worthless footage, bail out
            try:
                txt = (page.inner_text("body") or "").lower()[:600]
            except Exception:
                txt = ""
            bot_signals = ("verify you are human", "performing security verification",
                           "checking your browser", "are you a robot", "enable javascript and cookies",
                           "ddos protection by", "just a moment",
                           # a bare Cloudflare "Verifying..." challenge widget slipped through (caught on
                           # a real frame: a 'Verifying... CLOUDFLARE' bot-wall rendered as the b-roll).
                           "verifying...", "verifying you are", "needs to review the security",
                           "cloudflare", "cf-challenge", "cf_chl", "ray id", "attention required")
            # ERROR-PAGE detection — a 504/502/timeout/down page is worthless footage and was leaking
            # into episodes as slop (a '504 Gateway Time-out' frame appeared at the open). Bail the
            # same way so the pipeline falls back to a designed card.
            error_signals = ("504 gateway", "502 bad gateway", "503 service", "gateway time-out",
                             "gateway timeout", "didn't respond in time", "did not respond in time",
                             "this page isn't working", "took too long to respond", "site can't be reached",
                             "page not found", "404 not found", "internal server error", "error 500",
                             "temporarily unavailable", "origin server")
            # NEAR-EMPTY page: a bot-wall/challenge often renders its content in an iframe, leaving the
            # body with almost no text — that's worthless footage (a blank/near-blank scroll). Bail.
            near_empty = len(txt.strip()) < 40
            if near_empty or any(sig in txt for sig in bot_signals) or any(sig in txt for sig in error_signals):
                ctx.close(); browser.close()
                for f in rec_dir.glob("*"):
                    try: f.unlink()
                    except Exception: pass
                try: rec_dir.rmdir()
                except Exception: pass
                return None  # let the pipeline fall back to screenshot/Flux b-roll
            # smooth scroll top → bottom over ~seconds
            height = page.evaluate("document.body.scrollHeight") or 2000
            steps = max(20, int(seconds * 12))
            for i in range(steps):
                y = int((i / steps) * max(0, height - 1080))
                page.evaluate(f"window.scrollTo({{top:{y},behavior:'instant'}})")
                page.wait_for_timeout(int(seconds * 1000 / steps))
            page.wait_for_timeout(600)
            ctx.close()  # finalizes the webm
            browser.close()
        # find the produced webm, transcode to mp4 (h264) for Remotion
        webms = list(rec_dir.glob("*.webm"))
        if not webms:
            return None
        src = webms[0]
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vf", "scale=1920:1080", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-an", str(out_mp4)],
            capture_output=True, timeout=120)
        # cleanup
        for f in rec_dir.glob("*"):
            try: f.unlink()
            except Exception: pass
        try: rec_dir.rmdir()
        except Exception: pass
        if out_mp4.exists():
            # BLANK-PAGE guard: a page that rendered with NO visible content (blank scroll) is worthless
            # footage the text-based bot-wall check misses. CRITICAL: use the DARKEST region, not the
            # average — most real pages have a WHITE background (GitHub, docs), so avg brightness is high
            # even when they're full of dark text. A truly blank page has no dark pixels ANYWHERE; a page
            # with text/UI has dark regions. Bail only if even the darkest 20x20 cell is near-white (>=248)
            # AND the variance is tiny (flat). (An earlier avg>=240 check wrongly bailed real white pages.)
            try:
                import subprocess as _sp
                probe = _sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(out_mp4)], capture_output=True, text=True, timeout=20)
                mid = max(0.5, (float(probe.stdout.strip() or 1)) / 2)
                raw = _sp.run(["ffmpeg", "-v", "error", "-ss", str(mid), "-i", str(out_mp4),
                               "-frames:v", "1", "-vf", "scale=24:24,format=gray", "-f", "rawvideo", "-"],
                              capture_output=True, timeout=20).stdout
                if raw:
                    vals = list(raw)
                    darkest = min(vals)          # a page with ANY text/UI has a dark cell here
                    spread = max(vals) - darkest
                    if darkest >= 248 and spread <= 6:   # flat near-white everywhere = genuinely blank
                        out_mp4.unlink()
                        return None
            except Exception:
                pass   # if the check itself fails, keep the clip (don't drop good footage on a probe error)
            return f"/agenticnews-assets/{out_mp4.name}"
    except Exception:
        return None
    return None
