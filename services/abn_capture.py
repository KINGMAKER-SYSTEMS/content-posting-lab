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
import re
from pathlib import Path

from services.abn_assets import asset_path_from_slug, asset_url_from_slug
from services.fsutil import safe_rmtree


def _re_i(label: str):
    """Case-insensitive whole-label regex for Playwright get_by_role(name=…)."""
    return re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I)


def _cleanup_dir(path: Path) -> None:
    """Best-effort remove a temp recording dir (contents and all).

    Delegates to ``fsutil.safe_rmtree``, which never raises and logs on failure
    instead of swallowing it. This runs in destructive bail/finally paths where
    leaving an orphaned _rec_<name>/ dir is the only stake, so a missing dir or a
    locked file must never propagate.
    """
    safe_rmtree(path)

_VOL = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
_BASE = Path(_VOL) if _VOL and Path(_VOL).exists() else Path(__file__).resolve().parent.parent
ASSETS = _BASE / "agenticnews_assets"
ASSETS.mkdir(parents=True, exist_ok=True)


def capture_sync(url: str, name: str, seconds: float = 8.0) -> str | None:
    """Open url, scroll it smoothly top→bottom while recording. ``name`` is the flat factory
    slug (``{ep_id}_s{N}``); the asset is written through the abn_assets gateway to the
    episode-scoped ``{ep_id}/footage/s{N}_ui.mp4``. Returns its /agenticnews-assets/ URL or None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    # Route the final asset through the abn_assets GATEWAY — it validates ep_id/kind and
    # enforces the per-episode {ep_id}/footage/s{N}_ui.mp4 layout. A flat ASSETS/{name}_ui.mp4
    # write silently re-introduces the orphan-file blast radius the schema exists to prevent.
    out_mp4 = asset_path_from_slug(name, "ui")  # .../{ep_id}/footage/s{N}_ui.mp4 (dir created)
    # keep the Playwright recording temp dir episode-scoped too (sibling of the final mp4),
    # so no flat _rec_<name>/ dir lands at the unmanaged ASSETS root.
    rec_dir = out_mp4.parent / f"_rec_{name}"
    rec_dir.mkdir(exist_ok=True)
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
            # DISMISS cookie/consent banners — they're UI clutter in the b-roll (caught on a real frame:
            # an 'ACCEPT ALL / REJECT ALL' bar at the bottom of a captured page). Click a consent button
            # to clear it before recording. Try common labels; privacy-preserving order (reject first).
            for _label in ("Reject all", "Reject", "Decline", "Decline all", "Only necessary",
                           "Accept all", "Accept", "I agree", "Got it", "OK"):
                try:
                    btn = page.get_by_role("button", name=_re_i(_label)).first
                    if btn.is_visible(timeout=400):
                        btn.click(timeout=600); page.wait_for_timeout(300); break
                except Exception:
                    continue
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
                _cleanup_dir(rec_dir)
                return None  # let the pipeline fall back to screenshot/Flux b-roll
            # smooth scroll over ~seconds — but only through the TOP, READABLE portion of the page
            # (repo name, hero description, first section). Scrolling the FULL page on a long README
            # drags through walls of tiny unreadable body text (caught on a real frame). Cap the depth
            # to ~2.2 viewports so the shot stays on the legible hero/intro, not deep body paragraphs.
            full = page.evaluate("document.body.scrollHeight") or 2000
            height = min(full, int(1080 * 2.2))
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
        _cleanup_dir(rec_dir)
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
                    mean = sum(vals) / len(vals)
                    # BAIL if the page is washed-out near-white. Two cases, both = worthless footage:
                    #  (a) flat near-white everywhere (darkest>=248, spread<=6) — a pure blank page; OR
                    #  (b) overall near-white with only faint content (mean>=248) — caught on a real frame:
                    #      a clip at darkest=244/spread=11/mean=255 looked blank. A REAL light page (GitHub
                    #      with code) has mean far lower (~120) because dark text drags it down — so a
                    #      mean>=248 cutoff catches washed-out pages without touching legible light pages.
                    if (darkest >= 248 and spread <= 6) or mean >= 248:
                        out_mp4.unlink()
                        return None
            except Exception:
                pass   # if the check itself fails, keep the clip (don't drop good footage on a probe error)
            return asset_url_from_slug(name, "ui")  # /agenticnews-assets/{ep_id}/footage/s{N}_ui.mp4
    except Exception:
        return None
    finally:
        # ALWAYS remove the playwright recording temp dir — multiple bail paths (no-webm, blank-guard,
        # outer except) previously left empty _rec_<name>/ dirs orphaned on the volume (caught in an SOP
        # sweep: 4 empty _rec_ dirs accumulated). finally guarantees cleanup regardless of exit path.
        _cleanup_dir(rec_dir)
    return None
