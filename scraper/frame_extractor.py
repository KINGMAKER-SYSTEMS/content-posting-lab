"""Download TikTok videos with yt-dlp and extract frames with ffmpeg."""

import asyncio
import shutil
from pathlib import Path


def _check_deps():
    for cmd in ("yt-dlp", "ffmpeg"):
        if not shutil.which(cmd):
            raise RuntimeError(
                f"{cmd} not found on PATH. Install it first:\n"
                f"  brew install {cmd}   (macOS)\n"
                f"  pip install {cmd}    (yt-dlp only)"
            )


async def list_profile_videos(profile_url: str, max_videos: int = 20) -> list[str]:
    """Use yt-dlp --flat-playlist to list video URLs from a TikTok profile. No browser needed."""
    _check_deps()
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--no-warnings",
        "--no-check-certificates",
        "--playlist-end", str(max_videos),
        "--print", "webpage_url",
        profile_url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"yt-dlp listing failed: {err[-300:]}")
    urls = [line.strip() for line in stdout.decode().strip().splitlines() if line.strip()]
    return urls[:max_videos]


async def get_thumbnail(video_url: str, dest: Path) -> Path:
    """Get the TikTok video's cover/thumbnail image. Much faster than downloading the full video."""
    _check_deps()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Get thumbnail URL from yt-dlp
    cmd = ["yt-dlp", "--no-download", "--no-warnings", "--no-check-certificates",
           "--print", "thumbnail", video_url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp thumbnail failed: {stderr.decode(errors='replace')[-200:]}")
    thumb_url = stdout.decode().strip()
    if not thumb_url:
        raise RuntimeError("No thumbnail URL returned")

    # Download the thumbnail image (in thread to avoid blocking event loop)
    import urllib.request
    import functools
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            None, functools.partial(urllib.request.urlopen, thumb_url, timeout=15))
        dest.write_bytes(data.read())
    except Exception as e:
        raise RuntimeError(f"Thumbnail download failed: {e}")
    return dest


async def download_video(video_url: str, dest: Path, cookies_file: Path | None = None) -> Path:
    """Download a TikTok video using yt-dlp. Returns path to the mp4."""
    _check_deps()
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "-f", "mp4",
        "-o", str(dest),
        "--no-check-certificates",
    ]
    if cookies_file and cookies_file.exists():
        cmd += ["--cookies", str(cookies_file)]
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
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
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
    timestamps: list[float] = (1.0, 3.0, 5.0),
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
