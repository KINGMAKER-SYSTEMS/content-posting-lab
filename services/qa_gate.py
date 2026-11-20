"""Post-cut QA gate: the lead's hand ffprobe/ffmpeg checklist as code.

Standard: ~/ec-work/seeno-claude/SELF-PRODUCING-PAGES-1118.md item 3 (QA gate
as code). Ruling: ROADMAP board L57062 (2026-11-19 02:34Z) — look issues never
block; only page-type/format, repeat, wrong-page and broken-file faults block.

Two outcomes per body:

BLOCK (``QaVerdict.ok`` is False; the cut is re-cut or dropped, never a clip):
  qa_probe_unreadable      ffprobe cannot prove one bounded video stream
  qa_decode_failed         the full decode pass errored (-xerror)
  qa_geometry_mismatch     not the recipe's frame (1080x1920)
  qa_rotation              a rotation tag (not upright)
  qa_codec_mismatch        not h264 / not yuv420p
  qa_sar_non_square        SAR not 1:1 and the stream-copy repair failed
  qa_audio_unexpected      an audio stream on a video-only recipe (QaPolicy
                           default: intake/CLI; a Lab source cut allows audio,
                           see policy_for_source_cut)
  qa_audio_missing         no audio stream on a baked-audio recipe
  qa_duration_mismatch     frame count outside the recipe target (+-1 frame)
  qa_orientation_mismatch  the display frame (after any rotate tag) is not the
                           recipe's orientation (a landscape file on a 9:16
                           page) - the params check, ROADMAP L57098 rung 2

WARN (``QaVerdict.ok`` stays True; scored, logged, recorded on the clip
manifest so a planner can rank picks; never stops a post):
  qa_tail_cut              a scene cut inside the last N frames (scdet
                           threshold 3), unless the clip is a fast montage
                           (>= 4 cuts, median card <= 0.5 s: Golden/Glossy/Fly
                           flips are the look)
  qa_black_run             a near-black run over 0.1 s (blackdetect d=0.05
                           pix_th=0.1), unless the recipe declares a fade
  qa_freeze                a freeze over 0.5 s (freezedetect), unless the
                           recipe is a still/photo format
  qa_sound_window_miss     the body ends outside its sound's clean window
                           (recipes/sound-clean-windows.v1.json)
  qa_stutter               the lead's stutter test (stutter.py v4, 11-20):
                           near-duplicate frames (YDIF < 0.3 against the
                           previous frame) that repeat on a fixed pattern
                           (PERIODIC: >= 8 repeats and >= 60 % of the gaps
                           between them are 4-6 frames, or one fixed gap
                           >= 3) or spread unevenly over the whole clip in
                           a moving shot (IRREGULAR). A frame shown exactly
                           twice (DOUBLED-OK), a still held on purpose and a
                           low-motion shot pass. A look issue: never blocks.
  qa_sideways_picture      the picture inside an upright 9:16 frame looks
                           turned a quarter turn (Joy eba568ef, L57098): three
                           sampled frames all carry a left-right brightness
                           axis AND horizontal-dominant edges. An IMAGE cue,
                           so WARN + score only (ruling L57135: an upright
                           dark room with a bright side window reads the
                           same); only the params rung above may block.

Quality score: 100 minus 40 (tail cut), 25 per black run (cap 50), 15 per
freeze (cap 30), 20 (sound window miss), 40 (sideways picture), 40 (stutter);
floor 0. A clean body scores 100.

Fail open (ruling L57135): a missing ffmpeg/ffprobe, a tool timeout, an OS
error spawning it, or any unexpected exception inside the gate is logged and
recorded on the manifest (``toolError``) and the clip PASSES as in shadow -
a tool outage never fails a job or deletes a clip. Only a file the tools
read and reject (ffprobe exit != 0, -xerror decode exit != 0) is a broken
file. Shadow changes nothing: no SAR repair, same bytes, same sha; repairs
run only under enforce. An unknown CONTENT_LAB_QA_GATE value means shadow.

SAR-only faults are repaired in place with the frame-identical stream-copy
rewrite proven in ~/ec-work/lanes/fanout-1117/aiw-render-1118/REPORT.md:
``-c copy -bsf:v h264_metadata=sample_aspect_ratio=1/1 -aspect 9:16``.

Mode switch ``CONTENT_LAB_QA_GATE``: ``shadow`` (default: scan, log and record,
never refuse, never repair), ``enforce`` (set it explicitly) or ``off``; unset,
empty or anything unknown = shadow.

Runtime: one ffprobe (~20 ms) plus one decode pass with the three detectors
(~0.4 s per 8 s 1080x1920 body on an M-series Mac; bounded by SCAN_TIMEOUT_S).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = "content-lab.qa-gate.v1"
MODE_ENFORCE = "enforce"
MODE_SHADOW = "shadow"
MODE_OFF = "off"
MODES = frozenset({MODE_ENFORCE, MODE_SHADOW, MODE_OFF})

AUDIO_FORBIDDEN = "forbidden"
AUDIO_REQUIRED = "required"
AUDIO_ALLOWED = "allowed"

# The same accept set as services/post_render.py's render gate: an absent SAR
# means square pixels; any explicit non-square value is a fault.
SQUARE_SAR = frozenset({"1:1", "", "N/A", "0:1"})

# Formats whose bodies are photos/cards held still on purpose.
STILL_FORMAT_SLUGS = frozenset({
    "image-video", "single-image-video", "ugc-slideshow", "coffee-slideshow",
})

BLOCKING_CODES = frozenset({
    "qa_probe_unreadable", "qa_decode_failed", "qa_geometry_mismatch",
    "qa_rotation", "qa_codec_mismatch", "qa_sar_non_square",
    "qa_audio_unexpected", "qa_audio_missing", "qa_duration_mismatch",
    "qa_orientation_mismatch",
})
WARNING_CODES = frozenset({
    "qa_tail_cut", "qa_black_run", "qa_freeze", "qa_sound_window_miss", "qa_sideways_picture",
    "qa_stutter",
})
PENALTIES = {"qa_tail_cut": 40, "qa_black_run": 25, "qa_freeze": 15, "qa_sound_window_miss": 20,
             "qa_sideways_picture": 40, "qa_stutter": 40}
PENALTY_CAPS = {"qa_black_run": 50, "qa_freeze": 30}

PROBE_TIMEOUT_S = 60
SCAN_TIMEOUT_S = 300
FIX_TIMEOUT_S = 120

SOUND_WINDOWS_PATH = Path(__file__).resolve().parents[1] / "recipes" / "sound-clean-windows.v1.json"
SOUND_WINDOWS_SCHEMA = "content-lab.sound-clean-windows.v1"


def gate_mode() -> str:
    # Shadow-first: an unset or empty switch never refuses clips. Merging the
    # Lab deploys it at once, so enforce must be an explicit choice.
    value = os.environ.get("CONTENT_LAB_QA_GATE", "").strip().lower()
    if value in {"on", "1", "true"}:
        return MODE_ENFORCE
    # A typo during a shadow-first rollout must not start refusing clips.
    return value if value in MODES else MODE_SHADOW


class QaGateError(RuntimeError):
    """A tool failure (ffmpeg/ffprobe missing, timed out, or not spawnable).
    Never a verdict about the file: run_gate turns it into a fail-open pass."""


@dataclass(frozen=True)
class QaPolicy:
    """What the recipe says a body must be. Every field has the lead's default."""
    width: int | None = 1080        # None: any frame (a provider's native size)
    height: int | None = 1920
    # The display orientation after any rotate tag: "portrait" | "landscape" | None.
    expected_orientation: str | None = "portrait"
    # The picture-level sideways test (three sampled frames). Off for cuts of
    # vetted masters; on for provider-generated clips (L57098).
    sideways_check: bool = False
    sideways_axis_min: float = 30.0      # |left-right| luma must be at least this
    sideways_axis_ratio: float = 0.25    # and |top-bottom| / |left-right| at most this
    sideways_edge_ratio: float = 1.2     # and horizontal/vertical edge energy at least this
    # Calibration (12 PAID-GEN-1119 bodies, 3 frames each): the turned Joy body
    # eba568ef reads left-right 63-83 / top-bottom 0-7 / edge 1.35-1.46 on every
    # frame; the upright Joy 8ad8f371 reads 31 / 18 / 1.17 and the other ten
    # read axis ratios >= 1.7 with edge <= 0.83. Both cues must agree per frame
    # and all three frames must agree per body.
    video_codec: str = "h264"
    # 8-bit 4:2:0 either way; yuvj420p only flags full range (Josie/Golden/Fly
    # bodies carry it and render fine). Empty tuple = any pixel format.
    pixel_formats: tuple[str, ...] = ("yuv420p", "yuvj420p")
    fps: float = 30.0
    expected_frames: int | None = None
    frame_tolerance: int = 1
    audio: str = AUDIO_FORBIDDEN
    still: bool = False
    fade: bool = False
    sound_id: str | None = None
    last_frames: int = 6     # only when the clip's rate is unknown; tail_window_s governs
    scene_threshold: float = 3.0
    # The lead's stutter.py prints last-0.2 s scene cuts at scdet threshold 8
    # and black runs at blackdetect d=0.1 pix_th=0.06. A 114-body agreement
    # run (control-plane-pad#237) found threshold 3 flags 16 tails the lead
    # passes and pix_th 0.1 flags 2 dim scenes, so the warnings use the
    # lead's numbers; threshold 3 still counts montage cards.
    tail_cut_score: float = 8.0
    tail_window_s: float = 0.2
    black_min_s: float = 0.1
    black_d: float = 0.1
    black_pix_th: float = 0.06
    freeze_min_s: float = 0.5
    freeze_noise_db: float = -60.0
    montage_min_cuts: int = 4
    montage_max_median_s: float = 0.5


@dataclass(frozen=True)
class ClipProbe:
    width: int
    height: int
    video_codec: str
    pixel_format: str
    sample_aspect_ratio: str
    fps: float
    nb_frames: int | None
    duration_s: float
    has_audio: bool
    rotation: int


@dataclass(frozen=True)
class ClipScan:
    frames: int
    last_pts_s: float
    cuts: tuple[tuple[int, float, float], ...]      # (frame, time_s, score)
    black_runs: tuple[tuple[float, float], ...]     # (start_s, end_s)
    freeze_runs: tuple[tuple[float, float], ...]    # (start_s, end_s)
    # signalstats YDIF per decoded frame (frame 0 first; its value is 0).
    ydif: tuple[float, ...] = ()


@dataclass(frozen=True)
class QaFinding:
    code: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING_CODES


@dataclass
class QaVerdict:
    findings: tuple[QaFinding, ...]
    probe: ClipProbe | None
    scan: ClipScan | None
    repaired_sar: bool = False
    # Look faults healed under enforce (e.g. ("stutter",)); the verdict is
    # about the healed file at ``path``.
    healed: tuple[str, ...] = ()
    elapsed_s: float = 0.0
    sound_window: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def blocks(self) -> tuple[QaFinding, ...]:
        return tuple(item for item in self.findings if item.blocking)

    @property
    def warnings(self) -> tuple[QaFinding, ...]:
        return tuple(item for item in self.findings if not item.blocking)

    @property
    def ok(self) -> bool:
        return not self.blocks

    @property
    def reason(self) -> str:
        """The first blocking code (the job/cut failure reason), or ''."""
        return self.blocks[0].code if self.blocks else ""

    @property
    def detail(self) -> str:
        return self.blocks[0].detail if self.blocks else ""

    @property
    def score(self) -> int:
        spent: dict[str, int] = {}
        for item in self.warnings:
            cost = PENALTIES.get(item.code, 0)
            cap = PENALTY_CAPS.get(item.code)
            spent[item.code] = spent.get(item.code, 0) + cost
            if cap is not None:
                spent[item.code] = min(cap, spent[item.code])
        return max(0, 100 - sum(spent.values()))

    def manifest(self, *, mode: str = MODE_ENFORCE) -> dict[str, Any]:
        """Lab-private clip-manifest record: evidence a planner can rank on."""
        scan = self.scan
        probe = self.probe
        return {
            "schema": SCHEMA,
            "mode": mode,
            "ok": self.ok,
            "score": self.score,
            "blocks": [item.code for item in self.blocks],
            "warnings": [item.code for item in self.warnings],
            "details": [f"{item.code}: {item.detail}" for item in self.findings],
            "repairedSar": self.repaired_sar,
            "healed": list(self.healed),
            "frames": scan.frames if scan else (probe.nb_frames if probe else None),
            "durationS": round(probe.duration_s, 3) if probe else None,
            "tailCuts": [list(cut) for cut in self.extra.get("tail_cuts", ())],
            "blackRuns": [list(run) for run in scan.black_runs] if scan else [],
            "freezeRuns": [list(run) for run in scan.freeze_runs] if scan else [],
            "soundWindow": self.sound_window,
            "orientation": self.extra.get("orientation"),
            "stutter": self.extra.get("stutter"),
            "stutterBefore": self.extra.get("stutterBefore"),
            "healedFps": self.extra.get("healedFps"),
            "toolError": self.extra.get("tool_error"),
            "elapsedMs": int(self.elapsed_s * 1000),
        }


# ── ffprobe ──────────────────────────────────────────────────────────────────

def _fraction(value: Any, default: float) -> float:
    if isinstance(value, str) and "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            if float(denominator) > 0:
                return float(numerator) / float(denominator)
        except ValueError:
            return default
    return default


def probe_clip(path: Path | str) -> ClipProbe:
    """Bounded container facts (one video stream required)."""
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-show_streams", "-show_format", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_S, check=False)
    except FileNotFoundError as error:
        raise QaGateError("ffprobe_unavailable") from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise QaGateError(f"ffprobe_{type(error).__name__}") from error
    if completed.returncode != 0:
        raise _Unreadable("ffprobe exit %s: %s" % (completed.returncode, completed.stderr.strip()[-200:]))
    try:
        data = json.loads(completed.stdout)
        videos = [row for row in data["streams"] if row.get("codec_type") == "video"]
        if len(videos) != 1:
            raise _Unreadable("one video stream required, found %d" % len(videos))
        video = videos[0]
        duration = _first_duration(video.get("duration"), data.get("format", {}).get("duration"))
        if duration is None:
            raise _Unreadable("invalid duration")
        rotations = [int(float(row.get("rotation", 0))) for row in video.get("side_data_list", [])
                     if isinstance(row, dict) and "rotation" in row]
        rotations.append(int(float((video.get("tags") or {}).get("rotate", 0))))
        raw_frames = video.get("nb_frames")
        nb_frames = int(raw_frames) if isinstance(raw_frames, str) and raw_frames.isdigit() else None
        return ClipProbe(
            width=int(video["width"]), height=int(video["height"]),
            video_codec=str(video.get("codec_name", "")), pixel_format=str(video.get("pix_fmt", "")),
            sample_aspect_ratio=str(video.get("sample_aspect_ratio", "")),
            fps=_fraction(video.get("r_frame_rate"), _fraction(video.get("avg_frame_rate"), 30.0)),
            nb_frames=nb_frames, duration_s=duration,
            has_audio=any(row.get("codec_type") == "audio" for row in data["streams"]),
            rotation=next((rotation for rotation in rotations if rotation != 0), 0),
        )
    except _Unreadable:
        raise
    except (KeyError, ValueError, TypeError) as error:
        raise _Unreadable("ffprobe output did not prove bounded video facts") from error


class _Unreadable(Exception):
    pass


def _first_duration(*candidates: Any) -> float | None:
    """The first finite positive duration; ffprobe prints "N/A" for a stream
    duration on HEVC/10-bit and streamed provider output while the format
    duration is still good."""
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def display_size(probe: ClipProbe) -> tuple[int, int]:
    """The frame a player shows: a quarter-turn rotate tag swaps the coded
    width and height (services.ffmpeg.probe_display_size does the same)."""
    rotation = probe.rotation % 360
    if abs(rotation - 90) < 1 or abs(rotation - 270) < 1:
        return probe.height, probe.width
    return probe.width, probe.height


# ── the sideways test: three gray frames, two independent cues ──────────────

_ORIENT_W, _ORIENT_H = 96, 168


def _gray_frame(path: Path | str, at_s: float) -> bytes | None:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-ss", f"{max(0.0, at_s):.3f}", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
             "-vf", f"scale={_ORIENT_W}:{_ORIENT_H}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    data = completed.stdout
    return data[: _ORIENT_W * _ORIENT_H] if len(data) >= _ORIENT_W * _ORIENT_H else None


def orientation_metrics(pixels: bytes, width: int = _ORIENT_W, height: int = _ORIENT_H) -> dict[str, float]:
    """Two cues on one gray frame. An upright outdoor/street scene carries its
    brightness axis top-to-bottom (sky, lights, ground) and its edge energy in
    rows (horizon, rooflines, road texture: vertical gradient Ev > Eh). The
    same scene turned a quarter turn carries the axis left-to-right and the
    edge energy in columns (Eh > Ev)."""
    px = pixels
    eh = ev = 0
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            eh += abs(px[row + x + 1] - px[row + x])
    for y in range(height - 1):
        row = y * width
        nxt = row + width
        for x in range(width):
            ev += abs(px[nxt + x] - px[row + x])
    eh /= (width - 1) * height
    ev /= width * (height - 1)
    third_h, third_w = height // 3, width // 3
    top = sum(px[: third_h * width]) / (third_h * width)
    bottom = sum(px[(height - third_h) * width:]) / (third_h * width)
    left = right = 0
    for y in range(height):
        row = y * width
        left += sum(px[row: row + third_w])
        right += sum(px[row + width - third_w: row + width])
    left /= third_w * height
    right /= third_w * height
    return {"eh": eh, "ev": ev, "edge_ratio": eh / ev if ev > 0 else float("inf"),
            "top_bottom": abs(top - bottom), "left_right": abs(left - right)}


def frame_is_sideways(metrics: dict[str, float], policy: QaPolicy) -> bool:
    lr, tb = metrics["left_right"], metrics["top_bottom"]
    return (lr >= policy.sideways_axis_min
            and (tb + 1.0) / (lr + 1.0) <= policy.sideways_axis_ratio
            and metrics["edge_ratio"] >= policy.sideways_edge_ratio)


def scan_orientation(path: Path | str, duration_s: float, policy: QaPolicy) -> dict[str, Any]:
    """Three frames at 10 / 50 / 90 %; sideways only when all three agree
    (one frame of a lit shopfront must not turn a body)."""
    frames: list[dict[str, Any]] = []
    for fraction in (0.1, 0.5, 0.9):
        at = duration_s * fraction
        pixels = _gray_frame(path, at)
        if pixels is None:
            frames.append({"atS": round(at, 3), "unreadable": True})
            continue
        metrics = orientation_metrics(pixels)
        frames.append({"atS": round(at, 3), **{key: round(value, 3) for key, value in metrics.items()},
                       "sideways": frame_is_sideways(metrics, policy)})
    readable = [frame for frame in frames if not frame.get("unreadable")]
    sideways = bool(readable) and len(readable) == len(frames) and all(frame["sideways"] for frame in readable)
    return {"frames": frames, "sideways": sideways}


# ── one decode pass: scdet + blackdetect + freezedetect ──────────────────────

_FRAME_LINE = re.compile(r"^frame:(\d+)\s+pts:\S+\s+pts_time:(\S+)")
_BLACK_LINE = re.compile(r"black_start:(\S+)\s+black_end:(\S+)")


def scan_clip(path: Path | str, policy: QaPolicy) -> ClipScan:
    """Decode the whole clip once; -xerror turns a broken file into a verdict."""
    graph = (
        f"scdet=threshold={policy.scene_threshold:g},"
        f"blackdetect=d={policy.black_d:g}:pix_th={policy.black_pix_th:g},"
        f"freezedetect=n={policy.freeze_noise_db:g}dB:d={policy.freeze_min_s:g},"
        "signalstats,"
        "metadata=mode=print:file=-"
    )
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-xerror",
             "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-an",
             "-vf", graph, "-f", "null", "-"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=SCAN_TIMEOUT_S, check=False)
    except FileNotFoundError as error:
        raise QaGateError("ffmpeg_unavailable") from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise QaGateError(f"ffmpeg_scan_{type(error).__name__}") from error
    if completed.returncode != 0:
        raise _DecodeFailed("ffmpeg exit %s: %s" % (completed.returncode, completed.stderr.strip()[-200:]))
    frame = -1
    pts = 0.0
    cuts: list[tuple[int, float, float]] = []
    freeze_starts: list[float] = []
    freeze_runs: list[tuple[float, float]] = []
    ydif: list[float] = []
    score = 0.0
    for line in completed.stdout.splitlines():
        match = _FRAME_LINE.match(line)
        if match:
            frame, pts = int(match.group(1)), float(match.group(2))
            score = 0.0
            continue
        key, separator, value = line.strip().partition("=")
        if not separator:
            continue
        if key == "lavfi.scd.score":
            score = float(value)
        elif key == "lavfi.scd.time":
            cuts.append((frame, pts, score))
        elif key == "lavfi.freezedetect.freeze_start":
            freeze_starts.append(float(value))
        elif key == "lavfi.signalstats.YDIF":
            ydif.append(float(value))
        elif key == "lavfi.freezedetect.freeze_end" and freeze_starts:
            freeze_runs.append((freeze_starts.pop(0), float(value)))
    frames = frame + 1
    end_s = pts + (1.0 / policy.fps if policy.fps > 0 else 0.0)
    for start in freeze_starts:
        # A freeze still running at EOF never gets a freeze_end tag.
        freeze_runs.append((start, end_s))
    black_runs = [(float(start), float(end)) for start, end in _BLACK_LINE.findall(completed.stderr)]
    return ClipScan(frames=frames, last_pts_s=pts, cuts=tuple(cuts),
                    black_runs=tuple(black_runs), freeze_runs=tuple(sorted(freeze_runs)),
                    ydif=tuple(ydif))


# ── the lead's stutter test ──────────────────────────────────────────────────

STUTTER_DUP_YDIF = 0.3          # a frame this close to the one before is a repeat
STUTTER_BAD = frozenset({"PERIODIC", "IRREGULAR"})


def stutter_verdict(ydif: list[float] | tuple[float, ...], fps: float) -> dict[str, Any]:
    """The lead's stutter.py v4 (seeno-claude/scouts-1120/stutter.py, md5
    261855824b7c796215a5801f5fb7dfea), same thresholds, same order of rules.

    ``ydif`` is signalstats YDIF for every decoded frame, frame 0 first (the
    script drops frame 0 too: it has no previous frame). ``fps`` is the
    stream's r_frame_rate. Returns the verdict plus the numbers the lead
    prints, and ``dups``: the repeated frames' indices in decode order."""
    values = list(ydif)[1:]
    idx = [i for i, y in enumerate(values) if y < STUTTER_DUP_YDIF]
    gaps = [b - a for a, b in zip(idx, idx[1:])]
    share = (sum(1 for g in gaps if g in (4, 5, 6)) / len(gaps)) if gaps else 0.0
    n = max(len(values), 1)
    win = max(int(round(fps if fps > 0 else 30.0)), 1)
    windows = max((n + win - 1) // win, 1)
    spread = len({i // win for i in idx}) / windows
    mode_gap = max(set(gaps), key=gaps.count) if gaps else 0
    mode_share = (gaps.count(mode_gap) / len(gaps)) if gaps else 0.0
    rate = len(idx) / n
    moving = sorted(y for y in values if y >= STUTTER_DUP_YDIF)
    nondup_med = moving[len(moving) // 2] if moving else 0.0
    if len(idx) >= 8 and share >= 0.6:
        verdict = "PERIODIC"
    elif rate > 0.05 and spread > 0.7 and mode_share >= 0.9 and gaps and mode_gap == 2:
        verdict = "DOUBLED-OK"      # every frame shown exactly twice: a smooth doubled rate
    elif rate > 0.05 and spread > 0.7 and mode_share >= 0.9 and gaps and mode_gap >= 3:
        verdict = "PERIODIC"        # one frame repeated every k >= 3 frames (20p in 30): judder
    elif rate > 0.05 and spread > 0.7 and gaps and sum(1 for g in gaps if g == 1) / len(gaps) >= 0.55:
        verdict = "ok-heldstills"   # repeats in long runs: a still held on purpose
    elif rate > 0.05 and spread > 0.7 and nondup_med >= 1.2:
        verdict = "IRREGULAR"
    elif rate > 0.05 and spread > 0.7:
        verdict = "ok-lowmotion"    # nothing moves, so frames repeat
    else:
        verdict = "ok"
    return {"verdict": verdict, "nearDups": len(idx), "frames": len(values), "share": round(share, 3),
            "spread": round(spread, 3), "modeGap": mode_gap, "modeShare": round(mode_share, 3),
            "dups": [i + 1 for i in idx]}


class _DecodeFailed(Exception):
    pass


# ── the SAR repair ───────────────────────────────────────────────────────────

def fix_sample_aspect_ratio(source: Path | str, destination: Path | str) -> None:
    """Stream-copy rewrite declaring square pixels in the SPS AND the mp4 pasp
    box (either alone leaves one at the old value). Frame-identical: the
    decoded framemd5 of destination equals source (aiw-render-1118 PROOF)."""
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-i", str(source), "-map", "0", "-c", "copy",
             "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1", "-aspect", "9:16",
             "-movflags", "+faststart", str(destination)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=FIX_TIMEOUT_S, check=False)
    except FileNotFoundError as error:
        raise QaGateError("ffmpeg_unavailable") from error
    if completed.returncode != 0:
        raise RuntimeError("sar_fix_failed: " + completed.stderr.strip()[-200:])


# ── the stutter heal ─────────────────────────────────────────────────────────

# Rates a padded clip usually came from; a re-timed rate this close snaps to it.
_NATIVE_RATES = (Fraction(12), Fraction(15), Fraction(20), Fraction(24000, 1001), Fraction(24),
                 Fraction(25), Fraction(30000, 1001))
HEAL_MIN_FPS = 12.0


HEAL_MAX_DUPS_AFTER = 3        # the lead's #239 rule: <= 3 near-dups left


def native_rate(frames: int, dups: int, fps: float) -> Fraction | None:
    """The rate that plays the kept frames (every frame minus the repeats)
    over the same duration. A standard rate (24, 25, ...) is used only when
    it keeps the duration within one source frame; otherwise the exact rate
    (to 1/1000 fps). Unlike a fixed-cycle decimate this never drops a real
    picture, so a montage that mixes padded and native-30 clips loses no
    motion (heal-stutter-1120 REPORT: fixed 24/25 decimate skipped pictures
    in 13 of 24 mixed bodies). None when nothing sensible is left."""
    if frames <= 0 or dups <= 0 or fps <= 0 or dups >= frames:
        return None
    source_fps = Fraction(fps).limit_denominator(1001)
    kept = frames - dups
    duration = Fraction(frames) / source_fps
    raw = Fraction(kept) / duration
    closest = min(_NATIVE_RATES, key=lambda rate: abs(Fraction(kept) / rate - duration))
    if abs(Fraction(kept) / closest - duration) <= 1 / source_fps:
        return closest
    rate = Fraction(round(float(raw) * 1000), 1000)
    return rate if HEAL_MIN_FPS <= float(rate) < fps else None


def heal_stutter(source: Path | str, destination: Path | str, dups: tuple[int, ...] | list[int],
                 rate: Fraction) -> None:
    """Drop the repeated frames and play the rest evenly at ``rate``: the same
    frames the camera made, the same duration, the audio copied untouched
    (the lead's #239 heal: back to the native rate, same frames)."""
    keep_out = "+".join(f"eq(n,{index})" for index in sorted(set(dups)))
    graph = f"select='not({keep_out})',setpts=N/({rate.numerator}/{rate.denominator})/TB,setsar=1"
    try:
        completed = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
             "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-vf", graph,
             "-r", f"{rate.numerator}/{rate.denominator}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
             "-c:a", "copy", "-movflags", "+faststart", str(destination)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=SCAN_TIMEOUT_S, check=False)
    except FileNotFoundError as error:
        raise QaGateError("ffmpeg_unavailable") from error
    except (subprocess.SubprocessError, OSError) as error:
        # A heal that times out is a failed heal: the body keeps its own
        # verdict (a block still blocks), never the fail-open tool pass.
        raise RuntimeError(f"stutter_heal_failed: {type(error).__name__}") from error
    if completed.returncode != 0:
        raise RuntimeError("stutter_heal_failed: " + completed.stderr.strip()[-200:])


def _healed_policy(policy: QaPolicy, rate: Fraction) -> QaPolicy:
    """The same contract at the native rate: the same duration in frames of
    the new rate (a 6 s body is 150 frames at 25 fps, not 180)."""
    expected = None
    if policy.expected_frames is not None and policy.fps > 0:
        expected = frames_for(policy.expected_frames / policy.fps, float(rate))
    return dataclasses.replace(policy, fps=float(rate), expected_frames=expected)


def _try_heal_stutter(verdict: QaVerdict, source: Path, destination: Path, policy: QaPolicy) -> QaVerdict | None:
    """Heal a PERIODIC/IRREGULAR body and keep the heal only when the healed
    file passes the whole gate again with no stutter and no block. Anything
    else leaves the original body as it was: a look issue never blocks."""
    dups = verdict.extra.get("stutter_dups") or ()
    probe = verdict.probe
    scan = verdict.scan
    if not dups or probe is None or scan is None:
        return None
    rate = native_rate(scan.frames, len(dups), probe.fps)
    if rate is None:
        log.info("qa_gate.stutter_heal_skipped path=%s reason=no_native_rate frames=%s dups=%s",
                 source.name, scan.frames, len(dups))
        return None
    healed = destination.with_name(destination.stem + ".heal" + destination.suffix)
    try:
        healed.parent.mkdir(parents=True, exist_ok=True)
        heal_stutter(source, healed, dups, rate)
        healed_policy = _healed_policy(policy, rate)
        after = validate_clip(healed, healed_policy)
        stutter = (after.extra.get("stutter") or {}).get("verdict")
        dups_after = (after.extra.get("stutter") or {}).get("nearDups")
        drift_s = abs(after.probe.duration_s - probe.duration_s) if after.probe else math.inf
        if (not after.ok or stutter in STUTTER_BAD or stutter is None
                or dups_after is None or dups_after > HEAL_MAX_DUPS_AFTER
                or drift_s > 1.0 / probe.fps + 1e-3):
            log.warning("qa_gate.stutter_heal_rejected path=%s rate=%s ok=%s stutter=%s dups_after=%s"
                        " drift_s=%.3f blocks=%s", source.name, rate, after.ok, stutter, dups_after,
                        drift_s, [item.code for item in after.blocks])
            return None
        os.replace(healed, destination)
        after.healed = ("stutter",)
        after.extra["stutterBefore"] = verdict.extra.get("stutter")
        after.extra["healedFps"] = f"{rate.numerator}/{rate.denominator}"
        log.info("qa_gate.stutter_healed path=%s rate=%s dups_dropped=%s", source.name, rate, len(dups))
        return after
    except Exception as error:  # noqa: BLE001 - a failed heal keeps the original verdict
        log.warning("qa_gate.stutter_heal_failed path=%s error=%s", source.name, error)
        return None
    finally:
        healed.unlink(missing_ok=True)


# ── sound clean windows ──────────────────────────────────────────────────────

def load_sound_windows(path: Path | None = None) -> dict[str, tuple[tuple[float, float], ...]]:
    """{sound_id: ((start_s, end_s), ...)}; an unreadable catalog is empty."""
    try:
        data = json.loads((path or SOUND_WINDOWS_PATH).read_text("utf-8"))
        if data.get("schema") != SOUND_WINDOWS_SCHEMA:
            return {}
        catalog: dict[str, tuple[tuple[float, float], ...]] = {}
        for sound_id, entry in data.get("sounds", {}).items():
            windows = tuple(
                (float(window["startS"]), float(window["endS"]))
                for window in entry.get("cleanWindows", [])
                if float(window["endS"]) > float(window["startS"]) >= 0
            )
            if windows:
                catalog[str(sound_id)] = windows
        return catalog
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}


def clean_windows_for(sound_id: str | None, catalog: dict | None = None) -> tuple[tuple[float, float], ...] | None:
    if not sound_id:
        return None
    return (catalog if catalog is not None else load_sound_windows()).get(str(sound_id))


def ending_fits(duration_s: float, windows, slack_s: float = 0.0) -> bool:
    """True when the body's end lands inside one clean window (inclusive)."""
    return any(start - slack_s <= duration_s <= end + slack_s for start, end in windows)


# ── policy from a recipe ─────────────────────────────────────────────────────

def frames_for(duration_s: float, fps: float) -> int:
    return max(1, int(round(duration_s * fps)))


def _encode_fps(encode_args: list[str] | None, default: float = 30.0) -> float:
    if not encode_args:
        return default
    for index, argument in enumerate(encode_args[:-1]):
        if argument == "-r":
            try:
                return float(Fraction(encode_args[index + 1]))
            except (ValueError, ZeroDivisionError):
                return default
    return default


def policy_for_source_cut(recipe: Any, render_duration_ms: int, render_speed: float,
                          job: dict | None = None, encode_args: list[str] | None = None) -> QaPolicy:
    """The source-dna-recut contract: the recipe's frame, h264 4:2:0 at the
    delivery preset's rate, exactly the rendered window at the effective
    speed. Audio is ALLOWED here: the executor's cut inherits whatever its
    master carries (page imports strip audio with -an; a registry master may
    not), every sourced format's audioPolicy is campaign_sound_bound_downstream
    and the prepared-post render maps video only, so a cut's own audio never
    reaches a post. The video-only rule has its teeth at intake (pool sidecar
    bakedAudio=false; the Chem Thu 13-20 s clips), through QaPolicy's default.
    ``recipe_spec.qaGate`` may declare {audio: video_only|baked|any, fade,
    still, soundId} once the recipe schema admits it; a job may carry
    soundId."""
    spec = getattr(recipe, "recipe_spec", None) or {}
    declared = spec.get("qaGate") if isinstance(spec, dict) else None
    declared = declared if isinstance(declared, dict) else {}
    audio = {"video_only": AUDIO_FORBIDDEN, "baked": AUDIO_REQUIRED, "any": AUDIO_ALLOWED}.get(
        str(declared.get("audio", "")).lower(),
        AUDIO_REQUIRED if declared.get("bakedAudio") is True else AUDIO_ALLOWED)
    if encode_args is None:
        try:
            from services.ffmpeg import delivery_encode_args
            encode_args = delivery_encode_args(getattr(recipe, "encode_preset", ""))
        except Exception:  # noqa: BLE001 - an unknown preset fails the render itself
            encode_args = None
    fps = _encode_fps(encode_args)
    speed = float(render_speed) if render_speed else 1.0
    sound_id = None
    if isinstance(job, dict):
        sound_id = job.get("soundId") or (job.get("constraints") or {}).get("soundId")
    sound_id = sound_id or declared.get("soundId")
    return QaPolicy(
        width=int(getattr(recipe, "output_width", 1080)),
        height=int(getattr(recipe, "output_height", 1920)),
        fps=fps,
        expected_frames=frames_for(int(render_duration_ms) / 1000.0 / speed, fps),
        audio=audio,
        still=bool(declared.get("still")) or getattr(recipe, "format_slug", None) in STILL_FORMAT_SLUGS,
        fade=bool(declared.get("fade")),
        sound_id=str(sound_id) if sound_id else None,
    )


def policy_for_generated_clip(recipe: Any, provider_info: dict | None = None) -> QaPolicy:
    """An ai_video / still-backed provider clip (routers.control_plane
    _run_owned_dossier_generation): the provider's native frame is the clip
    (768x1344 for h3-highspeed; post render scales it), so no size or length
    target - only broken-file, codec, SAR, rotation, orientation, audio where
    the provider strips it, and the picture-level sideways test (L57098).
    Still-backed providers (output_kind image) are still formats."""
    spec = getattr(recipe, "recipe_spec", None) or {}
    declared = spec.get("qaGate") if isinstance(spec, dict) else None
    declared = declared if isinstance(declared, dict) else {}
    info = provider_info or {}
    audio = {"video_only": AUDIO_FORBIDDEN, "baked": AUDIO_REQUIRED, "any": AUDIO_ALLOWED}.get(
        str(declared.get("audio", "")).lower(),
        AUDIO_FORBIDDEN if info.get("strip_audio") is True else AUDIO_ALLOWED)
    return QaPolicy(
        width=None, height=None, expected_frames=None,
        expected_orientation="portrait",
        sideways_check=declared.get("sidewaysCheck", True) is not False,
        audio=audio,
        still=bool(declared.get("still")) or info.get("output_kind") == "image"
        or getattr(recipe, "format_slug", None) in STILL_FORMAT_SLUGS,
        fade=bool(declared.get("fade")),
        sound_id=str(declared["soundId"]) if declared.get("soundId") else None,
    )


# ── the gate ─────────────────────────────────────────────────────────────────

def _probe_findings(probe: ClipProbe, policy: QaPolicy) -> list[QaFinding]:
    findings: list[QaFinding] = []
    if policy.width is not None and policy.height is not None and (
            (probe.width, probe.height) != (policy.width, policy.height)):
        findings.append(QaFinding("qa_geometry_mismatch",
                                  f"{probe.width}x{probe.height} is not {policy.width}x{policy.height}"))
    if probe.rotation != 0:
        findings.append(QaFinding("qa_rotation", f"rotation {probe.rotation}"))
    display_width, display_height = display_size(probe)
    if policy.expected_orientation in {"portrait", "landscape"}:
        portrait = display_height > display_width
        if portrait != (policy.expected_orientation == "portrait"):
            findings.append(QaFinding("qa_orientation_mismatch",
                                      f"displays {display_width}x{display_height} "
                                      f"(coded {probe.width}x{probe.height}, rotation {probe.rotation}), "
                                      f"recipe wants {policy.expected_orientation}"))
    if probe.video_codec != policy.video_codec or (
            policy.pixel_formats and probe.pixel_format not in policy.pixel_formats):
        findings.append(QaFinding("qa_codec_mismatch",
                                  f"{probe.video_codec}/{probe.pixel_format} is not "
                                  f"{policy.video_codec}/{'|'.join(policy.pixel_formats) or 'any'}"))
    if probe.sample_aspect_ratio not in SQUARE_SAR:
        findings.append(QaFinding("qa_sar_non_square", f"SAR {probe.sample_aspect_ratio}"))
    if policy.audio == AUDIO_FORBIDDEN and probe.has_audio:
        findings.append(QaFinding("qa_audio_unexpected", "audio stream on a video-only recipe"))
    elif policy.audio == AUDIO_REQUIRED and not probe.has_audio:
        findings.append(QaFinding("qa_audio_missing", "no audio stream on a baked-audio recipe"))
    return findings


def _duration_finding(frames: int, policy: QaPolicy) -> QaFinding | None:
    if policy.expected_frames is None:
        return None
    if abs(frames - policy.expected_frames) > policy.frame_tolerance:
        return QaFinding("qa_duration_mismatch",
                         f"{frames} frames ({frames / policy.fps:.3f} s) is not the recipe's "
                         f"{policy.expected_frames} +-{policy.frame_tolerance}")
    return None


def is_fast_montage(scan: ClipScan, policy: QaPolicy) -> bool:
    """Golden/Glossy/Fly flip cadences: many cuts with short cards. A trailing
    partial card there is the look, not a last-frame jump (Tis 2825b619 kept
    1 s cards and ended on a 0.2 s one: not a montage, a jump)."""
    if len(scan.cuts) < policy.montage_min_cuts:
        return False
    times = [cut[1] for cut in scan.cuts]
    segments = [times[0]] + [later - earlier for earlier, later in zip(times, times[1:])]
    return statistics.median(segments) <= policy.montage_max_median_s


def _scan_findings(scan: ClipScan, probe: ClipProbe, policy: QaPolicy, verdict_extra: dict) -> list[QaFinding]:
    findings: list[QaFinding] = []
    fault = _duration_finding(scan.frames, policy)
    if fault:
        findings.append(fault)
    tail_frames = policy.last_frames
    if probe.fps > 0:
        tail_frames = max(1, int(round(policy.tail_window_s * probe.fps)))
    tail_cuts = [cut for cut in scan.cuts
                 # The window's first frame counts too, unlike the lead's
                 # -sseof read (it cannot score that frame): a 6-frame last
                 # card is the Tis 2825b619 jump the lead ruled a fault.
                 if cut[0] >= scan.frames - tail_frames and cut[0] > 0 and cut[2] >= policy.tail_cut_score]
    verdict_extra["tail_cuts"] = tuple(tail_cuts)
    verdict_extra["fast_montage"] = is_fast_montage(scan, policy)
    if tail_cuts and not verdict_extra["fast_montage"]:
        first = tail_cuts[0]
        findings.append(QaFinding("qa_tail_cut",
                                  f"scene cut at frame {first[0]}/{scan.frames} ({first[1]:.3f} s, score {first[2]:.1f})"))
    if not policy.fade:
        for start, end in scan.black_runs:
            if end - start > policy.black_min_s + 1e-6:
                findings.append(QaFinding("qa_black_run", f"near-black {start:.3f}-{end:.3f} s ({end - start:.2f} s)"))
    if scan.ydif:
        stutter = stutter_verdict(scan.ydif, probe.fps)
        verdict_extra["stutter"] = {key: value for key, value in stutter.items() if key != "dups"}
        verdict_extra["stutter_dups"] = tuple(stutter["dups"])
        if stutter["verdict"] in STUTTER_BAD:
            findings.append(QaFinding("qa_stutter",
                                      f"{stutter['verdict']}: {stutter['nearDups']}/{stutter['frames']} repeated "
                                      f"frames, periodic share {stutter['share']:.2f}, spread {stutter['spread']:.2f}"))
    if not policy.still:
        for start, end in scan.freeze_runs:
            if end - start > policy.freeze_min_s + 1e-3:
                findings.append(QaFinding("qa_freeze", f"frozen {start:.3f}-{end:.3f} s ({end - start:.2f} s)"))
    return findings


def validate_clip(path: Path | str, policy: QaPolicy, *, scan: bool = True) -> QaVerdict:
    """Probe (and decode-scan) one body against a policy. Never repairs."""
    started = time.monotonic()
    extra: dict[str, Any] = {}
    try:
        probe = probe_clip(path)
    except _Unreadable as error:
        return QaVerdict((QaFinding("qa_probe_unreadable", str(error)),), None, None,
                         elapsed_s=time.monotonic() - started)
    findings = _probe_findings(probe, policy)
    clip_scan = None
    if scan:
        try:
            clip_scan = scan_clip(path, policy)
        except _DecodeFailed as error:
            findings.append(QaFinding("qa_decode_failed", str(error)))
        else:
            findings.extend(_scan_findings(clip_scan, probe, policy, extra))
    else:
        frames = probe.nb_frames if probe.nb_frames is not None else frames_for(probe.duration_s, probe.fps)
        fault = _duration_finding(frames, policy)
        if fault:
            findings.append(fault)
    # The clip's own rate drives timing when the policy has no frame target
    # (a provider's native 24 fps clip is sampled at its own 10/50/90 %).
    fps = policy.fps if policy.expected_frames is not None else (probe.fps if probe.fps > 0 else policy.fps)
    if policy.sideways_check and not any(item.blocking for item in findings):
        duration = (clip_scan.frames / fps) if clip_scan and fps > 0 else probe.duration_s
        orientation = scan_orientation(path, duration, policy)
        extra["orientation"] = orientation
        if orientation["sideways"]:
            first = next(frame for frame in orientation["frames"] if not frame.get("unreadable"))
            findings.append(QaFinding("qa_sideways_picture",
                                      f"picture turned a quarter turn on all 3 sampled frames "
                                      f"(left-right {first['left_right']:.0f} vs top-bottom {first['top_bottom']:.0f}, "
                                      f"edge ratio {first['edge_ratio']:.2f})"))
    sound_window = None
    windows = clean_windows_for(policy.sound_id)
    if windows is not None:
        duration = (clip_scan.frames / fps) if clip_scan and fps > 0 else probe.duration_s
        fits = ending_fits(duration, windows)
        sound_window = {"soundId": policy.sound_id, "windows": [list(window) for window in windows],
                        "endS": round(duration, 3), "fits": fits}
        if not fits:
            findings.append(QaFinding("qa_sound_window_miss",
                                      f"ends at {duration:.3f} s, clean windows {windows}"))
    return QaVerdict(tuple(findings), probe, clip_scan, elapsed_s=time.monotonic() - started,
                     sound_window=sound_window, extra=extra)


def validate_and_repair(path: Path | str, policy: QaPolicy, *, repair: bool = True,
                        repaired_destination: Path | str | None = None) -> QaVerdict:
    """The gate: repair a SAR-only container fault (frame-identical stream
    copy), then validate. In place by default; with ``repaired_destination``
    the source is left untouched (a paid provider master is evidence) and the
    repaired file lands there. ``verdict.path`` is the file the verdict is
    about: the repaired destination when a repair happened, else ``path``."""
    path = Path(path)
    started = time.monotonic()
    repaired = False
    target = path
    if repair:
        try:
            probe = probe_clip(path)
        except _Unreadable:
            probe = None
        if probe is not None:
            faults = [item.code for item in _probe_findings(probe, policy)]
            if faults == ["qa_sar_non_square"]:
                destination = Path(repaired_destination) if repaired_destination else path
                fixed = destination.with_name(destination.stem + ".sar1" + destination.suffix)
                try:
                    fixed.parent.mkdir(parents=True, exist_ok=True)
                    fix_sample_aspect_ratio(path, fixed)
                    if probe_clip(fixed).sample_aspect_ratio in SQUARE_SAR:
                        os.replace(fixed, destination)
                        repaired = True
                        target = destination
                except (RuntimeError, _Unreadable, OSError) as error:
                    log.warning("qa_gate.sar_fix_failed path=%s error=%s", path.name, error)
                finally:
                    fixed.unlink(missing_ok=True)
    verdict = validate_clip(target, policy)
    if repair and verdict.ok and any(item.code == "qa_stutter" for item in verdict.findings):
        destination = Path(repaired_destination) if repaired_destination else path
        healed = _try_heal_stutter(verdict, target, destination, policy)
        if healed is not None:
            verdict = healed
            target = destination
    verdict.repaired_sar = repaired
    verdict.path = target
    verdict.elapsed_s = time.monotonic() - started
    return verdict


def run_gate(path: Path | str, policy: QaPolicy, mode: str, *,
             repaired_destination: Path | str | None = None) -> QaVerdict:
    """The executor entry point. Never raises: a tool failure (QaGateError)
    or any unexpected exception becomes a PASS verdict carrying
    ``extra["tool_error"]`` (fail open, L57135 b). The SAR repair runs only
    under enforce (L57135 c: shadow changes nothing)."""
    started = time.monotonic()
    try:
        return validate_and_repair(path, policy, repair=(mode == MODE_ENFORCE),
                                   repaired_destination=repaired_destination)
    except QaGateError as error:
        reason = str(error) or type(error).__name__
    except Exception as error:  # noqa: BLE001 - the gate must never stop a job
        reason = f"{type(error).__name__}: {str(error)[:120]}"
    log.error("qa_gate.tool_error path=%s mode=%s error=%s (clip passes as in shadow)",
              Path(path).name, mode, reason)
    verdict = QaVerdict((), None, None, elapsed_s=time.monotonic() - started,
                        extra={"tool_error": reason}, path=Path(path))
    return verdict


# ── CLI: the lead's hand check, one line per body ────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Content Lab QA gate on local bodies (read-only unless --repair).")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--target-seconds", type=float, default=None, help="recipe duration target (+-1 frame)")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--audio", choices=(AUDIO_FORBIDDEN, AUDIO_REQUIRED, AUDIO_ALLOWED), default=AUDIO_FORBIDDEN)
    parser.add_argument("--still", action="store_true", help="still/photo format: freezes are the look")
    parser.add_argument("--fade", action="store_true", help="recipe declares a fade: black runs are the look")
    parser.add_argument("--sound", default=None, help="sound id for the clean-window check")
    parser.add_argument("--repair", action="store_true", help="rewrite a SAR-only fault in place")
    parser.add_argument("--any-size", action="store_true", help="a provider's native frame: skip the 1080x1920 check")
    parser.add_argument("--sideways-check", action="store_true", help="read 3 frames for a quarter-turned picture (ai_video)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    policy = QaPolicy(
        fps=args.fps,
        expected_frames=frames_for(args.target_seconds, args.fps) if args.target_seconds else None,
        audio=args.audio, still=args.still, fade=args.fade, sound_id=args.sound,
        width=None if args.any_size else 1080, height=None if args.any_size else 1920,
        sideways_check=args.sideways_check,
    )
    worst = 0
    for raw in args.paths:
        verdict = run_gate(raw, policy, MODE_ENFORCE if args.repair else MODE_SHADOW)
        if args.json:
            print(json.dumps({"path": raw, **verdict.manifest()}, sort_keys=True))
        else:
            state = "PASS" if verdict.ok else "BLOCK"
            print(f"{state} score={verdict.score:3d} {Path(raw).name} "
                  f"blocks={[f.code for f in verdict.blocks]} warnings={[f.code for f in verdict.warnings]} "
                  f"sar_fixed={verdict.repaired_sar} tool_error={verdict.extra.get('tool_error')} "
                  f"{verdict.elapsed_s:.2f}s")
        worst = max(worst, 0 if verdict.ok else 1)
    return worst


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_main(sys.argv[1:]))
