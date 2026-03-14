"""Download TikTok videos with yt-dlp and extract frames with ffmpeg."""

import asyncio
import base64
import os
import shutil
import tempfile
from pathlib import Path

# ── Cookies support ──────────────────────────────────────────────────
# If YTDLP_COOKIES env var is set (base64-encoded Netscape cookies.txt),
# decode it to a temp file on first access.
_cookies_path: Path | None = None


def get_cookies_path() -> Path | None:
    global _cookies_path
    if _cookies_path is not None:
        return _cookies_path if _cookies_path.exists() else None
    raw = os.getenv("YTDLP_COOKIES")
    if not raw:
        # Also check for a plain file at a known path
        fallback = Path("cookies.txt")
        if fallback.exists():
            _cookies_path = fallback
            return _cookies_path
        return None
    try:
        data = base64.b64decode(raw)
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
        os.write(fd, data)
        os.close(fd)
        _cookies_path = Path(path)
        return _cookies_path
    except Exception:
        return None


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
    stdout, stderr = await proc.communicate()
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
    stdout2, _ = await proc2.communicate()
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


async def download_video(
    video_url: str, dest: Path, cookies_file: Path | None = None
) -> Path:
    """Download a TikTok video using yt-dlp. Returns path to the mp4."""
    _check_deps()
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
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
    if cookies_file and cookies_file.exists():
        cmd += ["--cookies", str(cookies_file)]
    else:
        cmd = _add_cookies(cmd)
    cmd.append(video_url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"yt-dlp failed ({proc.returncode}): {err[-300:]}")

    if dest.exists():
        return dest
    for variant in dest.parent.glob(f"{dest.stem}*"):
        return variant
    raise RuntimeError(f"yt-dlp completed but output not found at {dest}")


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
