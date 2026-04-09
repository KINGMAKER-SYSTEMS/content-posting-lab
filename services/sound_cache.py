"""Global sound cache for beat-synced slideshows.

Flow:
    1. User picks a campaign sound (from telegram sounds list)
    2. Match label → Campaign Hub campaign → get matched_videos + sound_id
    3. Download the highest-viewed creator video via yt-dlp
    4. Extract audio with ffmpeg → MP3
    5. Run librosa beat detection → {bpm, duration, beats}
    6. Cache MP3 + beats JSON under projects/sounds/{tiktok_sound_id}.*
    7. Subsequent requests for the same TikTok sound_id (across R1/R2/R3)
       hit the cache instantly.
"""

import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from project_manager import get_global_sounds_dir
from services.campaign_hub import fetch_campaign_detail, find_campaign_by_label

log = logging.getLogger("sound_cache")

# Per-sound lock to avoid concurrent prepares downloading the same sound twice
_prepare_locks: dict[str, asyncio.Lock] = {}


async def prepare_sound(telegram_sound_id: str, label: str) -> dict:
    """Resolve a telegram sound → Campaign Hub video → audio + beats.

    Args:
        telegram_sound_id: The 12-char hex ID from the telegram sounds list.
        label: The human-readable label (e.g. "Bella Kay - Wonder Wander").
            Must match a Campaign Hub campaign title.

    Returns dict with: sound_id, telegram_sound_id, cached, bpm, duration,
    beats, source_video_url, label, slug.

    Raises:
        ValueError: if no Campaign Hub match or no matched videos.
        RuntimeError: if yt-dlp/ffmpeg/librosa fail.
    """
    # Step 1: Match label → campaign
    campaign = await find_campaign_by_label(label)
    if not campaign:
        raise ValueError(f"No Campaign Hub match for sound label: {label}")

    slug = campaign["slug"]

    # Step 2: Fetch full campaign detail (includes matched_videos + sound_id)
    detail = await fetch_campaign_detail(slug)
    tiktok_sound_id = detail.get("sound_id") or detail.get("official_sound")
    if not tiktok_sound_id or str(tiktok_sound_id) == "-":
        raise ValueError(f"Campaign {slug} has no TikTok sound_id")
    tiktok_sound_id = str(tiktok_sound_id)

    sounds_dir = get_global_sounds_dir()
    audio_path = sounds_dir / f"{tiktok_sound_id}.mp3"
    beats_path = sounds_dir / f"{tiktok_sound_id}.beats.json"

    # Serialize concurrent prepares for the same sound
    lock = _prepare_locks.setdefault(tiktok_sound_id, asyncio.Lock())
    async with lock:
        # Cache hit check (inside the lock to avoid races)
        if audio_path.exists() and beats_path.exists():
            log.info(
                "sound cache hit: sound_id=%s label=%s",
                tiktok_sound_id, label,
            )
            data = json.loads(beats_path.read_text())
            return {
                "sound_id": tiktok_sound_id,
                "telegram_sound_id": telegram_sound_id,
                "cached": True,
                **data,
            }

        # Step 3: Pick best video (highest views)
        videos = [v for v in detail.get("matched_videos", []) if v.get("url")]
        if not videos:
            raise ValueError(
                f"Campaign '{slug}' has no matched videos with URLs yet. "
                f"Creators need to post content first."
            )
        videos.sort(key=lambda v: v.get("views", 0) or 0, reverse=True)
        best = videos[0]
        video_url = best["url"]
        log.info(
            "preparing sound: sound_id=%s label=%s video=%s views=%s",
            tiktok_sound_id, label, video_url, best.get("views", 0),
        )

        # Step 4 + 5: Download video + extract audio (blocking, run in thread)
        await asyncio.to_thread(_download_and_extract, video_url, audio_path)

        # Step 6: Beat detection (CPU-heavy, run in thread)
        beats_data = await asyncio.to_thread(_analyze_beats, audio_path)

        # Step 7: Save sidecar metadata
        metadata = {
            **beats_data,
            "source_video_url": video_url,
            "label": label,
            "slug": slug,
        }
        beats_path.write_text(json.dumps(metadata, indent=2))
        log.info(
            "sound cached: sound_id=%s bpm=%.1f duration=%.1fs beats=%d",
            tiktok_sound_id, beats_data["bpm"], beats_data["duration"],
            len(beats_data["beats"]),
        )

        return {
            "sound_id": tiktok_sound_id,
            "telegram_sound_id": telegram_sound_id,
            "cached": False,
            **metadata,
        }


def _download_and_extract(video_url: str, output_mp3: Path) -> None:
    """Download TikTok video via yt-dlp, then extract audio with ffmpeg.

    Blocking — caller should run in asyncio.to_thread().
    """
    with tempfile.TemporaryDirectory(prefix="sound_dl_") as tmp:
        tmp_path = Path(tmp)
        tmp_video = tmp_path / "video"  # no ext; yt-dlp adds one

        # yt-dlp video download (same flags as scraper/frame_extractor.py)
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--no-check-certificates",
            "-f", "bv*+ba/b",
            "-o", f"{tmp_video}.%(ext)s",
            video_url,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"yt-dlp failed: {stderr}")

        # Find the actual downloaded file (extension varies)
        video_files = [p for p in tmp_path.iterdir() if p.stem == "video"]
        if not video_files:
            raise RuntimeError("yt-dlp produced no output file")
        tmp_video_actual = video_files[0]

        # ffmpeg audio extract → MP3 (high quality)
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(tmp_video_actual),
            "-vn",
            "-acodec", "libmp3lame",
            "-q:a", "2",  # VBR quality 2 (~190 kbps)
            str(output_mp3),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"ffmpeg audio extract failed: {stderr}")


def _analyze_beats(audio_path: Path) -> dict:
    """librosa beat detection. CPU-bound — caller should run in thread."""
    import numpy as np
    import librosa
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    # librosa's tempo return type is ndarray in 0.10+, scalar in older versions
    bpm = float(np.asarray(tempo).flatten()[0]) if tempo is not None else 0.0
    return {
        "duration": float(len(y) / sr),
        "bpm": bpm,
        "beats": beat_times,
    }
