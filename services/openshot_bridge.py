"""OpenShot/libopenshot bridge contract for Editor Bay timelines.

This module does not import libopenshot. It translates Editor Bay state into
the JSON shapes consumed by OpenShot's own TimelineSync path:

- full loads: ``openshot.Timeline.SetJson(project_json)``
- incremental edits: ``openshot.Timeline.ApplyJsonDiff([UpdateAction])``

Keeping this pure-Python makes the command contract testable even on machines
where the OpenShot Python bindings are not installed yet.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


BEZIER = 0
LINEAR = 1
CONSTANT = 2
GRAVITY_CENTER = 4
SCALE_NONE = 3
ANCHOR_CANVAS = 0
FRAME_DISPLAY_NONE = 0
VOLUME_MIX_NONE = 0
COMPOSITE_SOURCE_OVER = 0


# ---------------------------------------------------------------------------
# Production asset vocabulary — the CLOSED list of source types an ABN episode
# is built from. This is the single source of truth for "what kinds of footage
# exist." An episode is heterogeneous layers from these sources, composited by
# OpenShot. None of them is "the renderer" — OpenShot is the compiler; these are
# its inputs. Do not treat any source here as legacy/slop to remove (Remotion in
# particular keeps getting wrongly ripped out — it is a live layer source).
#
# `source` is the production type that goes on a clip/asset as `asset["source"]`.
# `media` is the low-level kind libopenshot actually reads (what `type` resolves
# to). Several sources collapse to the same media kind — that's the point: a
# "video" clip can be a Remotion terminal shot, a Playwright webscroll, or a
# rasterized CSS card, and the `source` tag is how we keep them distinguishable.
SOURCE_TYPES: dict[str, dict[str, str]] = {
    "remotion": {
        "media": "video",
        "produces": "terminal / code-graphics shots (kinetic terminals, data HUDs)",
        "tool": "yt-pipeline/remotion/ (src/scenes/)",
    },
    "webscroll": {
        "media": "video",
        "produces": "real browser footage of source pages — Playwright-driven "
        "(scroll today; moving toward authentic mouse interaction)",
        "tool": "tools/gh-capture/capture_nav.cjs (Playwright/chromium)",
    },
    "css": {
        "media": "video",
        "produces": "rasterized seekable-HTML kinetic type / stat / quote cards",
        "tool": "tools/seekable-html-video/render_seekable.cjs",
    },
    "broll": {
        "media": "video",
        "produces": "ambient background plates (under framed footage / cards)",
        "tool": "broll_library/",
    },
    "still": {
        "media": "image",
        "produces": "static frames, source screenshots, title cards",
        "tool": "render_seekable.cjs / capture",
    },
    "vo": {
        "media": "audio",
        "produces": "narration — Pocket-TTS built-in English voice ONLY",
        "tool": "abn_factory._voice / pocket-tts",
    },
    "bed": {
        "media": "audio",
        "produces": "music bed (ducked under VO)",
        "tool": "branding/",
    },
}


def source_media_type(source: str) -> str:
    """Map a production source type (remotion/webscroll/css/…) to its libopenshot
    media kind (video/image/audio). Unknown sources fall through unchanged so the
    raw media `type` still works."""

    entry = SOURCE_TYPES.get(str(source or "").lower())
    return entry["media"] if entry else str(source or "")


def timeline_json(project: dict[str, Any], *, asset_root: Path | str | None = None) -> dict[str, Any]:
    """Return a libopenshot Timeline JSON payload for an Editor Bay project."""

    duration = _project_duration(project)
    return {
        "id": str(project.get("projectId") or "editor_project"),
        "type": "Timeline",
        "fps": {"num": int(project.get("fps") or 30), "den": 1},
        "width": int(project.get("width") or 1920),
        "height": int(project.get("height") or 1080),
        "duration": duration,
        "sample_rate": int(project.get("sampleRate") or 48000),
        "channels": int(project.get("channels") or 2),
        "channel_layout": int(project.get("channelLayout") or 3),
        "path": "",
        "clips": [clip_json(project, clip, asset_root=asset_root) for clip in _ordered_enabled_clips(project)],
        "effects": [],
    }


def update_actions_from_command_log(
    project: dict[str, Any], *, asset_root: Path | str | None = None
) -> list[dict[str, Any]]:
    """Convert Editor Bay command-log entries into OpenShot UpdateActions."""

    actions: list[dict[str, Any]] = []
    for entry in project.get("commandLog") or []:
        action = update_action_from_command(project, entry, asset_root=asset_root)
        if action:
            actions.append(action)
    return actions


def update_action_from_command(
    project: dict[str, Any], entry: dict[str, Any], *, asset_root: Path | str | None = None
) -> dict[str, Any] | None:
    """Convert one persisted Editor Bay command entry to an OpenShot action.

    Non-timeline commands such as notes and markers intentionally return None;
    OpenShot-Qt's TimelineSync also ignores keys like files, markers, layers,
    scale, profile, history, and export_settings.
    """

    op = str(entry.get("op") or "")
    after = entry.get("after") or {}
    before = entry.get("before") or {}

    if op == "clip.create" and after.get("clip"):
        return _update_action(
            "insert",
            ["clips"],
            clip_json(project, after["clip"], asset_root=asset_root),
            old_values=None,
            transaction=entry.get("id"),
        )

    if op == "clip.split":
        actions: list[dict[str, Any]] = []
        if after.get("clip"):
            actions.append(
                _update_action(
                    "update",
                    ["clips", {"id": after["clip"]["id"]}],
                    clip_json(project, after["clip"], asset_root=asset_root),
                    old_values=clip_json(project, before["clip"], asset_root=asset_root) if before.get("clip") else None,
                    transaction=entry.get("id"),
                )
            )
        if after.get("createdClip"):
            actions.append(
                _update_action(
                    "insert",
                    ["clips"],
                    clip_json(project, after["createdClip"], asset_root=asset_root),
                    old_values=None,
                    transaction=entry.get("id"),
                )
            )
        return {"batch": actions} if actions else None

    if op == "clip.unsplit":
        actions: list[dict[str, Any]] = []
        if after.get("clip"):
            actions.append(
                _update_action(
                    "update",
                    ["clips", {"id": after["clip"]["id"]}],
                    clip_json(project, after["clip"], asset_root=asset_root),
                    old_values=clip_json(project, before["clip"], asset_root=asset_root) if before.get("clip") else None,
                    transaction=entry.get("id"),
                )
            )
        deleted_clip_id = after.get("deletedClipId")
        if deleted_clip_id:
            actions.append(
                _update_action(
                    "delete",
                    ["clips", {"id": deleted_clip_id}],
                    None,
                    old_values=clip_json(project, before["createdClip"], asset_root=asset_root)
                    if before.get("createdClip")
                    else None,
                    transaction=entry.get("id"),
                )
            )
        return {"batch": actions} if actions else None

    if op == "clip.keyframes":
        # editor_timeline records the full post-edit clip in after["clip"], but a
        # keyframes-only edit may instead carry just the new envelope in the payload.
        # Resolve the target clip from whichever block has it and overlay the new
        # keyframes so the update never gets dropped (the bare catch-all below would
        # return None when after["clip"] is absent, silently losing the keyframes).
        target = after.get("clip") or before.get("clip")
        if not target:
            return None
        target = dict(target)
        payload = entry.get("payload") or {}
        if not after.get("clip") and "keyframes" in payload:
            target["keyframes"] = payload["keyframes"]
        return _update_action(
            "update",
            ["clips", {"id": target["id"]}],
            clip_json(project, target, asset_root=asset_root),
            old_values=clip_json(project, before["clip"], asset_root=asset_root) if before.get("clip") else None,
            transaction=entry.get("id"),
        )

    if op.startswith("clip.") and after.get("clip"):
        return _update_action(
            "update",
            ["clips", {"id": after["clip"]["id"]}],
            clip_json(project, after["clip"], asset_root=asset_root),
            old_values=clip_json(project, before["clip"], asset_root=asset_root) if before.get("clip") else None,
            transaction=entry.get("id"),
        )

    return None


def flattened_update_actions(
    project: dict[str, Any], *, asset_root: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return OpenShot UpdateActions with split-command batches flattened."""

    out: list[dict[str, Any]] = []
    for action in update_actions_from_command_log(project, asset_root=asset_root):
        if "batch" in action:
            out.extend(action["batch"])
        else:
            out.append(action)
    return out


def json_diff(project: dict[str, Any], *, asset_root: Path | str | None = None) -> str:
    """Serialize project command history as an ApplyJsonDiff payload."""

    return json.dumps(flattened_update_actions(project, asset_root=asset_root))


def clip_json(project: dict[str, Any], clip: dict[str, Any], *, asset_root: Path | str | None = None) -> dict[str, Any]:
    """Return a libopenshot Clip JSON payload for an Editor Bay clip."""

    asset = (project.get("assets") or {}).get(clip.get("assetId")) or {}
    transform = clip.get("transform") or {}
    scale = max(0.0, float(transform.get("scale", 1.0)))
    opacity = 0.0 if not clip.get("enabled", True) else float(transform.get("opacity", 1.0))
    volume = 0.0 if clip.get("muted") else float(clip.get("volume", 1.0))
    source_start = float(clip.get("sourceStart") or 0.0)
    duration = max(0.001, float(clip.get("duration") or 0.001))
    fps = int(project.get("fps") or 30)

    payload = {
        "id": str(clip.get("id")),
        "position": float(clip.get("start") or 0.0),
        "layer": _layer_number(project, str(clip.get("trackId") or "")),
        "start": source_start,
        "end": source_start + duration,
        "duration": duration,
        "parentObjectId": "",
        "gravity": GRAVITY_CENTER,
        "scale": SCALE_NONE,
        "anchor": ANCHOR_CANVAS,
        "display": FRAME_DISPLAY_NONE,
        "mixing": VOLUME_MIX_NONE,
        "composite": COMPOSITE_SOURCE_OVER,
        "waveform": False,
        "waveform_mode": 0,
        "reader_orientation_mode": "reader",
        "scale_x": _keyframe(scale),
        "scale_y": _keyframe(scale),
        "location_x": _keyframe(_location(float(transform.get("x", 0.5)))),
        "location_y": _keyframe(_location(float(transform.get("y", 0.5)))),
        "alpha": _keyframe(max(0.0, min(1.0, opacity))),
        "rotation": _keyframe(float(transform.get("rotation", 0.0))),
        "time": _keyframe(1.0),
        "volume": _keyframe(_openshot_volume(volume)),
        "wave_color": _color_keyframe(0, 123, 255, 255),
        "shear_x": _keyframe(0.0),
        "shear_y": _keyframe(0.0),
        "origin_x": _keyframe(0.5),
        "origin_y": _keyframe(0.5),
        "channel_filter": _keyframe(-1),
        "channel_mapping": _keyframe(-1),
        "has_audio": _keyframe(1 if _asset_type(asset) == "audio" else 0),
        "has_video": _keyframe(0 if _asset_type(asset) == "audio" else 1),
        "perspective_c1_x": _keyframe(0.0),
        "perspective_c1_y": _keyframe(0.0),
        "perspective_c2_x": _keyframe(0.0),
        "perspective_c2_y": _keyframe(0.0),
        "perspective_c3_x": _keyframe(0.0),
        "perspective_c3_y": _keyframe(0.0),
        "perspective_c4_x": _keyframe(0.0),
        "perspective_c4_y": _keyframe(0.0),
        "effects": [effect_json(effect, fps=fps) for effect in (clip.get("effects") or [])],
        "reader": reader_json(project, asset, duration=source_start + duration, asset_root=asset_root),
    }
    # Per-keyframe envelopes (dynamic volume/opacity/scale/position/rotation)
    # override the flat single-point defaults above. Empty / absent -> flat.
    payload.update(_keyframe_overrides(clip.get("keyframes"), fps=fps, source_start=source_start))
    return payload


def reader_json(
    project: dict[str, Any],
    asset: dict[str, Any],
    *,
    duration: float,
    asset_root: Path | str | None = None,
) -> dict[str, Any]:
    src = _resolve_asset_src(str(asset.get("src") or ""), asset_root=asset_root)
    asset_type = _asset_type(asset)
    reader_type = "DummyReader"
    if src:
        reader_type = "FFmpegReader" if asset_type in {"video", "audio"} else "QtImageReader"

    return {
        "id": str(asset.get("id") or Path(src).stem or "reader"),
        "type": reader_type,
        "path": src,
        "duration": max(0.001, float(duration)),
        "width": int(project.get("width") or 1920),
        "height": int(project.get("height") or 1080),
        "fps": {"num": int(project.get("fps") or 30), "den": 1},
        "video_length": max(1, int(max(0.001, float(duration)) * int(project.get("fps") or 30))),
        "sample_rate": int(project.get("sampleRate") or 48000),
        "channels": int(project.get("channels") or 2),
        "channel_layout": int(project.get("channelLayout") or 3),
        "has_audio": asset_type == "audio",
        "has_video": asset_type != "audio",
    }


# Map an editor effect `type` (validated in editor_timeline.EFFECT_TYPES) to the
# libopenshot effect class name. fadeIn/fadeOut/crossfade all use OpenShot's Fade
# effect (its `fade` / `type` fields select the direction); color filters map to
# their own effect classes.
_EFFECT_CLASS_MAP: dict[str, str] = {
    "fadeIn": "Fade",
    "fadeOut": "Fade",
    "crossfade": "Fade",
    "brightness": "Brightness",
    "saturation": "Saturation",
}

_FADE_DIRECTION_MAP = {"fadeIn": "in", "fadeOut": "out", "crossfade": "in"}


def effect_json(effect: dict[str, Any], *, fps: int) -> dict[str, Any]:
    """Translate one editor clip effect into a libopenshot Effect JSON object.

    The editor effect ``{"id", "type", "params"}`` (closed vocabulary, validated
    upstream) becomes an OpenShot effect: a `type` class name plus keyframed
    properties. Unknown types fall through with the raw type so nothing is silently
    dropped — but editor_timeline rejects those before they ever get here."""

    effect_type = str(effect.get("type") or "")
    params = effect.get("params") or {}
    out: dict[str, Any] = {
        "id": str(effect.get("id") or ""),
        "type": _EFFECT_CLASS_MAP.get(effect_type, effect_type),
    }
    if effect_type in _FADE_DIRECTION_MAP:
        out["fade"] = _FADE_DIRECTION_MAP[effect_type]
        out["duration"] = _keyframe(float(params.get("duration") or 0.0))
    elif effect_type == "brightness":
        out["brightness"] = _keyframe(float(params.get("value") or 0.0))
        out["contrast"] = _keyframe(0.0)
    elif effect_type == "saturation":
        out["saturation"] = _keyframe(float(params.get("value") or 0.0))
    return out


def _update_action(
    action_type: str,
    key: list[Any],
    value: dict[str, Any],
    *,
    old_values: dict[str, Any] | None,
    transaction: str | None,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "key": key,
        "value": value,
        "old_values": old_values,
        "transaction": transaction,
    }


def _keyframe(value: float | int) -> dict[str, Any]:
    return {
        "Points": [
            {
                "co": {"X": 1.0, "Y": value},
                "interpolation": CONSTANT,
            }
        ]
    }


def _color_keyframe(red: int, green: int, blue: int, alpha: int) -> dict[str, Any]:
    return {
        "red": _keyframe(red),
        "green": _keyframe(green),
        "blue": _keyframe(blue),
        "alpha": _keyframe(alpha),
    }


def _openshot_volume(editor_volume: float) -> float:
    """Convert Editor Bay 0..1 volume values to OpenShot's 0..100 scale."""

    return max(0.0, float(editor_volume) * 100.0)


def _location(value: float) -> float:
    return (max(0.0, min(1.0, value)) - 0.5) * 2.0


# libopenshot's openshot::InterpolationType enum is {BEZIER=0, LINEAR=1, CONSTANT=2}.
# Bezier MUST map to 0 — not a fall-through default — so smooth ducking/opacity/
# position curves from abn_factory render as curves instead of silently linearizing.
_INTERPOLATION_MAP = {"linear": LINEAR, "constant": CONSTANT, "bezier": BEZIER}


# Map an editor keyframe `property` to the OpenShot clip-JSON key(s) it animates,
# plus the value transform applied to each point (same transforms the flat
# defaults use above, so a 1-point envelope reproduces the static value).
# Defined after _openshot_volume/_location so the dict values resolve at import.
_KEYFRAME_PROPERTY_MAP: dict[str, tuple[tuple[str, ...], Any]] = {
    "volume": (("volume",), _openshot_volume),
    "opacity": (("alpha",), lambda v: max(0.0, min(1.0, float(v)))),
    "scale": (("scale_x", "scale_y"), lambda v: max(0.0, float(v))),
    "x": (("location_x",), _location),
    "y": (("location_y",), _location),
    "rotation": (("rotation",), float),
}


def _keyframe_overrides(keyframes: Any, *, fps: int, source_start: float = 0.0) -> dict[str, Any]:
    if not keyframes:
        return {}
    out: dict[str, Any] = {}
    for track in keyframes:
        prop = str((track or {}).get("property") or "")
        mapping = _KEYFRAME_PROPERTY_MAP.get(prop)
        if not mapping:
            continue
        keys, transform = mapping
        points = [
            {
                # `t` is editor time relative to clip start; OpenShot keyframe X is
                # a frame number in the clip's source-reader space, so trimmed clips
                # (sourceStart > 0) must offset by source_start (matches start/end).
                "co": {"X": (source_start + float(point.get("t") or 0.0)) * fps + 1.0, "Y": transform(point.get("value"))},
                "interpolation": _INTERPOLATION_MAP.get(str(point.get("interp") or "linear"), LINEAR),
            }
            for point in (track or {}).get("points") or []
        ]
        if not points:
            continue
        for key in keys:
            out[key] = {"Points": points}
    return out


def _layer_number(project: dict[str, Any], track_id: str) -> int:
    track = (project.get("tracks") or {}).get(track_id) or {}
    return int(track.get("index") or 0)


def _asset_type(asset: dict[str, Any]) -> str:
    """Resolve an asset to its libopenshot media kind. A production `source`
    (remotion/webscroll/css/…) wins and is mapped via SOURCE_TYPES; otherwise the
    raw `type` is used directly."""

    source = asset.get("source")
    if source:
        return source_media_type(source)
    return str(asset.get("type") or "")


def _ordered_enabled_clips(project: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = project.get("tracks") or {}
    clips = [copy.deepcopy(c) for c in (project.get("clips") or {}).values() if c.get("enabled", True)]
    return sorted(
        clips,
        key=lambda clip: (
            int((tracks.get(clip.get("trackId")) or {}).get("index") or 0),
            float(clip.get("start") or 0.0),
            str(clip.get("id") or ""),
        ),
    )


def _project_duration(project: dict[str, Any]) -> float:
    clips = _ordered_enabled_clips(project)
    if not clips:
        return 1.0
    return max(0.1, max(float(c.get("start") or 0.0) + float(c.get("duration") or 0.0) for c in clips))


def _resolve_asset_src(src: str, *, asset_root: Path | str | None) -> str:
    if src.startswith("/agenticnews-assets/") and asset_root:
        return str(Path(asset_root) / src.removeprefix("/agenticnews-assets/"))
    return src
