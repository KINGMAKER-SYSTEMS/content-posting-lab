"""Creative per-cut transforms for clip-varying pages (Eric 2026-10-22).

"The footage can be iterated with the reverse etc to make it fresh and new."
After a varied cut is made (services/clip_variation.py: speed, crop, grade),
a cut may also be mirrored, given a slow camera move, played backwards, and
loop-swapped (second half moved to the front, Eric 2026-10-28).
Each is drawn per cut from the cut's own time frame, so a resumed job makes
the same choice, and a cut never repeats the previous cut's combination.

All four work on the finished short clip (5-10 s at 1080x1920), never on the
master, and none changes its length. They are recorded on the Lab clip
manifest (`clipTransforms`), not on the treatment receipt: the receipt schema
is closed on both sides and the final render never re-applies them, so the
Worker has nothing to attest.

Reverse is bounded in memory: ffmpeg's reverse filter holds every frame it is
given, so the picture is reversed in REVERSE_CHUNK_S pieces (about 30 decoded
frames, ~90 MB at 1080x1920) joined in reverse order, while the audio (a few
MB of PCM) is reversed as one stream, so it has no gaps at the joins. Every
re-encode keeps the colour tags the cut was written with.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

REVERSE_SHARE = 1 / 4
MIRROR_SHARE = 1 / 3
MOTION_SHARE = 1 / 3
# Eric's loop recipe (2026-10-28): "split it in half n move the second half to
# the front so it has a loop". Drawn per cut from the cut's own time frame.
LOOP_SWAP_SHARE = 1 / 3
MOTIONS = ("push_in", "pull_out", "pan_left", "pan_right")
# A slow move: at most 12 % of zoom (or a pan across a 10 % margin) over the
# whole clip, so it reads as an intentional camera move, not a jolt. Frame
# sizes and positions are whole pixels, so the move advances in 1-2 px steps.
MOTION_ZOOM = 0.12
PAN_MARGIN = 0.10
REVERSE_CHUNK_S = 1.0
MAX_DRAWS = 8
FFMPEG_TIMEOUT_S = 600

# Loop swap is opt-in per page and OFF by default. It is gated by its own
# allow-list, distinct from services/clip_variation.py CLIP_VARIATION_PAGES:
# no page gets a re-ordered cut until it is listed here. The list is env-driven
# for a gap-timed rollout (LOOP_SWAP_PAGES="acct:one,acct:two").
def _loop_swap_pages() -> frozenset[str]:
    raw = os.environ.get("LOOP_SWAP_PAGES", "")
    return frozenset(page.strip() for page in raw.split(",") if page.strip())


LOOP_SWAP_PAGES: frozenset[str] = _loop_swap_pages()


def loop_swap_enabled(page_id: str) -> bool:
    """Whether ``page_id`` may draw the loop-swap transform."""
    return page_id in LOOP_SWAP_PAGES


@dataclass(frozen=True)
class CutTransforms:
    mirror: bool = False
    motion: str | None = None
    reverse: bool = False
    loop_swap: bool = False

    def manifest(self) -> dict:
        return {"mirror": self.mirror, "motion": self.motion, "reverse": self.reverse,
                "loop_swap": self.loop_swap}

    def any(self) -> bool:
        return self.mirror or self.motion is not None or self.reverse or self.loop_swap


def _unit(seed: str, part: str) -> float:
    digest = hashlib.sha256(f"{seed}\0{part}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _draw(seed: str, loop_swap: bool = False) -> CutTransforms:
    motion = None
    if _unit(seed, "motion") < MOTION_SHARE:
        motion = MOTIONS[int(_unit(seed, "motion-kind") * len(MOTIONS))]
    return CutTransforms(
        mirror=_unit(seed, "mirror") < MIRROR_SHARE,
        motion=motion,
        reverse=_unit(seed, "reverse") < REVERSE_SHARE,
        loop_swap=loop_swap and _unit(seed, "loop_swap") < LOOP_SWAP_SHARE,
    )


def cut_transforms(page_id: str, cut_slot_id: str, previous: CutTransforms | None = None) -> CutTransforms:
    """The transforms for one cut; never the same combination as ``previous``.

    loop_swap is drawn only for pages on LOOP_SWAP_PAGES; for every other page
    the draw is byte-identical to the pre-loop-swap build (mirror, motion and
    reverse are seeded by their own parts and loop_swap stays off).
    """
    loop_swap = loop_swap_enabled(page_id)
    seed = f"{page_id}\0{cut_slot_id}\0transforms"
    for attempt in range(MAX_DRAWS):
        drawn = _draw(seed if attempt == 0 else f"{seed}#{attempt}", loop_swap)
        if drawn != previous:
            return drawn
    # Eight equal draws in a row is not reachable at these shares; flip the
    # mirror so the combination still differs.
    return CutTransforms(not drawn.mirror, drawn.motion, drawn.reverse, drawn.loop_swap)


def transforms_from_manifest(value) -> CutTransforms | None:
    if not isinstance(value, dict):
        return None
    motion = value.get("motion")
    return CutTransforms(bool(value.get("mirror")), motion if motion in MOTIONS else None,
                         bool(value.get("reverse")), bool(value.get("loop_swap")))


def _motion_graph(motion: str, duration_s: float, width: int, height: int) -> str:
    """A time-based move as a filtergraph from [0:v] to [m]; the frame keeps its size.

    A zoom scales every frame (eval=frame) and centres it on a fixed canvas by
    overlay, whose position is evaluated per frame (crop's is not: it keeps the
    first frame's width, which anchors the zoom to the corner). A pan scales
    once and slides a crop window across the margin.
    """
    progress = f"(t/{duration_s:.6f})"
    if motion in ("push_in", "pull_out"):
        factor = (f"(1+{MOTION_ZOOM}*{progress})" if motion == "push_in"
                  else f"(1+{MOTION_ZOOM}*(1-{progress}))")
        return (f"[0:v]scale=w='trunc({width}*{factor}/2)*2':h='trunc({height}*{factor}/2)*2'"
                f":eval=frame:flags=lanczos[zoomed];color=c=black:s={width}x{height}:r=30[canvas];"
                f"[canvas][zoomed]overlay=x='(W-w)/2':y='(H-h)/2':eval=frame:shortest=1[m]")
    scaled_w = int(round(width * (1 + PAN_MARGIN) / 2)) * 2
    scaled_h = int(round(height * (1 + PAN_MARGIN) / 2)) * 2
    x = (f"(iw-{width})*{progress}" if motion == "pan_right" else f"(iw-{width})*(1-{progress})")
    return (f"[0:v]scale={scaled_w}:{scaled_h}:flags=lanczos,"
            f"crop={width}:{height}:{x}:(ih-{height})/2[m]")


def _probe(path: Path) -> tuple[float, int, int, bool]:
    duration_s, width, height, has_audio, _ = _probe_full(path)
    return duration_s, width, height, has_audio


def _probe_full(path: Path) -> tuple[float, int, int, bool, list[str]]:
    """Duration, size, whether there is audio, and the clip's colour tags as
    encoder arguments, so a re-encode keeps the tags the cut was written with
    (an HDR-tagged cut stays HDR-tagged; untagged stays untagged)."""
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height,color_primaries,color_transfer,color_space",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=120)
    data = json.loads(completed.stdout)
    video = next(stream for stream in data["streams"] if stream.get("codec_type") == "video")
    has_audio = any(stream.get("codec_type") == "audio" for stream in data["streams"])
    color = []
    for key, option in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
                        ("color_space", "-colorspace")):
        value = video.get(key)
        if isinstance(value, str) and value and value != "unknown":
            color += [option, value]
    return float(data["format"]["duration"]), int(video["width"]), int(video["height"]), has_audio, color


def _probe_frames(path: Path, duration_s: float) -> tuple[int, float]:
    """The exact frame count and rate, so a loop split lands on a frame boundary."""
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames,r_frame_rate,avg_frame_rate",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=120)
    video = json.loads(completed.stdout)["streams"][0]
    fps = 30.0
    for key in ("r_frame_rate", "avg_frame_rate"):
        value = video.get(key)
        if isinstance(value, str) and "/" in value:
            numerator, _, denominator = value.partition("/")
            if denominator and float(denominator) > 0:
                fps = float(numerator) / float(denominator)
                break
    raw = video.get("nb_frames")
    if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
        return int(raw), fps
    return max(1, round(duration_s * fps)), fps


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args],
                   check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_S)


def _reverse_chunked(source: Path, destination: Path, duration_s: float, has_audio: bool,
                     encode_args: list[str], color: list[str], work: Path) -> None:
    """Video reversed in short pieces (bounded memory), audio reversed whole.

    Audio is small (seconds of PCM), so it is reversed as one stream and laid
    under the joined video: no per-piece audio priming gaps at the joins and
    no drift against the picture.
    """
    chunks: list[Path] = []
    start = 0.0
    index = 0
    while start < duration_s - 0.01:
        length = min(REVERSE_CHUNK_S, duration_s - start)
        chunk = work / f"reverse-{index:03d}.mp4"
        _run(["-ss", f"{start:.6f}", "-t", f"{length:.6f}", "-i", str(source),
              "-vf", "reverse", "-an", *_video_only(encode_args), *color, str(chunk)])
        chunks.append(chunk)
        start += length
        index += 1
    listing = work / "reverse.txt"
    listing.write_text("".join(f"file '{chunk.name}'\n" for chunk in reversed(chunks)))
    if not has_audio:
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
              "-movflags", "+faststart", str(destination)])
        return
    _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(source),
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", "areverse",
          *_audio_only(encode_args), "-shortest", "-movflags", "+faststart", str(destination)])


def _loop_swap(source: Path, destination: Path, n_frames: int, fps: float,
               has_audio: bool, encode_args: list[str], color: list[str]) -> None:
    """Second half then first half, split at the frame nearest the midpoint.

    Uses ffmpeg's ``trim``/``concat`` filters: each half is selected by frame
    index and streamed out, so the whole clip is never decoded into RAM (the
    same bounded-memory approach as reverse). Audio is split at the matching
    sample boundary (1600 samples per frame at 30 fps / 48 kHz) and concat'd
    in the same order, so it stays in step with the picture.
    """
    split = (n_frames + 1) // 2  # ceil(n/2): frame 0 of the result
    split_s = split / fps
    video = (f"[0:v]trim=start_frame={split}:end_frame={n_frames},setpts=PTS-STARTPTS[v2];"
             f"[0:v]trim=end_frame={split},setpts=PTS-STARTPTS[v1];"
             f"[v2][v1]concat=n=2:v=1:a=0[v]")
    if not has_audio:
        _run(["-i", str(source), "-filter_complex", video, "-map", "[v]",
              *_video_only(encode_args), *color, str(destination)])
        return
    audio = (f"[0:a]atrim=start={split_s:.6f},asetpts=PTS-STARTPTS[a2];"
             f"[0:a]atrim=end={split_s:.6f},asetpts=PTS-STARTPTS[a1];"
             f"[a2][a1]concat=n=2:v=0:a=1[a]")
    _run(["-i", str(source), "-filter_complex", video + ";" + audio,
          "-map", "[v]", "-map", "[a]",
          *_video_only(encode_args), *_audio_only(encode_args), *color, str(destination)])


def apply_transforms(path: Path, transforms: CutTransforms, encode_args: list[str]) -> None:
    """Rewrite one finished cut in place with its transforms (no-op when none).

    Fixed order (Eric 2026-10-28): the cut is already sped/cropped/graded, then
    loop_swap, then reverse (reversing a perfect loop is still a perfect loop),
    then mirror, then the slow camera move. When loop_swap is off (the default),
    the cut is re-encoded exactly as on main: mirror and the camera move are
    folded into one graph before reverse, so off-path output is byte-identical
    to the pre-loop-swap build for the same seeds.
    """
    if not transforms.any():
        return
    path = Path(path)
    work = path.parent / f".{path.stem}-transforms"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    try:
        duration_s, width, height, has_audio, color = _probe_full(path)
        current = path
        if transforms.loop_swap:
            # loop_swap is a re-order: frames (and the audio) are re-arranged,
            # which is a full re-encode before anything else touches the clip.
            n_frames, fps = _probe_frames(current, duration_s)
            swapped = work / "loop-swapped.mp4"
            _loop_swap(current, swapped, n_frames, fps, has_audio, encode_args, color)
            current = swapped
            if transforms.reverse:
                reversed_path = work / "reversed.mp4"
                _reverse_chunked(current, reversed_path, duration_s, has_audio, encode_args, color, work)
                current = reversed_path
            if transforms.mirror:
                mirrored = work / "mirrored.mp4"
                _run(["-i", str(current), "-vf", "hflip,setsar=1,format=yuv420p",
                      "-map", "0:v:0",
                      *(["-map", "0:a:0", "-c:a", "copy"] if has_audio else ["-an"]),
                      *_video_only(encode_args), *color, str(mirrored)])
                current = mirrored
            if transforms.motion is not None:
                moved = work / "moved.mp4"
                graph = _motion_graph(transforms.motion, duration_s, width, height)
                graph += ";[m]setsar=1,format=yuv420p[v]"
                _run(["-i", str(current), "-filter_complex", graph, "-map", "[v]",
                      *(["-map", "0:a", "-c:a", "copy"] if has_audio else ["-an"]),
                      *_video_only(encode_args), *color, str(moved)])
                current = moved
        else:
            # Off path (main's code): mirror and motion in one filtergraph, then
            # reverse, so a non-loop-swapped cut re-encodes at most twice and is
            # byte-identical to the pre-loop-swap build for the same seeds.
            if transforms.motion is not None or transforms.mirror:
                graph = (_motion_graph(transforms.motion, duration_s, width, height)
                         if transforms.motion is not None else "[0:v]null[m]")
                graph += f";[m]{'hflip,' if transforms.mirror else ''}setsar=1,format=yuv420p[v]"
                moved = work / "moved.mp4"
                _run(["-i", str(current), "-filter_complex", graph, "-map", "[v]",
                      *(["-map", "0:a", "-c:a", "copy"] if has_audio else ["-an"]),
                      *_video_only(encode_args), *color, str(moved)])
                current = moved
            if transforms.reverse:
                reversed_path = work / "reversed.mp4"
                _reverse_chunked(current, reversed_path, duration_s, has_audio, encode_args, color, work)
                current = reversed_path
        current.replace(path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _video_only(encode_args: list[str]) -> list[str]:
    """The preset's video settings; audio is stream-copied by the caller."""
    result: list[str] = []
    skip = False
    for index, argument in enumerate(encode_args):
        if skip:
            skip = False
            continue
        if argument.startswith("-c:a") or argument.startswith("-b:a") or argument in ("-ar", "-ac"):
            skip = index + 1 < len(encode_args) and not encode_args[index + 1].startswith("-")
            continue
        result.append(argument)
    return result


def _audio_only(encode_args: list[str]) -> list[str]:
    """The preset's audio settings (codec and bitrate), for the reversed audio."""
    result: list[str] = []
    for index, argument in enumerate(encode_args[:-1]):
        if argument.startswith("-c:a") or argument.startswith("-b:a") or argument in ("-ar", "-ac"):
            result += [argument, encode_args[index + 1]]
    return result or ["-c:a", "aac"]
