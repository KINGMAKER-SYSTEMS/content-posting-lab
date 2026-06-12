"""Layered render adapters for Editor Bay v2 timelines.

OpenShot/libopenshot is the preferred long-term backend, but this module keeps
the renderer interface usable when the local OpenShot Python bindings are not
installed by providing a small ffmpeg fallback for image/video/audio layers.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class RenderError(RuntimeError):
    """Raised when a render backend cannot produce an artifact."""


def detect_render_backends() -> dict[str, dict[str, Any]]:
    openshot_spec = importlib.util.find_spec("openshot")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    melt = shutil.which("melt")
    blender = shutil.which("blender")
    return {
        "openshot": {
            "available": bool(openshot_spec),
            "preferred": True,
            "reason": "available" if openshot_spec else "Python bindings not importable in this environment",
        },
        "ffmpeg": {
            "available": bool(ffmpeg and ffprobe),
            "preferred": False,
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "reason": "available" if ffmpeg and ffprobe else "ffmpeg/ffprobe missing",
        },
        "mlt": {
            "available": bool(melt),
            "preferred": False,
            "binary": melt,
            "reason": "available" if melt else "melt binary missing",
        },
        "blender": {
            "available": bool(blender),
            "preferred": False,
            "binary": blender,
            "reason": "available" if blender else "blender binary missing",
        },
    }


def choose_renderer(output_dir: Path | str, *, asset_root: Path | str | None = None):
    capabilities = detect_render_backends()
    if capabilities["openshot"]["available"]:
        return OpenShotRenderer(output_dir, asset_root=asset_root)
    if capabilities["ffmpeg"]["available"]:
        return FFmpegLayeredRenderer(output_dir, asset_root=asset_root)
    raise RenderError(f"no supported renderer available: {json.dumps(capabilities)}")


class OpenShotRenderer:
    """Placeholder adapter boundary for the preferred backend.

    The class exists so downstream code can depend on the backend interface while
    the spike records that local Python bindings are not available yet.
    """

    backend = "openshot"

    def __init__(self, output_dir: Path | str, *, asset_root: Path | str | None = None):
        self.output_dir = Path(output_dir)
        self.asset_root = Path(asset_root) if asset_root else None

    def render(self, project: dict[str, Any], *, output_path: Path | str | None = None) -> dict[str, Any]:
        raise RenderError("OpenShot Python bindings are not wired in this environment")

    def render_frame(self, project: dict[str, Any], *, at: float, output_path: Path | str | None = None) -> dict[str, Any]:
        raise RenderError("OpenShot Python bindings are not wired in this environment")


class FFmpegLayeredRenderer:
    backend = "ffmpeg"

    def __init__(self, output_dir: Path | str, *, asset_root: Path | str | None = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.asset_root = Path(asset_root) if asset_root else None
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        if not self.ffmpeg or not self.ffprobe:
            raise RenderError("ffmpeg and ffprobe are required")

    def render(
        self, project: dict[str, Any], *, output_path: Path | str | None = None
    ) -> dict[str, Any]:
        output = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        duration = _project_duration(project)
        cmd = self._build_video_command(project, output, duration=duration)
        _run(cmd)
        return {
            "backend": self.backend,
            "video": str(output),
            "duration": _probe_duration(self.ffprobe, output),
        }

    def render_frame(
        self,
        project: dict[str, Any],
        *,
        at: float,
        output_path: Path | str | None = None,
    ) -> dict[str, Any]:
        frame = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}_{at:.2f}.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        temp_video = frame.with_suffix(".preview.mp4")
        self.render(project, output_path=temp_video)
        cmd = [
            self.ffmpeg,
            "-y",
            "-ss",
            f"{max(0.0, float(at)):.3f}",
            "-i",
            str(temp_video),
            "-frames:v",
            "1",
            str(frame),
        ]
        _run(cmd)
        return {"backend": self.backend, "frame": str(frame), "at": at}

    def _build_video_command(self, project: dict[str, Any], output: Path, *, duration: float) -> list[str]:
        width = int(project.get("width") or 1920)
        height = int(project.get("height") or 1080)
        fps = int(project.get("fps") or 30)
        assets = project.get("assets") or {}
        clips = _enabled_clips(project)
        visual_clips = [clip for clip in clips if _asset_type(assets, clip) in {"image", "video", "title"}]
        audio_clips = [clip for clip in clips if _asset_type(assets, clip) == "audio"]

        cmd: list[str] = [
            self.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}",
        ]
        input_meta: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        for clip in visual_clips + audio_clips:
            asset = assets.get(clip.get("assetId")) or {}
            src = self._resolve_src(asset.get("src", ""))
            if not src.exists():
                raise RenderError(f"asset missing: {src}")
            asset_kind = asset.get("type")
            if asset_kind == "image" or asset_kind == "title":
                cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(src)])
            else:
                cmd.extend(["-i", str(src)])
            input_meta.append((clip, asset, len(input_meta) + 1))

        filters: list[str] = []
        current = "[0:v]"
        for overlay_index, (clip, asset, input_index) in enumerate(
            [item for item in input_meta if item[1].get("type") in {"image", "video", "title"}],
            start=1,
        ):
            clip_label = f"vclip{overlay_index}"
            out_label = f"vout{overlay_index}"
            filters.append(self._visual_filter(input_index, clip, asset, clip_label, width, height))
            x_expr, y_expr = _overlay_expr(clip)
            start = float(clip.get("start") or 0)
            end = start + float(clip.get("duration") or duration)
            filters.append(
                f"{current}[{clip_label}]overlay=x={x_expr}:y={y_expr}:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{out_label}]"
            )
            current = f"[{out_label}]"

        if not visual_clips:
            filters.append(f"{current}format=yuv420p[v]")
        else:
            filters.append(f"{current}format=yuv420p[v]")

        audio_labels: list[str] = []
        for audio_index, (clip, asset, input_index) in enumerate(
            [item for item in input_meta if item[1].get("type") == "audio"],
            start=1,
        ):
            label = f"a{audio_index}"
            delay_ms = int(float(clip.get("start") or 0) * 1000)
            source_start = float(clip.get("sourceStart") or 0)
            duration_sec = float(clip.get("duration") or duration)
            volume = float(clip.get("volume") or 1.0)
            muted = bool(clip.get("muted"))
            volume = 0.0 if muted else volume
            filters.append(
                f"[{input_index}:a]atrim=start={source_start:.3f}:duration={duration_sec:.3f},"
                f"asetpts=PTS-STARTPTS,volume={volume:.3f},adelay={delay_ms}:all=1[{label}]"
            )
            audio_labels.append(f"[{label}]")
        if audio_labels:
            filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest[a]")

        cmd.extend(["-filter_complex", ";".join(filters), "-map", "[v]"])
        if audio_labels:
            cmd.extend(["-map", "[a]", "-c:a", "aac", "-shortest"])
        cmd.extend(
            [
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        return cmd

    def _visual_filter(
        self,
        input_index: int,
        clip: dict[str, Any],
        asset: dict[str, Any],
        label: str,
        width: int,
        height: int,
    ) -> str:
        transform = clip.get("transform") or {}
        opacity = float(transform.get("opacity", 1.0))
        scale = max(0.01, float(transform.get("scale", 1.0)))
        duration = float(clip.get("duration") or 0)
        source_start = float(clip.get("sourceStart") or 0)
        asset_type = asset.get("type")
        if asset_type == "video":
            trim = f"trim=start={source_start:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS,"
        else:
            trim = ""
        # The fallback keeps graphics at native size by default. Full-frame video clips
        # can opt in later via track-specific policies in the renderer slice.
        return (
            f"[{input_index}:v]{trim}scale=iw*{scale:.4f}:ih*{scale:.4f},"
            f"format=rgba,colorchannelmixer=aa={opacity:.4f}[{label}]"
        )

    def _resolve_src(self, src: str) -> Path:
        if src.startswith("/agenticnews-assets/") and self.asset_root:
            return self.asset_root / src.removeprefix("/agenticnews-assets/")
        return Path(src)


def _enabled_clips(project: dict[str, Any]) -> list[dict[str, Any]]:
    clips = [clip for clip in (project.get("clips") or {}).values() if clip.get("enabled", True)]
    tracks = project.get("tracks") or {}
    return sorted(
        clips,
        key=lambda clip: (
            int((tracks.get(clip.get("trackId")) or {}).get("index") or 0),
            float(clip.get("start") or 0),
            clip.get("id") or "",
        ),
    )


def _asset_type(assets: dict[str, Any], clip: dict[str, Any]) -> str:
    return str((assets.get(clip.get("assetId")) or {}).get("type") or "")


def _project_duration(project: dict[str, Any]) -> float:
    clips = _enabled_clips(project)
    if not clips:
        return 1.0
    return max(0.1, max(float(c.get("start") or 0) + float(c.get("duration") or 0) for c in clips))


def _overlay_expr(clip: dict[str, Any]) -> tuple[str, str]:
    transform = clip.get("transform") or {}
    x = float(transform.get("x", 0.5))
    y = float(transform.get("y", 0.5))
    return f"(main_w-overlay_w)*{x:.6f}", f"(main_h-overlay_h)*{y:.6f}"


def _run(cmd: list[str]) -> None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"render command timed out: {' '.join(cmd[:4])} ...") from exc
    if result.returncode != 0:
        raise RenderError(result.stderr[-1200:] or result.stdout[-1200:])


def _probe_duration(ffprobe: str, output: Path) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RenderError(result.stderr[-1200:] or result.stdout[-1200:])
    return float(result.stdout.strip())
