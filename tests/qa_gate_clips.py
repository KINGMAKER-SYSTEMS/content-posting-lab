"""Tiny synthetic bodies for the QA gate tests (ffmpeg lavfi; no fixtures on disk).

Every builder writes h264/yuv420p mp4 at the given frame and rate. The
faults are the ones the lead caught by hand on 11-18: non-square SAR, a scene
cut inside the last frames, a near-black run, a freeze, a wrong length, baked
audio, a wrong frame, a truncated file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SIZE = "1080x1920"
FPS = 30


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-v", "error", *args],
                   check=True, capture_output=True, text=True, timeout=300)


def _encode(path: Path, graph: str, *, frames: int | None = None, audio: bool = False,
            fps: int = FPS, extra: list[str] | None = None) -> Path:
    args = ["-f", "lavfi", "-i", graph]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000"]
    args += ["-map", "0:v:0"]
    if audio:
        args += ["-map", "1:a:0", "-c:a", "aac", "-shortest"]
    if frames is not None:
        args += ["-frames:v", str(frames)]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(fps),
             "-movflags", "+faststart", *(extra or []), str(path)]
    _run(args)
    return path


def _src(seconds: float, *, size: str = SIZE, fps: int = FPS, seed: int = 0) -> str:
    # testsrc2 is a moving pattern: never black, never frozen, no scene cuts.
    return f"testsrc2=size={size}:rate={fps}:duration={seconds:.4f}"


def _solid(seconds: float, color: str, *, size: str = SIZE, fps: int = FPS) -> str:
    return f"color=c={color}:size={size}:rate={fps}:duration={seconds:.4f}"


def _concat(*segments: str) -> str:
    labels = "".join(f"[s{index}]" for index in range(len(segments)))
    body = ";".join(f"{segment}[s{index}]" for index, segment in enumerate(segments))
    return f"{body};{labels}concat=n={len(segments)}:v=1:a=0"


def clean(path: Path, seconds: float = 6.0, *, size: str = SIZE, fps: int = FPS,
          audio: bool = False) -> Path:
    """A body with nothing wrong: moving picture, exact length, video-only."""
    return _encode(path, _src(seconds, size=size, fps=fps), frames=round(seconds * fps),
                   audio=audio, fps=fps)


def with_tail_cut(path: Path, seconds: float = 6.0, *, frames_before_end: int = 2) -> Path:
    """A hard scene cut ``frames_before_end`` frames before the last frame."""
    total = round(seconds * FPS)
    # lavfi emits every frame whose pts is under the duration; the half frame
    # keeps float rounding from adding one more, so the cut lands exactly
    # ``frames_before_end`` frames before the last one.
    head = (total - frames_before_end - 0.5) / FPS
    graph = _concat(_src(head), _solid(frames_before_end / FPS + 0.5, "red"))
    return _encode(path, graph, frames=total)


def with_black_run(path: Path, seconds: float = 6.0, *, black_s: float = 0.5, at_s: float = 3.0) -> Path:
    graph = _concat(_src(at_s), _solid(black_s, "black"), _src(seconds - at_s - black_s + 0.5))
    return _encode(path, graph, frames=round(seconds * FPS))


def with_freeze(path: Path, seconds: float = 6.0, *, freeze_s: float = 1.0, at_s: float = 3.0) -> Path:
    graph = _concat(_src(at_s), _solid(freeze_s, "gray"), _src(seconds - at_s - freeze_s + 0.5))
    return _encode(path, graph, frames=round(seconds * FPS))


def montage(path: Path, *, cards: int, card_s: float, last_card_s: float) -> Path:
    """Flip cards (alternating colours) ending on a short last card."""
    colours = ["red", "blue", "green", "yellow", "magenta", "cyan", "orange", "white"]
    segments = [_solid(card_s, colours[index % len(colours)]) for index in range(cards)]
    segments.append(_solid(last_card_s + 0.5, "gray"))
    total = round((cards * card_s + last_card_s) * FPS)
    return _encode(path, _concat(*segments), frames=total)


def non_square_sar(source: Path, destination: Path, sar: str = "2272/2277", dar: str = "142:253") -> Path:
    """The AIW fault: the same frames declared with a non-square SAR in both
    the SPS (bitstream filter) and the mp4 pasp box (-aspect)."""
    _run(["-i", str(source), "-map", "0", "-c", "copy",
          "-bsf:v", f"h264_metadata=sample_aspect_ratio={sar}", "-aspect", dar, str(destination)])
    return destination


def truncated(source: Path, destination: Path, keep: float = 0.6) -> Path:
    data = source.read_bytes()
    destination.write_bytes(data[: int(len(data) * keep)])
    return destination


def framemd5(path: Path) -> str:
    completed = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", str(path),
                                "-map", "0:v:0", "-f", "framemd5", "-"],
                               check=True, capture_output=True, text=True, timeout=300)
    return "\n".join(line for line in completed.stdout.splitlines() if not line.startswith("#"))


def _scene_graph(width: int, height: int, seconds: float, fps: int = FPS, seed: int = 0) -> str:
    """A stand-in outdoor scene: bright sky fading to dark ground (a
    top-to-bottom brightness axis) with a few horizon/roofline bands (row
    edges) and gentle texture. Upright, its brightness axis is vertical and
    its edge energy sits in rows, like every upright PAID-GEN-1119 body."""
    bands = ",".join(
        f"drawbox=x=0:y={int(height * frac)}:w={width}:h={max(2, height // 60)}:color=black@0.8:t=fill"
        for frac in (0.55, 0.68, 0.8)
    )
    return (f"gradients=size={width}x{height}:rate={fps}:duration={seconds:.4f}"
            f":c0=white:c1=0x303030:x0={width // 2}:y0=0:x1={width // 2}:y1={height - 1}:speed=0.0,"
            f"{bands},noise=alls=6:allf=t:all_seed={1000 + seed}")


def upright_scene(path: Path, seconds: float = 6.0, *, size: str = SIZE, seed: int = 0) -> Path:
    """``seed`` varies the texture so sibling bodies are distinct bytes (the
    executor's duplicate-output guard refuses identical clips)."""
    width, height = (int(part) for part in size.split("x"))
    return _encode(path, _scene_graph(width, height, seconds, seed=seed), frames=round(seconds * FPS))


def sideways_scene(path: Path, seconds: float = 6.0, *, size: str = SIZE, seed: int = 0) -> Path:
    """The Joy eba568ef fault: a landscape-composed scene turned a quarter
    turn inside the upright portrait frame, no rotate tag anywhere."""
    width, height = (int(part) for part in size.split("x"))
    graph = _scene_graph(height, width, seconds, seed=seed) + ",transpose=1"
    return _encode(path, graph, frames=round(seconds * FPS))


def with_display_rotation(source: Path, destination: Path, degrees: int = 90) -> Path:
    """The same bytes with a display-matrix rotate tag (a phone-style file)."""
    _run(["-display_rotation", str(degrees), "-i", str(source), "-map", "0", "-c", "copy", str(destination)])
    return destination


def dark_room_side_window(path: Path, seconds: float = 6.0, *, size: str = SIZE, fps: int = FPS) -> Path:
    """GLM's upright false-positive: a dark room with a bright window filling
    the left third (a one-sided light, held composition). Upright, yet its
    brightness axis runs left-right and the window edge is a vertical step."""
    width, height = (int(part) for part in size.split("x"))
    graph = (f"color=c=0x202020:size={width}x{height}:rate={fps}:duration={seconds:.4f},"
             f"drawbox=x=0:y={height // 6}:w={width // 3}:h={height * 2 // 3}:color=0xF0E6C8:t=fill,"
             f"noise=alls=8:allf=t")
    return _encode(path, graph, frames=round(seconds * fps), fps=fps)


def clean_24fps(path: Path, seconds: float = 9.4167, *, size: str = "768x1344") -> Path:
    """A provider-shaped clip: 768x1344 at 24 fps (h3-highspeed's native frame)."""
    width, height = (int(part) for part in size.split("x"))
    graph = f"testsrc2=size={width}x{height}:rate=24:duration={seconds:.4f}"
    return _encode(path, graph, frames=round(seconds * 24), fps=24)
