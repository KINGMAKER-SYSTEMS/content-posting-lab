"""Download TikTok videos with yt-dlp and extract frames with ffmpeg."""

import asyncio
import base64
import os
import shutil
import tempfile
from pathlib import Path

# ── Cookies support ──────────────────────────────────────────────────
# Resolution order (first hit wins):
#   1. YTDLP_COOKIES_FILE env var — explicit path to a cookies.txt
#   2. YTDLP_COOKIES env var — base64-encoded cookies.txt (decoded to temp file)
#   3. cookies.txt on the Railway volume (RAILWAY_VOLUME_MOUNT_PATH/cookies.txt)
#   4. cookies.txt in CWD (local dev)
_cookies_path: Path | None = None


def _volume_cookies_path() -> Path:
    """Path where uploaded cookies.txt is persisted on the Railway volume."""
    base = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
    if base and Path(base).exists():
        return Path(base) / "cookies.txt"
    return Path(__file__).resolve().parent.parent / "cookies.txt"


def get_cookies_path() -> Path | None:
    global _cookies_path
    if _cookies_path is not None and _cookies_path.exists():
        return _cookies_path
    _cookies_path = None

    explicit = os.getenv("YTDLP_COOKIES_FILE")
    if explicit:
        p = Path(explicit)
        if p.exists():
            _cookies_path = p
            return _cookies_path

    raw = os.getenv("YTDLP_COOKIES")
    if raw:
        try:
            data = base64.b64decode(raw)
            fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
            os.write(fd, data)
            os.close(fd)
            _cookies_path = Path(path)
            return _cookies_path
        except Exception:
            pass

    for candidate in (_volume_cookies_path(), Path("cookies.txt")):
        if candidate.exists():
            _cookies_path = candidate
            return _cookies_path

    return None


def reset_cookies_cache() -> None:
    """Drop the cached cookies path so the next call re-resolves from disk/env."""
    global _cookies_path
    _cookies_path = None


def _add_cookies(cmd: list[str]) -> list[str]:
    cp = get_cookies_path()
    if cp and cp.exists():
        cmd += ["--cookies", str(cp)]
    return cmd


def _check_deps():
    for cmd in ("yt-dlp", "ffmpeg"):
        if not shutil.which(cmd):
            raise RuntimeError(
                f"{cmd} not found on PATH. Install it first:\n"
                f"  brew install {cmd}   (macOS)\n"
                f"  pip install {cmd}    (yt-dlp only)"
            )


async def _list_profile_videos_with_playwright(
    profile_url: str, max_videos: int, sort: str
) -> list[str]:
    from scraper.tiktok_scraper import _create_browser, collect_video_urls

    pw = None
    browser = None
    context = None
    try:
        pw, browser, context, page = await _create_browser(headless=True)
        return await collect_video_urls(page, profile_url, max_videos, sort=sort)
    finally:
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        if pw is not None:
            await pw.stop()


async def list_profile_videos(
    profile_url: str, max_videos: int = 20, sort: str = "latest"
) -> list[str]:
    _check_deps()

    if sort == "popular":
        try:
            urls = await _list_profile_videos_with_playwright(
                profile_url, max_videos, sort
            )
            if urls:
                return urls[:max_videos]
        except Exception as e:
            print(f"[frame_extractor] Playwright fallback failed: {e}", flush=True)

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--playlist-end",
        str(max_videos),
        "--print",
        "webpage_url",
        profile_url,
    ]
    cmd = _add_cookies(cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # yt-dlp can't list TikTok profiles directly anymore — fall back to Playwright
        print(
            f"[frame_extractor] yt-dlp listing failed, trying Playwright fallback",
            flush=True,
        )
        try:
            urls = await _list_profile_videos_with_playwright(
                profile_url, max_videos, sort
            )
            if urls:
                return urls[:max_videos]
        except Exception as pw_err:
            print(
                f"[frame_extractor] Playwright fallback also failed: {pw_err}",
                flush=True,
            )
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"yt-dlp listing failed: {err[-300:]}")
    urls = [
        line.strip() for line in stdout.decode().strip().splitlines() if line.strip()
    ]
    return urls[:max_videos]


async def get_thumbnail(video_url: str, dest: Path) -> Path:
    """Get the TikTok video's cover/thumbnail image. Much faster than downloading the full video."""
    _check_deps()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Use yt-dlp's built-in thumbnail download — it handles cookies/headers properly
    # Output template without extension; yt-dlp adds the actual extension
    thumb_base = dest.with_suffix("")
    cmd = [
        "yt-dlp",
        "--no-download",
        "--no-warnings",
        "--no-check-certificates",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "-o", f"thumbnail:{thumb_base}",
        video_url,
    ]
    cmd = _add_cookies(cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("yt-dlp thumbnail timed out after 30s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"yt-dlp thumbnail failed: {stderr.decode(errors='replace')[-200:]}"
        )

    # yt-dlp may write as dest (without ext) or with .jpg extension
    if dest.exists():
        return dest
    jpg_path = dest.with_suffix(".jpg")
    if jpg_path.exists():
        jpg_path.rename(dest)
        return dest
    # Search for any file yt-dlp wrote with matching stem
    for variant in dest.parent.glob(f"{dest.stem}*"):
        if variant.is_file():
            variant.rename(dest)
            return dest

    # Fallback: try the old urllib approach
    cmd2 = [
        "yt-dlp",
        "--no-download",
        "--no-warnings",
        "--no-check-certificates",
        "--print", "thumbnail",
        video_url,
    ]
    cmd2 = _add_cookies(cmd2)
    proc2 = await asyncio.create_subprocess_exec(
        *cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc2.kill()
        await proc2.communicate()
        raise RuntimeError("yt-dlp thumbnail URL lookup timed out")
    thumb_url = stdout2.decode().strip()
    if thumb_url:
        import urllib.request
        import functools
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, functools.partial(urllib.request.urlopen, thumb_url, timeout=15)
            )
            dest.write_bytes(data.read())
            return dest
        except Exception as e:
            raise RuntimeError(f"Thumbnail download failed: {e}")

    raise RuntimeError("No thumbnail downloaded")


def _in_container() -> bool:
    """True when running on Railway (or any container without a desktop browser).

    Browser-cookie extraction (`--cookies-from-browser`) can never work there: no
    Chrome/Firefox profile exists on disk. Attempting it burns a subprocess per
    browser and — worse — fails with messages containing the word "cookies",
    which used to be misread as "your cookies.txt was rejected".
    """
    return bool(
        os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RAILWAY_SERVICE_ID")
        or Path("/.dockerenv").exists()
    )


# yt-dlp prints an interpreter-deprecation banner to stderr on old Pythons, e.g.
# "Deprecated Feature: Support for Python version 3.10 has been deprecated".
# It is not a download failure, but when it rode along in the surfaced error it
# read to users as "the clipper is deprecated". Strip it from user-facing text.
_NOISE_PREFIXES = (
    "deprecated feature:",
    "warning:",
    "please update to python",
)


def _clean_stderr(raw: str) -> str:
    """Drop yt-dlp banner noise, keep the lines that explain the failure."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(line.lower().startswith(pfx) for pfx in _NOISE_PREFIXES):
            continue
        lines.append(line)
    return " ".join(lines) if lines else raw.strip()


def _classify(err: str) -> str:
    """Bucket a yt-dlp stderr message: 'auth', 'env', or 'other'.

    'env' means yt-dlp could not read a *browser* cookie store — a property of
    this machine, not of the user's credentials. Keeping it distinct from 'auth'
    is what stops a container's five failed browser probes from being reported
    as "your cookies.txt is expired".
    """
    low = err.lower()
    if "could not find" in low and "cookies" in low:
        return "env"
    if "unsupported platform" in low or "cookies database" in low:
        return "env"
    if (
        "sign in" in low
        or "authenticat" in low
        or "login required" in low
        or "private video" in low
        or "cookies" in low
    ):
        return "auth"
    return "other"


async def download_video(
    video_url: str, dest: Path, cookies_file: Path | None = None
) -> Path:
    """Download a video using yt-dlp. Returns path to the mp4.

    Tries strategies in order: explicit cookies file, cookies.txt from the
    volume/env, then (only off-container) browser cookies, then no auth.
    TikTok/YouTube increasingly require cookies to download.

    On failure the raised error reports the *cookie-backed* attempt when there
    was one, not merely whichever strategy ran last — the no-auth attempt is
    always the least informative, and reporting it hid the real reason.
    """
    _check_deps()
    dest.parent.mkdir(parents=True, exist_ok=True)

    base_cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "-f",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(dest),
        "--no-check-certificates",
    ]

    # Build auth strategies in order of preference.
    strategies: list[tuple[str, list[str]]] = []
    cookies_source: str | None = None
    if cookies_file and cookies_file.exists():
        strategies.append(("cookies-file", ["--cookies", str(cookies_file)]))
        cookies_source = "explicit cookies_file arg"
    env_cookies = get_cookies_path()
    if env_cookies is not None and env_cookies.exists():
        strategies.append(("cookies-from-env", ["--cookies", str(env_cookies)]))
        if cookies_source is None:
            cookies_source = str(env_cookies)
    if not _in_container():
        for browser in ("chrome", "safari", "firefox", "edge", "brave"):
            strategies.append(
                (f"cookies-from-{browser}", ["--cookies-from-browser", browser])
            )
    strategies.append(("no-auth", []))

    # (label, cleaned_error, kind) for every strategy that failed.
    failures: list[tuple[str, str, str]] = []

    for label, extra in strategies:
        cmd = base_cmd + extra + [video_url]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode == 0:
            if dest.exists():
                return dest
            for variant in dest.parent.glob(f"{dest.stem}*"):
                return variant
            failures.append((label, "completed but output not found", "other"))
            continue

        err = _clean_stderr(stderr.decode(errors="replace"))
        kind = _classify(err)
        failures.append((label, err, kind))
        if kind == "other":
            # A non-auth failure (404, geo-block, 403, unsupported URL). Other
            # auth strategies cannot help, so stop burning subprocesses.
            break

    # Report the most informative attempt: a cookie-backed one if we made any,
    # otherwise the last thing we tried.
    cookie_failures = [f for f in failures if f[0].startswith("cookies-") and f[2] != "env"]
    label, err, _kind = (cookie_failures or failures)[-1] if failures else ("", "", "other")

    saw_real_auth_error = any(f[2] == "auth" for f in failures)
    low = err.lower()

    if "403" in low or "forbidden" in low:
        hint = (
            " A 403 from a datacenter IP usually means the platform blocked the "
            "server itself, not you. Valid, fresh cookies for that platform are "
            "the only reliable fix — re-export cookies.txt while logged in and "
            "upload it via POST /api/clipper/cookies. If cookies are already "
            "fresh, download the file locally and use the upload box instead."
        )
    elif saw_real_auth_error and cookies_source is None:
        hint = (
            " No cookies configured. On Railway, upload a cookies.txt via "
            "POST /api/clipper/cookies (or set YTDLP_COOKIES_FILE / YTDLP_COOKIES). "
            "See yt-dlp FAQ for exporting cookies."
        )
    elif saw_real_auth_error and cookies_source:
        hint = (
            f" Cookies were tried from {cookies_source} but yt-dlp still rejected them — "
            "they may be expired, or be for a different platform than this link. "
            "Re-export a fresh cookies.txt while logged in."
        )
    else:
        hint = ""

    tried = ", ".join(f[0] for f in failures)
    raise RuntimeError(
        f"yt-dlp failed: {label}: {err[-300:]}{hint} (tried: {tried})"
    )


async def extract_frame(
    video_path: Path,
    output_path: Path,
    timestamp: float = 2.0,
) -> Path:
    """Extract a single frame from a video at the given timestamp."""
    _check_deps()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        str(timestamp),
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(output_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg frame extract failed: {err[-300:]}")

    if not output_path.exists():
        raise RuntimeError(f"ffmpeg completed but frame not found at {output_path}")

    return output_path


async def extract_multiple_frames(
    video_path: Path,
    output_dir: Path,
    timestamps: tuple[float, ...] = (1.0, 3.0, 5.0),
) -> list[Path]:
    """Extract frames at multiple timestamps. Returns list of frame paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for ts in timestamps:
        out = output_dir / f"frame_{ts:.1f}s.jpg"
        try:
            await extract_frame(video_path, out, timestamp=ts)
            frames.append(out)
        except RuntimeError:
            pass  # Video shorter than this timestamp
    return frames
