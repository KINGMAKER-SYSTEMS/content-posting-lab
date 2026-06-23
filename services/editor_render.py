"""Layered render adapters for Editor Bay v2 timelines.

OpenShot/libopenshot is the preferred long-term backend, but this module keeps
the renderer interface usable when the local OpenShot Python bindings are not
installed by providing a small ffmpeg fallback for image/video/audio layers.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Recovery instructions surfaced when services/openshot_bridge.py is missing.
# Exposed as a module constant so the import-error path is testable without
# having to actually delete the file. ponytail: one string, no logic.
#
# The original message only said "git add && git commit", which is wrong for the
# case that actually bit us: the file was UNTRACKED in the main checkout, so a
# `git worktree add` never copied it in. In a fresh worktree the file does not
# exist at all, `git add services/openshot_bridge.py` finds nothing, and the
# person is stuck. The real fix in a worktree is to copy it back from the main
# checkout (it lives there, just untracked), then commit it so it survives.
_MISSING_BRIDGE_MSG = (
    "services/openshot_bridge.py is missing from this checkout. It is the "
    "sanctioned OpenShot compiler module that editor_render imports at load "
    "time and it has been untracked/lost in a worktree before.\n"
    "Fix (committed checkout): `git add services/openshot_bridge.py && "
    "git commit` it on this branch.\n"
    "Fix (git worktree where the file is absent because it was untracked in "
    "main): copy it from the main checkout, then commit it so the worktree "
    "keeps it: `cp /path/to/main/services/openshot_bridge.py "
    "services/openshot_bridge.py && git add services/openshot_bridge.py && "
    "git commit`. Do NOT `git checkout`/`git stash` in the main repo while "
    "worktrees are live -- that deletes the untracked original."
)

try:
    import services.openshot_bridge as openshot_bridge
except ModuleNotFoundError as exc:
    # openshot_bridge is the sanctioned OpenShot compiler module and is imported
    # at module load. It has been UNTRACKED in a worktree before (see
    # .claude/workflows/prod-cycle.js): a fresh worktree or a `git checkout`/
    # `stash` that doesn't carry the file makes this import explode with a bare
    # "No module named 'services.openshot_bridge'" that points at nothing and
    # takes down choose_renderer with it. Re-raise with the actual fix so the
    # next person doesn't lose an hour. ponytail: one try/except, no behavior
    # change when the file is present.
    if exc.name not in {"services.openshot_bridge", "openshot_bridge"}:
        raise
    raise ModuleNotFoundError(_MISSING_BRIDGE_MSG) from exc

# The import above only proves a *file* called openshot_bridge.py is importable.
# A worktree/checkout accident can also leave the file present but truncated
# (0 bytes, partial write, merge-clobbered stub). That imports fine but is
# missing the symbols editor_render compiles against, so the real failure would
# surface much later as a cryptic AttributeError mid-render. Fail closed here
# with the same recovery message instead. ponytail: a tuple check, no new deps.
_REQUIRED_BRIDGE_ATTRS = ("timeline_json", "_resolve_asset_src")
_missing_attrs = [a for a in _REQUIRED_BRIDGE_ATTRS if not hasattr(openshot_bridge, a)]
if _missing_attrs:
    raise ModuleNotFoundError(
        f"services/openshot_bridge.py imported but is missing {_missing_attrs} "
        f"(present-but-broken stub).\n{_MISSING_BRIDGE_MSG}"
    )

log = logging.getLogger("editor_render")


class RenderError(RuntimeError):
    """Raised when a render backend cannot produce an artifact."""


def _num(value: Any, default: float) -> float:
    """Coerce a JSON-sourced transform/opacity value to float, falling back to
    ``default`` for garbage (``None``, ``"foo"``, NaN/inf). Frontend transforms
    arrive as untrusted JSON; a poisoned ``opacity``/``scale``/``x``/``y`` must
    not crash filter-graph construction with a bare ``ValueError`` (which escapes
    ``render()`` as an uncaught 500). ponytail: one coercion, sane default."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


# Effects the ffmpeg fallback can reproduce natively. fadeIn/fadeOut map to
# ffmpeg's `fade`/`afade` filters (the same in/out semantics OpenShot's Fade
# uses). brightness/saturation are flat color grades the `eq` filter reproduces
# 1:1 with OpenShot's Brightness/Saturation ranges (see _visual_filter). Crossfade
# and every keyframe envelope stay OpenShot-only; the fallback can't composite
# them, so it must SAY SO rather than silently drop them.
_FFMPEG_NATIVE_EFFECTS = {"fadeIn", "fadeOut", "brightness", "saturation"}

# On audio clips a crossfade is just a start-anchored fade-in (openshot_bridge's
# _FADE_DIRECTION_MAP maps both fadeIn and crossfade to OpenShot's `in` Fade), and
# the audio (a)fade chains below reproduce it as an `afade=t=in`. Visual crossfades
# stay unreproducible (the fallback can't composite two overlapping streams), so
# this extra-native set applies to audio clips only.
_FFMPEG_NATIVE_AUDIO_EFFECTS = _FFMPEG_NATIVE_EFFECTS | {"crossfade"}


def _ffmpeg_unsupported_drops(
    clip: dict[str, Any], *, is_audio: bool = False
) -> list[dict[str, str]]:
    """List the per-clip effects/keyframes the ffmpeg fallback cannot honor.

    OpenShot renders these; ffmpeg can't (without reinventing the compiler), so
    we surface a structured warning instead of dropping them in silence. Audio
    clips additionally reproduce ``crossfade`` (as a fade-in), so it is not a drop
    for them."""

    native = _FFMPEG_NATIVE_AUDIO_EFFECTS if is_audio else _FFMPEG_NATIVE_EFFECTS
    drops: list[dict[str, str]] = []
    clip_id = str(clip.get("id") or "")
    for effect in clip.get("effects") or []:
        effect_type = str((effect or {}).get("type") or "")
        if effect_type in native:
            continue
        drops.append({"clipId": clip_id, "kind": "effect", "name": effect_type})
    for track in clip.get("keyframes") or []:
        if not (track or {}).get("points"):
            continue
        prop = str((track or {}).get("property") or "")
        # volume/opacity/x/y/rotation envelopes are now reproduced as piecewise-
        # linear ffmpeg `t`-expressions (volume on the mux path; opacity + x/y +
        # rotation on the visual overlay). Only scale envelopes remain OpenShot-only.
        if prop in _FFMPEG_KEYFRAME_PROPERTIES:
            continue
        drops.append({"clipId": clip_id, "kind": "keyframe", "name": prop})
    return drops


def _fade_window(clip: dict[str, Any], direction: str) -> float | None:
    """Return the fadeIn/fadeOut duration (seconds) declared on a clip, if any."""

    best: float | None = None
    for effect in clip.get("effects") or []:
        if str((effect or {}).get("type") or "") != direction:
            continue
        seconds = float(((effect or {}).get("params") or {}).get("duration") or 0.0)
        if seconds > 0 and (best is None or seconds > best):
            best = seconds
    return best


def _color_grade_filter(clip: dict[str, Any]) -> str:
    """Reproduce flat brightness/saturation effects via ffmpeg's `eq` filter.

    OpenShot's Brightness effect (`brightness` -1..1, 0 neutral) and Saturation
    effect (`saturation` 0..3, 1 neutral) map 1:1 onto ffmpeg `eq=brightness=`
    and `eq=saturation=`, which use the same ranges and neutrals. Without this the
    ffmpeg fallback silently shipped UNGRADED clips when OpenShot bindings were
    missing (the bug). Returns an empty string when the clip carries no color
    grade so the common path stays a no-op; the last brightness/saturation effect
    wins (one flat grade per property). Defaults: brightness 0.0, saturation 1.0
    (both eq neutrals), so a clip carrying only one property leaves the other
    untouched."""

    brightness: float | None = None
    saturation: float | None = None
    for effect in clip.get("effects") or []:
        effect_type = str((effect or {}).get("type") or "")
        params = (effect or {}).get("params") or {}
        if effect_type == "brightness":
            brightness = _num(params.get("value"), 0.0)
        elif effect_type == "saturation":
            saturation = _num(params.get("value"), 1.0)
    if brightness is None and saturation is None:
        return ""
    return (
        f"eq=brightness={brightness if brightness is not None else 0.0:.4f}:"
        f"saturation={saturation if saturation is not None else 1.0:.4f},"
    )


def _audio_fade_in_window(clip: dict[str, Any]) -> float | None:
    """Start-anchored fade-in duration for an audio clip, treating ``crossfade``
    as a fade-in.

    OpenShot renders an audio crossfade as an `in` Fade (openshot_bridge's
    _FADE_DIRECTION_MAP: crossfade -> "in"); without this the ffmpeg audio path
    only saw `fadeIn` and silently dropped crossfade ducking transitions. The
    longest of the clip's fade-in / crossfade windows wins (same max() semantics
    _fade_window uses), so a clip carrying both still gets one fade-in."""

    fade_in = _fade_window(clip, "fadeIn")
    crossfade = _fade_window(clip, "crossfade")
    candidates = [d for d in (fade_in, crossfade) if d is not None]
    return max(candidates) if candidates else None


# Editor keyframe `property` names the ffmpeg fallback can now animate via a
# piecewise-linear `t`-expression, and how each maps onto an ffmpeg filter:
#   volume  -> the `volume` filter gain (audio mux path, see _volume_filter)
#   opacity -> colorchannelmixer alpha (visual overlay, see _visual_filter)
#   x, y    -> overlay x/y position (visual overlay, see _overlay_expr)
#   rotation-> the `rotate` filter angle (visual overlay, see _visual_filter)
# `scale` would re-time overlay_w/overlay_h mid-overlay (can't be a single
# overlay branch) and brightness/saturation are flat effects with no keyframe
# track in this schema, so both stay OpenShot-only and are reported as warnings.
_FFMPEG_KEYFRAME_PROPERTIES = {"volume", "opacity", "x", "y", "rotation"}


def _keyframe_points(clip: dict[str, Any], prop: str) -> list[tuple[float, float, str]]:
    """Sorted (clip-relative seconds, value, interp) points of one keyframe envelope.

    `t` is clip-relative, which after atrim/trim + (a)setpts (PTS reset to 0) is
    exactly the ffmpeg filter's `t`, so no source_start offset is needed. Values
    are floored at 0 for volume (a negative gain is meaningless); other
    properties pass through (position can be <0.5 / >0.5). `interp` is carried per
    point (mirroring the OpenShot bridge) so the ffmpeg fallback honors it instead
    of silently linearizing bezier/constant easing — see _piecewise_linear_expr."""

    points: list[tuple[float, float, str]] = []
    floor = prop == "volume"
    for track in clip.get("keyframes") or []:
        if str((track or {}).get("property") or "") != prop:
            continue
        for point in (track or {}).get("points") or []:
            t = max(0.0, float(point.get("t") or 0.0))
            v = float(point.get("value") or 0.0)
            interp = str(point.get("interp") or "linear").lower()
            points.append((t, max(0.0, v) if floor else v, interp))
    return sorted(points, key=lambda p: p[0])


def _piecewise_linear_expr(
    points: list[tuple[float, float] | tuple[float, float, str]], var: str = "t"
) -> str:
    """Build an ffmpeg time-expression that interpolates between sorted points,
    holding the first value before the first `t` and the last after the final `t`.
    Shared by the volume/opacity/position envelopes. `var` is the time symbol (`t`
    for most filters, `T` for geq). Assumes `points` is non-empty and sorted.

    Each point may carry a third element, its OpenShot interpolation, which (as in
    libopenshot) governs the segment leading INTO it from the previous point. Without
    this the ffmpeg fallback silently flattened every curve to linear, so bezier
    ducking/easing dropped vs the OpenShot render:
      - `constant` -> hold the previous value, step at t1 (no ramp)
      - `bezier`   -> smoothstep ease (3s^2-2s^3), the cheap ffmpeg-expressible
                       stand-in for OpenShot's cubic so curves stay curved
      - `linear`/anything else -> straight ramp (unchanged)
    A 2-tuple point (no interp) is treated as linear, preserving old callers."""

    def _interp(pt: tuple[float, ...]) -> str:
        return pt[2] if len(pt) > 2 else "linear"

    # The smallest threshold must be the OUTERMOST `if` (eval order is outside-in),
    # so iterate pairs in reverse and wrap each smaller-t1 branch around the rest.
    expr = f"{points[-1][1]:.4f}"  # default = last value (covers t >= final t)
    for p0, p1 in reversed(list(zip(points, points[1:]))):
        t0, v0 = p0[0], p0[1]
        t1, v1 = p1[0], p1[1]
        span = t1 - t0
        interp = _interp(p1)  # the segment's interp is the END point's (OpenShot)
        if span <= 0 or interp == "constant":
            seg = f"{v0:.4f}" if interp == "constant" else f"{v1:.4f}"
        elif interp == "bezier":
            # libopenshot's default BEZIER is the symmetric ease-in-out cubic with
            # x-handles at 0.5: parametric x(u)=1.5u-1.5u^2+u^3, y(u)=3u^2-2u^3.
            # Smoothstep (3s^2-2s^3) was the old stand-in but diverges from that
            # curve by up to ~0.056 mid-segment (it silently assumes u==x), so a
            # bezier ducking envelope rendered audibly different in this ffmpeg
            # fallback vs the OpenShot compiler. ffmpeg can't solve the cubic
            # x(u)=s for u in closed form, so refine u from u0=s with ONE Newton
            # step -- u -= (x(u)-s)/x'(u), x'(u)=1.5-3u+3u^2 -- which collapses the
            # parity gap to <=0.002 (30x better) with a branch-free expression.
            # See tests/test_editor_render.py::test_bezier_envelope_matches_openshot*.
            s = f"(({var}-{t0:.4f})/{span:.6f})"
            u = (
                f"({s}-((1.5*{s}-1.5*{s}*{s}+{s}*{s}*{s})-{s})"
                f"/(1.5-3*{s}+3*{s}*{s}))"
            )
            seg = f"({v0:.4f}+({v1 - v0:.4f})*(3*{u}*{u}-2*{u}*{u}*{u}))"
        else:
            slope = (v1 - v0) / span
            seg = f"({v0:.4f}+({slope:.6f})*({var}-{t0:.4f}))"
        expr = f"if(lt({var},{t1:.4f}),{seg},{expr})"
    return f"if(lt({var},{points[0][0]:.4f}),{points[0][1]:.4f},{expr})"


def _volume_keyframe_points(clip: dict[str, Any]) -> list[tuple[float, float]]:
    """Sorted (clip-relative seconds, gain) points of the clip's volume envelope.

    Mirrors openshot_bridge's `volume` keyframe track."""

    return _keyframe_points(clip, "volume")


def _volume_filter(clip: dict[str, Any], base_volume: float) -> str:
    """ffmpeg `volume` filter honoring a volume keyframe envelope when present.

    OpenShot animates `volume` per-keyframe; the flat `volume=` the mux path used
    silently flattened ducking envelopes. Build a piecewise-linear `t`-expression
    (eval=frame) so OpenShot and the mux path apply the same gain over time.

    A `volume` envelope's points are ABSOLUTE gains and win outright — exactly as
    they do in openshot_bridge._keyframe_overrides, where the envelope OVERWRITES
    the flat `volume` key rather than multiplying it. We must NOT scale the
    envelope by the clip's flat `volume` here: editor_timeline neutralizes the
    flat gain to 1.0 when it promotes an absolute envelope, but if any caller
    leaves a non-1.0 flat gain alongside an envelope, multiplying would
    double-attenuate the bed (the documented 0.6 -> 0.6*0.22 bug) and silently
    diverge from the OpenShot render. Ignoring base_volume when an envelope is
    present guarantees backend parity regardless of upstream neutralization."""

    points = _volume_keyframe_points(clip)
    if not points:
        return f"volume={base_volume:.4f}"
    expr = _piecewise_linear_expr(points)
    return f"volume='{expr}':eval=frame"


def detect_render_backends() -> dict[str, dict[str, Any]]:
    openshot_module, openshot_reason, openshot_path = _import_openshot()
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    melt = shutil.which("melt")
    blender = shutil.which("blender")
    return {
        "openshot": {
            "available": bool(openshot_module),
            "preferred": True,
            "reason": openshot_reason,
            "pythonPath": str(openshot_path) if openshot_path else None,
            "version": str(openshot_module.GetVersion()) if openshot_module else None,
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


def _backend_health(capabilities: dict[str, dict[str, Any]], selected: str) -> dict[str, Any]:
    """Build the per-render backend-health block the frontend uses to explain WHY
    an episode is on a given compiler.

    The preferred compiler (CLAUDE.md: OpenShot) may be unavailable, in which case
    ``choose_renderer`` silently downgrades to ffmpeg. Without this block the editor
    can't tell a deliberate OpenShot render from a silent ffmpeg fallback that lost
    effects/keyframes. ``downgraded`` is true whenever the selected backend is not
    the preferred one; ``reason`` carries the preferred backend's blocker so the UI
    can show it. ponytail: one dict, derived from the capability map we already have."""

    preferred = next(
        (name for name, cap in capabilities.items() if cap.get("preferred")),
        "openshot",
    )
    downgraded = selected != preferred
    return {
        "selected": selected,
        "preferred": preferred,
        "downgraded": downgraded,
        "reason": str((capabilities.get(preferred) or {}).get("reason") or "")
        if downgraded
        else "",
    }


class _HealthStampingRenderer:
    """Wraps the chosen renderer so every render result carries ``backendHealth``
    (selected-vs-preferred compiler + downgrade reason) and a ``warnings`` list.

    The OpenShot/subprocess backends return no ``warnings`` key, while the ffmpeg
    fallback does; stamping a default ``[]`` here gives callers (and the render-cache
    persistence in routers/agenticnews.py) one uniform contract regardless of which
    compiler ran. ponytail: one proxy at the single chokepoint, not three edits across
    three renderer classes."""

    def __init__(self, inner: Any, health: dict[str, Any]):
        self._inner = inner
        self._health = health

    @property
    def backend(self) -> str:
        return self._inner.backend

    def _stamp(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        result.setdefault("warnings", [])
        result["backendHealth"] = self._health
        return result

    def render(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._stamp(self._inner.render(*args, **kwargs))

    def render_frame(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._stamp(self._inner.render_frame(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def choose_renderer(output_dir: Path | str, *, asset_root: Path | str | None = None):
    capabilities = detect_render_backends()
    if capabilities["openshot"]["available"]:
        if os.getenv("EDITOR_RENDER_CHILD") == "1":
            # Child process: return the bare renderer. The parent
            # OpenShotSubprocessRenderer stamps health on the result it parses back,
            # so double-wrapping the child would be redundant (and the child's stdout
            # contract stays minimal).
            return OpenShotRenderer(output_dir, asset_root=asset_root)
        inner: Any = OpenShotSubprocessRenderer(output_dir, asset_root=asset_root)
        return _HealthStampingRenderer(inner, _backend_health(capabilities, "openshot"))
    if capabilities["ffmpeg"]["available"]:
        inner = FFmpegLayeredRenderer(output_dir, asset_root=asset_root)
        return _HealthStampingRenderer(inner, _backend_health(capabilities, "ffmpeg"))
    raise RenderError(f"no supported renderer available: {json.dumps(capabilities)}")


class OpenShotSubprocessRenderer:
    """Runs libopenshot outside the API process so native failures are contained."""

    backend = "openshot"

    def __init__(self, output_dir: Path | str, *, asset_root: Path | str | None = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.asset_root = Path(asset_root) if asset_root else None

    def render(
        self,
        project: dict[str, Any],
        *,
        output_path: Path | str | None = None,
        start: float = 0.0,
        duration: float | None = None,
    ) -> dict[str, Any]:
        output = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        return self._run_child({
            "kind": "video",
            "project": project,
            "outputPath": str(output),
            "start": start,
            "duration": duration,
        })

    def render_frame(
        self,
        project: dict[str, Any],
        *,
        at: float,
        output_path: Path | str | None = None,
    ) -> dict[str, Any]:
        frame = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}_{at:.2f}.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        return self._run_child({
            "kind": "frame",
            "project": project,
            "outputPath": str(frame),
            "at": at,
        })

    def _run_child(self, payload: dict[str, Any]) -> dict[str, Any]:
        child_payload = {
            **payload,
            "outputDir": str(self.output_dir),
            "assetRoot": str(self.asset_root) if self.asset_root else None,
        }
        # The whole project (keyframe envelopes included) crosses the subprocess
        # boundary as JSON. A non-finite keyframe value (NaN/Infinity) would
        # serialize to a bare `NaN`/`Infinity` token that the child's lenient
        # json.loads silently accepts, then OpenShot flattens the volume/ducking
        # envelope to garbage and the child exits 0 -- a corrupt-audio render the
        # parent never hears about. `allow_nan=False` makes that round-trip fail
        # loudly *here*, before we spawn, instead of shipping a broken duck.
        try:
            child_json = json.dumps(child_payload, allow_nan=False)
        except ValueError as exc:
            raise RenderError(
                f"refusing to render: project has non-finite keyframe/value "
                f"that would corrupt the subprocess JSON ({exc})"
            ) from exc
        env = os.environ.copy()
        env["EDITOR_RENDER_CHILD"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", _OPENSHOT_CHILD_CODE],
            input=child_json,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            timeout=1800,
        )
        result = _parse_child_render_result(completed.stdout)
        if completed.returncode != 0:
            if result and _render_result_artifact_exists(result):
                return {
                    **result,
                    "subprocessExitCode": completed.returncode,
                    "subprocessStderr": completed.stderr[-1200:],
                }
            detail = completed.stderr[-1200:] or completed.stdout[-1200:]
            raise RenderError(f"OpenShot subprocess failed ({completed.returncode}): {detail}")
        if not result:
            raise RenderError("OpenShot subprocess produced no render result")
        return result


class OpenShotRenderer:
    """OpenShot/libopenshot renderer for Editor Bay timelines."""

    backend = "openshot"

    def __init__(self, output_dir: Path | str, *, asset_root: Path | str | None = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.asset_root = Path(asset_root) if asset_root else None
        self.openshot, reason, _path = _import_openshot()
        if not self.openshot:
            raise RenderError(reason)

    def render(
        self,
        project: dict[str, Any],
        *,
        output_path: Path | str | None = None,
        start: float = 0.0,
        duration: float | None = None,
    ) -> dict[str, Any]:
        output = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        project_duration = _project_duration(project)
        window_start = max(0.0, float(start))
        remaining = max(0.1, project_duration - window_start)
        render_duration = remaining if duration is None else max(0.1, min(float(duration), remaining))
        render_project = _render_scope_project(project, window_start=window_start, duration=duration)
        missing_assets = _missing_assets(render_project, self.asset_root)
        if missing_assets:
            raise RenderError(f"render blocked by missing assets: {json.dumps(missing_assets)}")

        timeline = self._timeline(render_project)
        writer = self.openshot.FFmpegWriter(str(output))
        fps = _fps(project)
        width = int(project.get("width") or 1920)
        height = int(project.get("height") or 1080)
        sample_rate = int(project.get("sampleRate") or 48000)
        channels = int(project.get("channels") or 2)
        channel_layout = int(project.get("channelLayout") or 3)
        try:
            writer.SetVideoOptions(
                True,
                "libx264",
                self.openshot.Fraction(int(project.get("fps") or 30), 1),
                width,
                height,
                self.openshot.Fraction(1, 1),
                False,
                False,
                8000000,
            )
            has_audio = _project_has_audio(render_project)
            mix_audio_externally = has_audio and bool(shutil.which("ffmpeg"))
            writer.SetAudioOptions(
                has_audio and not mix_audio_externally,
                "aac" if has_audio and not mix_audio_externally else "",
                sample_rate,
                channels,
                channel_layout,
                192000 if has_audio and not mix_audio_externally else 0,
            )
            writer.Open()
            start_frame = _frame_number(0.0 if render_project is not project else window_start, fps)
            frame_count = max(1, int(math.ceil(render_duration * fps)))
            for offset in range(frame_count):
                writer.WriteFrame(timeline.GetFrame(start_frame + offset))
            writer.Close()
        except Exception as exc:
            try:
                writer.Close()
            except Exception:
                pass
            raise RenderError(f"OpenShot render failed: {exc}") from exc
        finally:
            try:
                timeline.Close()
            except Exception:
                pass

        # Only mux the deterministic ffmpeg timeline mix when OpenShot was told to
        # write SILENT video (mix_audio_externally). When ffmpeg is absent OpenShot
        # already wrote its own in-engine AAC stream from the Timeline, and the mux
        # path would only raise (it needs ffmpeg) — throwing away a valid render.
        audio_muxed = False
        if mix_audio_externally:
            audio_muxed = _mux_timeline_audio(
                render_project,
                output,
                duration=render_duration,
                asset_root=self.asset_root,
            )

        return {
            "backend": self.backend,
            "video": str(output),
            "start": window_start,
            "duration": _probe_duration(shutil.which("ffprobe") or "ffprobe", output),
            "missingAssets": missing_assets,
            "audioMuxed": audio_muxed,
        }

    def render_frame(self, project: dict[str, Any], *, at: float, output_path: Path | str | None = None) -> dict[str, Any]:
        frame = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}_{at:.2f}.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        render_project = _render_scope_project(project, window_start=max(0.0, float(at)), duration=0.25)
        missing_assets = _missing_assets(render_project, self.asset_root)
        if missing_assets:
            raise RenderError(f"render blocked by missing assets: {json.dumps(missing_assets)}")
        timeline = self._timeline(render_project)
        try:
            timeline.GetFrame(_frame_number(0.0, _fps(project))).Save(str(frame), 1.0, "PNG", 100)
        except Exception as exc:
            raise RenderError(f"OpenShot frame render failed: {exc}") from exc
        finally:
            try:
                timeline.Close()
            except Exception:
                pass
        return {"backend": self.backend, "frame": str(frame), "at": at, "missingAssets": missing_assets}

    def _timeline(self, project: dict[str, Any]):
        fps = int(project.get("fps") or 30)
        timeline = self.openshot.Timeline(
            int(project.get("width") or 1920),
            int(project.get("height") or 1080),
            self.openshot.Fraction(fps, 1),
            int(project.get("sampleRate") or 48000),
            int(project.get("channels") or 2),
            int(project.get("channelLayout") or 3),
        )
        timeline.SetJson(json.dumps(openshot_bridge.timeline_json(project, asset_root=self.asset_root)))
        timeline.Open()
        return timeline


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
        self,
        project: dict[str, Any],
        *,
        output_path: Path | str | None = None,
        start: float = 0.0,
        duration: float | None = None,
    ) -> dict[str, Any]:
        output = Path(output_path) if output_path else self.output_dir / f"{project['projectId']}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        project_duration = _project_duration(project)
        window_start = max(0.0, float(start))
        remaining = max(0.1, project_duration - window_start)
        render_duration = remaining if duration is None else max(0.1, min(float(duration), remaining))
        cmd, missing_assets, warnings = self._build_video_command(
            project,
            output,
            duration=render_duration,
            window_start=window_start,
        )
        if missing_assets:
            raise RenderError(f"render blocked by missing assets: {json.dumps(missing_assets)}")
        if warnings:
            if _ffmpeg_strict_drops():
                # Hard gate (CLAUDE.md): OpenShot is the sanctioned compiler. Rather than
                # silently emit a degraded artifact (a video crossfade dropped to a hard
                # cut, a color filter lost) and rely on a downstream UI to notice the
                # warnings list, refuse the render outright so nothing ships unnoticed.
                raise RenderError(
                    "ffmpeg fallback cannot honor effects/keyframes and "
                    f"EDITOR_RENDER_STRICT_DROPS is set: {json.dumps(warnings)}"
                )
            log.warning("ffmpeg fallback cannot honor %s; rendered without them", json.dumps(warnings))
        _run(cmd)
        return {
            "backend": self.backend,
            "video": str(output),
            "start": window_start,
            "duration": _probe_duration(self.ffprobe, output),
            "missingAssets": missing_assets,
            "warnings": warnings,
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
        render_result = self.render(project, output_path=temp_video, start=max(0.0, float(at)), duration=0.25)
        cmd = [
            self.ffmpeg,
            "-y",
            "-i",
            str(temp_video),
            "-frames:v",
            "1",
            str(frame),
        ]
        _run(cmd)
        # Parity with the OpenShot backend: the frame response must carry
        # missingAssets so callers see the same contract regardless of which
        # renderer (OpenShot vs ffmpeg-layered fallback) is selected. Carry the
        # dropped-effect/keyframe warnings through too — a preview frame that
        # silently lacks the fades/effects the OpenShot path would show is a lie
        # without this signal.
        return {
            "backend": self.backend,
            "frame": str(frame),
            "at": at,
            "missingAssets": render_result.get("missingAssets", []),
            "warnings": render_result.get("warnings", []),
        }

    def _build_video_command(
        self,
        project: dict[str, Any],
        output: Path,
        *,
        duration: float,
        window_start: float = 0.0,
    ) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
        width = int(project.get("width") or 1920)
        height = int(project.get("height") or 1080)
        fps = int(project.get("fps") or 30)
        assets = project.get("assets") or {}
        clips = _windowed_clips(project, window_start=window_start, duration=duration)
        visual_clips = [clip for clip in clips if _asset_type(assets, clip) in {"image", "video", "title"}]
        audio_clips = [clip for clip in clips if _asset_type(assets, clip) == "audio"]
        # Collect every effect/keyframe the fallback can't reproduce so the result
        # carries a warning instead of dropping them silently (the ticket's core).
        warnings: list[dict[str, str]] = []
        for clip in clips:
            warnings.extend(
                _ffmpeg_unsupported_drops(
                    clip, is_audio=_asset_type(assets, clip) == "audio"
                )
            )

        cmd: list[str] = [
            self.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}",
        ]
        input_meta: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        missing_assets: list[dict[str, str]] = []
        for clip in visual_clips + audio_clips:
            asset = assets.get(clip.get("assetId")) or {}
            raw_src = str(asset.get("src") or "").strip()
            if not raw_src:
                # Text-only placeholder (e.g. a lower-third whose headline lives in
                # metadata, not a media file). OpenShot's bridge renders these via a
                # DummyReader (blank); the ffmpeg fallback must likewise skip it, not
                # feed Path("")==='.' (a directory) as a phantom -i input and crash.
                # ponytail: skip-to-blank parity with the OpenShot path; live text
                # rendering (drawtext from metadata) can replace this when needed.
                continue
            src = self._resolve_src(raw_src)
            if not src.exists():
                missing_assets.append({
                    "clipId": str(clip.get("id") or ""),
                    "assetId": str(clip.get("assetId") or ""),
                    "src": str(src),
                })
                continue
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
            # Single-quote x/y: a position keyframe envelope is an if()/lt() expr
            # full of commas, which ffmpeg's filtergraph parser would otherwise read
            # as option separators (same quoting the `enable=` between() uses).
            filters.append(
                f"{current}[{clip_label}]overlay=x='{x_expr}':y='{y_expr}':"
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
            afades = ""
            a_fade_in = _audio_fade_in_window(clip)
            if a_fade_in is not None and duration_sec > 0:
                afades += f"afade=t=in:st=0:d={min(a_fade_in, duration_sec):.3f},"
            a_fade_out = _fade_window(clip, "fadeOut")
            if a_fade_out is not None and duration_sec > 0:
                a_out_d = min(a_fade_out, duration_sec)
                afades += f"afade=t=out:st={max(0.0, duration_sec - a_out_d):.3f}:d={a_out_d:.3f},"
            # Honor a volume keyframe envelope (ducking) the same way the OpenShot
            # mux path does — _volume_filter falls back to flat `volume=` when the
            # clip has no envelope, so the cheap path is unchanged.
            vol_filter = "volume=0.000" if muted else _volume_filter(clip, volume)
            filters.append(
                f"[{input_index}:a]atrim=start={source_start:.3f}:duration={duration_sec:.3f},"
                f"asetpts=PTS-STARTPTS,{vol_filter},{afades}adelay={delay_ms}:all=1[{label}]"
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
        return cmd, missing_assets, warnings

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
        opacity = min(1.0, max(0.0, _num(transform.get("opacity"), 1.0)))
        scale = max(0.01, _num(transform.get("scale"), 1.0))
        rotation = _num(transform.get("rotation"), 0.0)
        duration = _num(clip.get("duration"), 0.0)
        source_start = _num(clip.get("sourceStart"), 0.0)
        asset_type = asset.get("type")
        if asset_type == "video":
            trim = f"trim=start={source_start:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS,"
        else:
            trim = ""
        # fadeIn/fadeOut are the one effect ffmpeg reproduces natively: a `fade`
        # on the rgba alpha (alpha=1) ramps the overlay's transparency, matching
        # OpenShot's Fade. Timings are clip-relative (input PTS start at 0 after
        # trim/loop). Crossfade + color filters + keyframes stay OpenShot-only and
        # are reported via warnings, not faked here.
        fades = ""
        fade_in = _fade_window(clip, "fadeIn")
        if fade_in is not None and duration > 0:
            fades += f"fade=t=in:alpha=1:st=0:d={min(fade_in, duration):.3f},"
        fade_out = _fade_window(clip, "fadeOut")
        if fade_out is not None and duration > 0:
            out_d = min(fade_out, duration)
            fades += f"fade=t=out:alpha=1:st={max(0.0, duration - out_d):.3f}:d={out_d:.3f},"
        # Opacity: a flat transform multiplies the alpha plane via colorchannelmixer
        # (cheap, per-frame constant). An `opacity` keyframe envelope instead drives
        # a piecewise-linear alpha over time via `geq` (the one ffmpeg primitive that
        # accepts a `T`-expression on the alpha plane), matching OpenShot's animated
        # `alpha` keyframes. RGB planes pass through untouched.
        opacity_pts = _keyframe_points(clip, "opacity")
        if opacity_pts:
            # geq uses uppercase T (seconds); build the interpolator over T and
            # clamp it to a valid 0..1 alpha range.
            alpha_t = _piecewise_linear_expr(opacity_pts, var="T")
            alpha = (
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                f"a='255*max(0,min(1,{alpha_t}))'"
            )
        else:
            alpha = f"colorchannelmixer=aa={opacity:.4f}"
        # Rotation: the editor schema (and the OpenShot bridge, _KEYFRAME_PROPERTY_MAP)
        # support a `rotation` track in DEGREES; ffmpeg's `rotate` filter takes an angle
        # in RADIANS and accepts a `t`-expression, so a flat value and a keyframe envelope
        # both map cleanly. Without this the envelope silently dropped vs the OpenShot
        # render (the ticket). `c=none` keeps corners transparent. The output box (ow/oh)
        # must be CONSTANT — ffmpeg evaluates it once at init and rejects a t-dependent
        # rotw()/roth(), so we size it to the rotation diagonal hypot(iw,ih), which never
        # clips at any angle. ow/oh are single-quoted because hypot()'s comma would
        # otherwise read as a filtergraph option separator. Skip the filter entirely when
        # there's no rotation so the common (unrotated) path stays a no-op.
        _box = "ow='hypot(iw,ih)':oh='hypot(iw,ih)'"
        rotation_pts = _keyframe_points(clip, "rotation")
        if rotation_pts:
            deg_t = _piecewise_linear_expr(rotation_pts, var="t")
            rotate = f"rotate=a='({deg_t})*PI/180':c=none:{_box},"
        elif abs(rotation) > 1e-9:
            rotate = f"rotate=a='{rotation:.4f}*PI/180':c=none:{_box},"
        else:
            rotate = ""
        # Flat brightness/saturation color grades reproduced via `eq` (matches
        # OpenShot's Brightness/Saturation ranges 1:1). Applied on the rgba stream
        # before alpha/fade so the grade affects RGB while transparency is honored.
        # No-op string when the clip carries no color grade.
        grade = _color_grade_filter(clip)
        # The fallback keeps graphics at native size by default. Full-frame video clips
        # can opt in later via track-specific policies in the renderer slice.
        return (
            f"[{input_index}:v]{trim}scale=iw*{scale:.4f}:ih*{scale:.4f},"
            f"format=rgba,{grade}{alpha},{fades}{rotate}null[{label}]"
        )

    def _resolve_src(self, src: str) -> Path:
        return _resolve_asset_src(src, self.asset_root)


def _ffmpeg_strict_drops() -> bool:
    """Whether the ffmpeg fallback must REFUSE (raise) rather than silently emit a
    degraded artifact when it can't honor an effect/keyframe (e.g. a video crossfade).

    CLAUDE.md hard gate: OpenShot is the sanctioned compiler, so silently shipping a
    render that dropped effects violates the contract. The drops are always surfaced
    in the result's ``warnings`` list, but a warning is only useful if something acts
    on it — and there is no guarantee a caller/UI checks it before publishing. With
    ``EDITOR_RENDER_STRICT_DROPS=1`` the fallback turns a would-be-degraded render
    into a loud ``RenderError`` so a degraded ABN episode can never complete unnoticed.
    Default off: warn-and-continue stays the behavior every existing caller/test sees.
    ponytail: one env read, no new config surface, no signature churn."""

    return os.getenv("EDITOR_RENDER_STRICT_DROPS", "").strip().lower() in {"1", "true", "yes", "on"}


def _import_openshot():
    candidates = _openshot_python_candidates()
    for python_path in candidates:
        if python_path.exists() and str(python_path) not in sys.path:
            sys.path.insert(0, str(python_path))

    # When the bindings are missing the bare reason ("not importable") doesn't
    # tell ops WHERE the renderer looked, so a wiped .codex runtime or a stale
    # OPENSHOT_PYTHON_PATH looks identical to "never installed". Surface the
    # searched candidates so backendHealth.reason points at the fix. CLAUDE.md:
    # OpenShot is the sanctioned compiler — a silent ffmpeg downgrade must be
    # diagnosable. ponytail: one f-string off the list we already built.
    searched = ", ".join(str(p) for p in candidates) or "<none>"

    # find_spec is not always a clean None on a broken install. A half-loaded
    # native-extension module (the "dylibs wiped, bindings present" failure from
    # the OpenShot runtime fragility note) can sit in sys.modules with
    # ``__spec__ = None``, and then find_spec("openshot") raises ValueError
    # ("openshot.__spec__ is None") instead of returning None. If that escapes,
    # it crashes choose_renderer and defeats the whole point of the ffmpeg
    # fallback. Treat any spec-discovery failure as "not importable" so the
    # renderer degrades to ffmpeg instead of 500ing. ponytail: one try/except.
    try:
        spec = importlib.util.find_spec("openshot")
    except Exception as exc:
        return None, f"Python bindings not importable in this environment: {exc}", None
    if not spec:
        return (
            None,
            f"Python bindings not importable in this environment (searched: {searched})",
            None,
        )

    try:
        import openshot  # type: ignore
    except Exception as exc:
        return None, f"Python bindings failed to import: {exc}", Path(spec.origin).parent if spec.origin else None

    _disable_openshot_zmq_logger(openshot)

    return openshot, "available", Path(spec.origin).parent if spec.origin else None


_zmq_logger_disabled = False


def _disable_openshot_zmq_logger(openshot) -> None:
    """Disable libopenshot's ZMQ debug logger exactly once per process.

    The logger binds a fixed TCP port (5556) the first time a render runs. With
    multiple in-process renders (the test suite, or parallel cycle gates) the
    singleton contends on that port and a later render blocks forever. We don't
    consume its debug stream, so turn it off. Calling Enable(False) on every
    import re-touches the singleton and reintroduces the contention this guards
    against — so do it once and latch the flag. ponytail: one call kills the
    port bind.
    """
    global _zmq_logger_disabled
    if _zmq_logger_disabled:
        return
    try:
        openshot.ZmqLogger.Instance().Enable(False)
    except Exception:
        pass  # logger API absent on some builds -> nothing to disable, safe to ignore
    _zmq_logger_disabled = True


def _openshot_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    for value in (os.environ.get("OPENSHOT_PYTHON_PATH"), os.environ.get("PYTHONPATH")):
        if value:
            candidates.extend(Path(p) for p in value.split(os.pathsep) if p)
    candidates.append(_repo_root() / ".codex" / "openshot-runtime" / "install" / "python")
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


_OPENSHOT_CHILD_CODE = r"""
import json
import sys

from services.editor_render import OpenShotRenderer

payload = json.loads(sys.stdin.read())
renderer = OpenShotRenderer(payload["outputDir"], asset_root=payload.get("assetRoot"))
if payload["kind"] == "video":
    result = renderer.render(
        payload["project"],
        output_path=payload.get("outputPath"),
        start=payload.get("start", 0),
        duration=payload.get("duration"),
    )
elif payload["kind"] == "frame":
    result = renderer.render_frame(
        payload["project"],
        output_path=payload.get("outputPath"),
        at=payload.get("at", 0),
    )
else:
    raise SystemExit(f"unsupported child render kind: {payload['kind']}")
print(json.dumps(result), flush=True)
"""


def _parse_child_render_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _artifact_is_structurally_complete(path: Path) -> bool:
    """Cheap, dependency-free integrity gate for a salvaged render artifact.

    `st_size > 0` is necessary but not sufficient: a child killed mid-write (OOM,
    SIGKILL, timeout) commonly leaves a present, non-empty, *truncated* file whose
    container is missing the index it needs to be playable. ffmpeg writes the mp4
    `moov` atom LAST (no faststart), so a mid-write kill leaves the `mdat` payload
    but no `moov` -- the exact "incomplete moov atom" corruption the editorial
    retry would otherwise silently re-use. We byte-scan for the format's terminal
    marker rather than shelling out to ffprobe (no subprocess under render
    concurrency, no extra dep). Unknown extensions fall back to size>0 so we never
    falsely reject a format we don't model.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in {".mp4", ".mov", ".m4v", ".m4a"}:
            # The moov atom is small relative to the file; reading the whole thing
            # is fine for a render artifact and avoids missing a front-faststart
            # moov on a clean file. A truncated render simply won't contain it.
            return b"moov" in path.read_bytes()
        if suffix == ".png":
            # Canonical PNG end-of-file chunk. A truncated PNG lacks it.
            return path.read_bytes().rstrip().endswith(b"IEND\xaeB`\x82")
    except OSError:
        return False
    return True


def _render_result_artifact_exists(result: dict[str, Any]) -> bool:
    artifact = result.get("video") or result.get("frame")
    if not artifact:
        return False
    path = Path(str(artifact))
    # A non-zero child exit can leave a *present but truncated* artifact behind
    # (process killed mid-write). A zero-byte file is never a salvageable render,
    # and neither is a non-zero-byte file whose container is structurally
    # incomplete (truncated mp4 with no moov atom, half-written png). Reject both
    # so the caller raises a hard RenderError instead of shipping a corrupt clip
    # -- or worse, having a silent retry re-use the broken file as if finished.
    try:
        if not (path.is_file() and path.stat().st_size > 0):
            return False
    except OSError:
        return False
    return _artifact_is_structurally_complete(path)


def _asset_type(assets: dict[str, Any], clip: dict[str, Any]) -> str:
    return str((assets.get(clip.get("assetId")) or {}).get("type") or "")


def _project_duration(project: dict[str, Any]) -> float:
    clips = _enabled_clips(project)
    if not clips:
        return 1.0
    return max(0.1, max(float(c.get("start") or 0) + float(c.get("duration") or 0) for c in clips))


def _project_has_audio(project: dict[str, Any]) -> bool:
    assets = project.get("assets") or {}
    return any(_asset_type(assets, clip) == "audio" for clip in _enabled_clips(project))


def _audio_clips(project: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    assets = project.get("assets") or {}
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for clip in _enabled_clips(project):
        asset = assets.get(clip.get("assetId")) or {}
        if str(asset.get("type") or "") != "audio":
            continue
        if clip.get("muted") or float(clip.get("volume") or 0) <= 0:
            continue
        out.append((clip, asset))
    return out


def _mux_timeline_audio(
    project: dict[str, Any],
    video_path: Path,
    *,
    duration: float,
    asset_root: Path | None,
) -> bool:
    """Replace OpenShot's audio stream with a deterministic editor-timeline mix."""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("ffmpeg is required to mux Editor Bay timeline audio")

    clips = _audio_clips(project)
    if not clips:
        return False

    output = Path(video_path)
    temp_output = output.with_name(f"{output.stem}.audio-mux{output.suffix}")
    cmd: list[str] = [ffmpeg, "-y", "-i", str(output)]
    input_meta: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    for clip, asset in clips:
        src = _resolve_asset_src(str(asset.get("src") or ""), asset_root)
        if not src.exists():
            raise RenderError(f"audio mux blocked by missing asset: {src}")
        cmd.extend(["-i", str(src)])
        input_meta.append((clip, asset, len(input_meta) + 1))

    filters: list[str] = []
    labels: list[str] = []
    for audio_index, (clip, _asset, input_index) in enumerate(input_meta, start=1):
        label = f"mixa{audio_index}"
        delay_ms = int(round(max(0.0, float(clip.get("start") or 0)) * 1000))
        source_start = max(0.0, float(clip.get("sourceStart") or 0))
        clip_duration = max(0.001, float(clip.get("duration") or duration))
        volume = max(0.0, float(clip.get("volume") or 1.0))
        # Apply fadeIn/fadeOut the same way the ffmpeg-layered fallback does, so
        # OpenShot-muxed audio honors crossfade/ducking effects instead of
        # silently dropping them. Timings are clip-relative (PTS reset to 0 by
        # asetpts), inserted between volume and the timeline adelay offset.
        afades = ""
        a_fade_in = _audio_fade_in_window(clip)
        if a_fade_in is not None:
            afades += f"afade=t=in:st=0:d={min(a_fade_in, clip_duration):.3f},"
        a_fade_out = _fade_window(clip, "fadeOut")
        if a_fade_out is not None:
            a_out_d = min(a_fade_out, clip_duration)
            afades += f"afade=t=out:st={max(0.0, clip_duration - a_out_d):.3f}:d={a_out_d:.3f},"
        filters.append(
            f"[{input_index}:a]atrim=start={source_start:.3f}:duration={clip_duration:.3f},"
            f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
            f"{_volume_filter(clip, volume)},{afades}adelay={delay_ms}:all=1[{label}]"
        )
        labels.append(f"[{label}]")

    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
        + f"alimiter=limit=0.98,atrim=duration={max(0.1, duration):.3f},asetpts=PTS-STARTPTS[aout]"
    )
    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-t",
            f"{max(0.1, duration):.3f}",
            str(temp_output),
        ]
    )
    try:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=900)
        except subprocess.TimeoutExpired as exc:
            raise RenderError("audio mux command timed out") from exc
        if result.returncode != 0:
            raise RenderError(result.stderr[-1200:] or result.stdout[-1200:])
        temp_output.replace(output)
        return True
    finally:
        # A failed or timed-out mux can leave a partial `.audio-mux` file next to
        # the real output. The source mp4 is never touched (replace is past the
        # raise), but the half-written temp turd must not linger. ponytail: one
        # finally, missing_ok swallows the success path where it was renamed away.
        temp_output.unlink(missing_ok=True)


def _missing_assets(project: dict[str, Any], asset_root: Path | None) -> list[dict[str, str]]:
    assets = project.get("assets") or {}
    missing: list[dict[str, str]] = []
    for clip in _enabled_clips(project):
        asset = assets.get(clip.get("assetId")) or {}
        src = _resolve_asset_src(str(asset.get("src") or ""), asset_root)
        if src and not src.exists():
            missing.append({
                "clipId": str(clip.get("id") or ""),
                "assetId": str(clip.get("assetId") or ""),
                "src": str(src),
            })
    return missing


def _resolve_asset_src(src: str, asset_root: Path | None) -> Path:
    """Path-typed view of the canonical ``/agenticnews-assets/`` resolver.

    The single source of truth is ``openshot_bridge._resolve_asset_src`` (str ->
    str); this wrapper exists only because the ffmpeg fallback's callers need a
    ``Path`` to call ``.exists()`` on. ponytail: one resolver, one coercion."""
    return Path(openshot_bridge._resolve_asset_src(src, asset_root=asset_root))


def _fps(project: dict[str, Any]) -> float:
    return float(project.get("fps") or 30)


def _frame_number(seconds: float, fps: float) -> int:
    return max(1, int(math.floor(max(0.0, seconds) * fps)) + 1)


def _windowed_clips(project: dict[str, Any], *, window_start: float, duration: float) -> list[dict[str, Any]]:
    window_end = window_start + max(0.1, duration)
    windowed: list[dict[str, Any]] = []
    for clip in _enabled_clips(project):
        clip_start = float(clip.get("start") or 0)
        clip_duration = float(clip.get("duration") or 0)
        clip_end = clip_start + clip_duration
        if clip_end <= window_start or clip_start >= window_end:
            continue
        overlap_start = max(clip_start, window_start)
        overlap_end = min(clip_end, window_end)
        front_trim = max(0.0, window_start - clip_start)
        next_clip = dict(clip)
        next_clip["start"] = overlap_start - window_start
        windowed_duration = max(0.001, overlap_end - overlap_start)
        next_clip["duration"] = windowed_duration
        next_clip["sourceStart"] = float(clip.get("sourceStart") or 0) + front_trim
        # Keyframe point `t` is seconds relative to the clip start. Trimming the
        # clip's front by `front_trim` shifts that origin, so each point must move
        # down by the same amount (clamped at 0) or its animation fires too late.
        if front_trim and clip.get("keyframes"):
            next_clip["keyframes"] = _shift_keyframes(clip["keyframes"], -front_trim)
        # Effects with a `duration` param (fadeIn/fadeOut) assume the ORIGINAL clip
        # length. A windowed clip is shorter, so the param must be re-fit to the new
        # window or it overruns it (a 1s fadeOut on a clip windowed to 2s is fine; on
        # one windowed to 0.5s it would silence the whole clip). fadeIn is also
        # start-anchored, so a front trim eats into it. Adjust both renderers' shared
        # source-of-truth here so OpenShot (effects array) and ffmpeg (_fade_window)
        # stay in sync.
        if clip.get("effects") and (front_trim or windowed_duration < clip_duration):
            next_clip["effects"] = _refit_effects(clip["effects"], front_trim, windowed_duration)
        windowed.append(next_clip)
    return windowed


def _refit_effects(
    effects: list[dict[str, Any]], front_trim: float, windowed_duration: float
) -> list[dict[str, Any]]:
    """Re-fit duration-bearing effects onto a windowed (shortened) clip.

    fadeIn and crossfade are start-anchored (both map to OpenShot's `in` Fade, see
    openshot_bridge._FADE_DIRECTION_MAP): a front trim of `front_trim` removes that
    much of their ramp. fadeOut is end-anchored: a front trim leaves it alone, but no
    fade may be longer than the windowed clip. Effects with no `duration` pass through.

    A fade whose duration re-fits to <= 0 (the front trim ate the whole start-anchored
    ramp, e.g. a 0.5s crossfade with a 0.6s front trim, or any fade on a zero-length
    window) is DROPPED entirely rather than kept as a `duration: 0.0` no-op. OpenShot's
    Fade silently ignores a 0-second fade (openshot_bridge exports it as a valid but
    meaningless `duration: 0.0` keyframe), so emitting it breaches the contract — the
    timeline shows a fade that the compiled video never renders. Dropping it keeps the
    editor state and the rendered output in agreement."""

    refit: list[dict[str, Any]] = []
    for effect in effects:
        effect = effect or {}
        params = effect.get("params") or {}
        original = params.get("duration")
        if original is None:
            refit.append(effect)
            continue
        new_duration = float(original)
        if str(effect.get("type") or "") in ("fadeIn", "crossfade"):
            new_duration -= front_trim
        new_duration = min(new_duration, windowed_duration)
        if new_duration <= 0.0:
            continue
        refit.append({**effect, "params": {**params, "duration": new_duration}})
    return refit


def _shift_keyframes(keyframes: list[dict[str, Any]], delta: float) -> list[dict[str, Any]]:
    """Shift a keyframe envelope's `t` by `delta` (negative for a front trim).

    Points pushed below t=0 by a front trim are NOT clamped: clamping pins each
    out-of-window point to t=0 keeping its stale value, which both mis-fires the
    envelope at the new window start AND can emit several points at t=0 (a corrupt
    multi-value origin). Instead the sub-zero points are dropped and replaced by ONE
    synthetic point at t=0 carrying the value the envelope linearly held at the trim
    boundary, so the windowed clip's animation starts at the right value. This is the
    split->window seam: a split clip's tail is already rebased to its own t=0, and a
    window that trims into it past its first keyframe must re-anchor, not clamp."""

    shifted: list[dict[str, Any]] = []
    for track in keyframes:
        raw = sorted(
            (track.get("points") or []),
            key=lambda p: float(p.get("t") or 0.0),
        )
        moved = [
            {**point, "t": float(point.get("t") or 0.0) + delta}
            for point in raw
        ]
        kept = [p for p in moved if p["t"] >= 0.0]
        below = [p for p in moved if p["t"] < 0.0]
        if below and (not kept or kept[0]["t"] > 0.0):
            # Re-anchor at t=0 with the boundary value the envelope held there.
            last_below = below[-1]
            if kept:
                first_above = kept[0]
                span = first_above["t"] - last_below["t"]
                v0 = float(last_below.get("value") or 0.0)
                v1 = float(first_above.get("value") or 0.0)
                # The crossing segment's interp is the END point's (OpenShot convention).
                if span <= 0 or str(first_above.get("interp") or "linear") == "constant":
                    boundary_value = v0
                else:
                    boundary_value = v0 + (v1 - v0) * ((0.0 - last_below["t"]) / span)
            else:
                # Whole envelope trimmed off the front: hold the last value.
                boundary_value = float(last_below.get("value") or 0.0)
            kept.insert(
                0,
                {**last_below, "t": 0.0, "value": boundary_value},
            )
        shifted.append({**track, "points": kept})
    return shifted


# Visual fade effect types whose `duration` param must be baked into an `opacity`
# keyframe ramp for the OpenShot render. This libopenshot build ships NO "Fade"
# effect class (EffectInfo lists 37 effects; "Fade" is not among them), so the
# bridge's crossfade/fadeIn/fadeOut -> "Fade" mapping loads via SetJson but is
# silently ignored at render time — the timeline shows a fade the compiled video
# never renders. OpenShot itself renders fades as clip `alpha` keyframes (what its
# own Qt editor writes), and the bridge already turns an `opacity` keyframe track
# into alpha keyframes. So bake the fade window into opacity here, on the render
# path only (stored project keeps its declarative effect). fadeIn/crossfade are
# start-anchored (0 -> 1 over duration); fadeOut is end-anchored (1 -> 0 over the
# trailing duration).
_VISUAL_FADE_TYPES = {"fadeIn", "crossfade", "fadeOut"}


def _bake_fade_keyframes(clip: dict[str, Any]) -> dict[str, Any]:
    """Return a render-clip with visual fade effects expressed as opacity keyframes.

    Without this the OpenShot render drops every visual fade (no Fade effect class
    in libopenshot), so the re-fit crossfade duration the windowing path computes
    never animates the compiled frames — the exact split->window->export seam the
    contract test guards. The fade is layered onto the clip's existing opacity
    envelope (multiplicatively at the fade boundaries) so a clip that both fades and
    animates opacity keeps both. Effects are left intact; the bridge ignores them."""

    fades = [e for e in (clip.get("effects") or [])
             if str((e or {}).get("type") or "") in _VISUAL_FADE_TYPES]
    if not fades:
        return clip
    duration = max(0.001, float(clip.get("duration") or 0.001))
    fade_in = max((float((e.get("params") or {}).get("duration") or 0.0)
                   for e in fades if e.get("type") in ("fadeIn", "crossfade")), default=0.0)
    fade_out = max((float((e.get("params") or {}).get("duration") or 0.0)
                    for e in fades if e.get("type") == "fadeOut"), default=0.0)
    fade_in = min(max(0.0, fade_in), duration)
    fade_out = min(max(0.0, fade_out), duration)
    if fade_in <= 0.0 and fade_out <= 0.0:
        return clip

    # The clip's existing opacity envelope (if any) is the base curve; the fade is
    # a 0..1 multiplier ramp layered on top. When there is NO base envelope the
    # bare ramp IS the opacity, so emit exactly the ramp points (cheapest, and the
    # shape downstream/contract tests expect). When a base envelope DOES exist,
    # compose them multiplicatively (the docstring's promise) so a clip that both
    # fades and animates opacity keeps both, instead of the fade clobbering the
    # envelope. Non-opacity tracks (scale/x/y/rotation/volume) are always kept.
    base = _keyframe_points(clip, "opacity")  # sorted (t, value, interp)
    points: list[dict[str, Any]] = []

    if not base:
        if fade_in > 0.0:
            points.append({"t": 0.0, "value": 0.0, "interp": "linear"})
            points.append({"t": fade_in, "value": 1.0, "interp": "linear"})
        if fade_out > 0.0:
            out_start = max(fade_in, duration - fade_out)
            points.append({"t": out_start, "value": 1.0, "interp": "linear"})
            points.append({"t": duration, "value": 0.0, "interp": "linear"})
    else:
        def _fade_factor(t: float) -> float:
            f = 1.0
            if fade_in > 0.0:
                f *= min(1.0, max(0.0, t / fade_in))
            if fade_out > 0.0:
                out_start = max(fade_in, duration - fade_out)
                if t >= out_start:
                    f *= min(1.0, max(0.0, (duration - t) / fade_out))
            return f

        def _base_value(t: float) -> float:
            if t <= base[0][0]:
                return base[0][1]
            if t >= base[-1][0]:
                return base[-1][1]
            for (t0, v0, _i0), (t1, v1, _i1) in zip(base, base[1:]):
                if t0 <= t <= t1:
                    span = t1 - t0
                    return v1 if span <= 0 else v0 + (v1 - v0) * ((t - t0) / span)
            return base[-1][1]

        # Sample at every fade boundary AND every existing opacity keyframe time, so
        # neither curve's corners are smoothed away by the linear segments between.
        times = {0.0, duration}
        if fade_in > 0.0:
            times.add(fade_in)
        if fade_out > 0.0:
            times.add(max(fade_in, duration - fade_out))
        times.update(t for (t, _v, _i) in base if 0.0 <= t <= duration)
        points = [
            {"t": t, "value": _base_value(t) * _fade_factor(t), "interp": "linear"}
            for t in sorted(times)
        ]

    next_clip = dict(clip)
    tracks = [dict(t) for t in (clip.get("keyframes") or []) if t.get("property") != "opacity"]
    tracks.append({"property": "opacity", "points": points})
    next_clip["keyframes"] = tracks
    return next_clip


def _render_scope_project(project: dict[str, Any], *, window_start: float, duration: float | None) -> dict[str, Any]:
    if window_start <= 0 and duration is None:
        clips = project.get("clips") or {}
        # Identity fast-path: only copy + bake when a clip actually carries a visual
        # fade (otherwise there's nothing to translate and callers rely on identity).
        if not any(
            str((e or {}).get("type") or "") in _VISUAL_FADE_TYPES
            for clip in clips.values()
            for e in (clip.get("effects") or [])
        ):
            return project
        scoped = dict(project)
        scoped["clips"] = {cid: _bake_fade_keyframes(clip) for cid, clip in clips.items()}
        return scoped
    render_duration = duration
    if render_duration is None:
        render_duration = max(0.1, _project_duration(project) - window_start)
    scoped = dict(project)
    scoped_clips = _windowed_clips(project, window_start=window_start, duration=float(render_duration))
    scoped["clips"] = {
        str(clip.get("id") or f"clip_{index}"): _bake_fade_keyframes(clip)
        for index, clip in enumerate(scoped_clips)
    }
    return scoped


def _overlay_expr(clip: dict[str, Any]) -> tuple[str, str]:
    """Overlay x/y position expressions. A static transform fraction yields a
    constant; an x/y keyframe envelope yields a piecewise-linear `t`-expression
    (overlay's x/y accept the timeline `t`), so position animates in the fallback
    the way it does through OpenShot's location_x/location_y keyframes."""

    transform = clip.get("transform") or {}
    x_pts = _keyframe_points(clip, "x")
    y_pts = _keyframe_points(clip, "y")
    x_frac = _piecewise_linear_expr(x_pts) if x_pts else f"{_num(transform.get('x'), 0.5):.6f}"
    y_frac = _piecewise_linear_expr(y_pts) if y_pts else f"{_num(transform.get('y'), 0.5):.6f}"
    return f"(main_w-overlay_w)*({x_frac})", f"(main_h-overlay_h)*({y_frac})"


def _run(cmd: list[str]) -> None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"render command timed out: {' '.join(cmd[:4])} ...") from exc
    except (FileNotFoundError, OSError) as exc:
        # ffmpeg detected at __init__ via shutil.which can vanish before the render
        # actually runs (uninstalled mid-job, PATH change, broken symlink). Without
        # this the fallback crashes with a bare OSError the caller can't classify;
        # surface it as the same RenderError every other failure mode raises.
        raise RenderError(f"render command could not be executed ({cmd[0]}): {exc}") from exc
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
    # ffmpeg can exit 0 yet leave a truncated/corrupt artifact whose container has
    # no parseable `format=duration` (empty / `N/A` stdout). float("")/float("N/A")
    # raises a ValueError the renderer would otherwise leak as an uncaught crash;
    # turn the corrupt-output case into the standard RenderError instead.
    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise RenderError(
            f"render produced an unprobeable artifact (no duration: {raw!r}): {output}"
        ) from exc
