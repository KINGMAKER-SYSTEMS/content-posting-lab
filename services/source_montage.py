"""DRAFT, not wired: a montage body built from several planned source windows.

Status: proposal for review (branch draft/source-montage-executor). Nothing in
the registry, the job router or the Worker references this module, so live
behaviour is unchanged. docs/SOURCE-MONTAGE-DRAFT.md lists what wiring it in
would take and the decisions it needs from the owners.

What it makes: one silent, text-free 1080x1920 H.264 body, 5-9 s long (the
Worker's sourced-video admission bound), made of 2-4 short slices. Each slice
comes from its own window chosen by plan_source_cuts, so the never-reuse rule
holds without a second planner:

* the planner picks ``cuts`` fresh 5-9 s windows (seeded, deterministic);
* each window is reserved whole, as today, even though only a slice of it is
  shown. That spends supply faster than a recut but can never reuse footage;
* the slice offset inside its window is seeded from the job seed and the
  window's slot id, so the same job always renders the same frames.

Every window is re-checked with source_cut_is_planned before it is rendered
(the same rule the recut executor follows).
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.control_plane_sources import (
    CutUse,
    SourceCut,
    SourceRecipe,
    plan_source_cuts,
    source_cut_is_planned,
)

MONTAGE_FPS = 30
MONTAGE_MIN_MS = 5_000          # Worker admission bound for sourced video
MONTAGE_MAX_MS = 9_000
MONTAGE_MIN_CUTS = 2
MONTAGE_MAX_CUTS = 4
MONTAGE_MIN_SLICE_MS = 1_500    # shorter than this reads as a flash cut


@dataclass(frozen=True)
class MontageSlice:
    window: SourceCut     # the planned (and reserved) window
    offset_ms: int        # where the slice starts inside the window
    frames: int           # how many output frames it contributes

    @property
    def length_ms(self) -> int:
        return round(self.frames * 1000 / MONTAGE_FPS)

    @property
    def source_start_ms(self) -> int:
        return self.window.start_ms + self.offset_ms


@dataclass(frozen=True)
class MontagePlan:
    slices: tuple[MontageSlice, ...]

    @property
    def total_frames(self) -> int:
        return sum(s.frames for s in self.slices)

    @property
    def reserved_slot_ids(self) -> tuple[str, ...]:
        return tuple(s.window.slot_id for s in self.slices)


def _split_frames(total_frames: int, parts: int) -> list[int]:
    base, extra = divmod(total_frames, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _seeded_offset(seed: str, window: SourceCut, room_ms: int) -> int:
    """A whole-frame offset in [0, room_ms], fixed by the seed and the window."""
    if room_ms <= 0:
        return 0
    digest = hashlib.sha256(f"{seed}\0{window.slot_id}".encode("utf-8")).hexdigest()
    frame_ms = 1000 / MONTAGE_FPS
    steps = int(room_ms // frame_ms)
    return round((int(digest[:12], 16) % (steps + 1)) * frame_ms)


def plan_montage(
    recipe: SourceRecipe,
    served_slots: set[str],
    exclusions: list[tuple[str, str | None, int, int]] | None = None,
    *,
    seed: str,
    cuts: int = 3,
    total_ms: int = 7_000,
    history: dict[str, CutUse] | None = None,
) -> MontagePlan | None:
    """Plan one montage body, or None when the page cannot supply it.

    None means fewer than ``cuts`` fresh windows remain: the caller reports
    exhaustion the same way an empty recut plan does.
    """
    if not MONTAGE_MIN_CUTS <= cuts <= MONTAGE_MAX_CUTS:
        raise ValueError(f"montage cuts must be {MONTAGE_MIN_CUTS}-{MONTAGE_MAX_CUTS}")
    if not MONTAGE_MIN_MS <= total_ms <= MONTAGE_MAX_MS:
        raise ValueError(f"montage length must be {MONTAGE_MIN_MS}-{MONTAGE_MAX_MS} ms")
    total_frames = round(total_ms * MONTAGE_FPS / 1000)
    per_slice = _split_frames(total_frames, cuts)
    if min(per_slice) * 1000 / MONTAGE_FPS < MONTAGE_MIN_SLICE_MS:
        raise ValueError("montage slices would be shorter than the minimum slice")

    windows = plan_source_cuts(
        recipe, cuts, served_slots, exclusions, seed=f"montage\0{seed}", history=history,
    )
    if len(windows) < cuts:
        return None
    slices = []
    for window, frames in zip(windows, per_slice):
        length_ms = round(frames * 1000 / MONTAGE_FPS)
        if length_ms > window.duration_ms:
            return None
        offset = _seeded_offset(seed, window, window.duration_ms - length_ms)
        slices.append(MontageSlice(window=window, offset_ms=offset, frames=frames))
    return MontagePlan(slices=tuple(slices))


def montage_is_planned(recipe: SourceRecipe, plan: MontagePlan) -> bool:
    """Every window passes the planner's own check and every slice sits inside it."""
    for item in plan.slices:
        w = item.window
        if not source_cut_is_planned(recipe, w.master, w.start_ms, w.duration_ms, w.slot_id):
            return False
        if item.offset_ms < 0 or item.offset_ms + item.length_ms > w.duration_ms:
            return False
    total_ms = plan.total_frames * 1000 / MONTAGE_FPS
    return MONTAGE_MIN_MS <= total_ms <= MONTAGE_MAX_MS


def montage_ffmpeg_args(
    plan: MontagePlan,
    master_paths: dict[str, Path],
    output_path: Path,
    encode_args: list[str],
    *,
    width: int = 1080,
    height: int = 1920,
) -> list[str]:
    """One ffmpeg call: trim each slice, fill the frame, concatenate, encode silent."""
    args: list[str] = ["ffmpeg", "-y", "-v", "error"]
    chains = []
    for i, item in enumerate(plan.slices):
        src = master_paths[item.window.master.sha256]
        args += ["-ss", f"{item.source_start_ms / 1000:.3f}", "-i", str(src)]
        chains.append(
            f"[{i}:v]fps={MONTAGE_FPS},trim=end_frame={item.frames},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[v{i}]"
        )
    joined = "".join(f"[v{i}]" for i in range(len(plan.slices)))
    graph = ";".join(chains) + f";{joined}concat=n={len(plan.slices)}:v=1:a=0[out]"
    # The delivery preset's audio settings do not apply: the body is silent and
    # the campaign sound is bound downstream (audioPolicy in every contract).
    video_args = _without_audio(encode_args)
    args += ["-filter_complex", graph, "-map", "[out]", "-an",
             "-frames:v", str(plan.total_frames), *video_args, str(output_path)]
    return args


def _without_audio(encode_args: list[str]) -> list[str]:
    out, skip = [], False
    for i, arg in enumerate(encode_args):
        if skip:
            skip = False
            continue
        if arg in ("-c:a", "-b:a", "-ar", "-ac"):
            skip = True
            continue
        out.append(arg)
    return out


async def render_montage(
    recipe: SourceRecipe,
    plan: MontagePlan,
    master_paths: dict[str, Path],
    output_path: Path,
    encode_args: list[str],
) -> dict[str, Any]:
    """Re-check the plan, render it, and return what was made."""
    if not montage_is_planned(recipe, plan):
        raise ValueError("montage plan does not match the planner")
    args = montage_ffmpeg_args(plan, master_paths, output_path, encode_args,
                               width=recipe.output_width, height=recipe.output_height)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"montage render failed: {err.decode(errors='replace')[-800:]}")
    return {"output": str(output_path), "frames": plan.total_frames,
            "reservedSlots": list(plan.reserved_slot_ids)}
