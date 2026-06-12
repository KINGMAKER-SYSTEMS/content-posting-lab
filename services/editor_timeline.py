"""Non-destructive Editor Bay timeline command core.

The UI, agents, and render workers should all mutate project state through this
module. The render backend is deliberately out of scope for this slice.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = "editor-timeline/v1"


class TimelineError(Exception):
    """Base exception for timeline command errors."""


class RevisionConflict(TimelineError):
    """Raised when a command targets a stale project revision."""


class CommandValidationError(TimelineError):
    """Raised when a command payload is invalid."""


def _now() -> float:
    return time.time()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _default_tracks() -> dict[str, dict[str, Any]]:
    return {
        "video_1": {"id": "video_1", "kind": "video", "name": "Video 1", "index": 10, "locked": False},
        "graphics_1": {"id": "graphics_1", "kind": "graphics", "name": "Graphics 1", "index": 20, "locked": False},
        "titles_1": {"id": "titles_1", "kind": "title", "name": "Titles 1", "index": 30, "locked": False},
        "audio_1": {"id": "audio_1", "kind": "audio", "name": "Voice", "index": 40, "locked": False},
        "music_1": {"id": "music_1", "kind": "audio", "name": "Music", "index": 50, "locked": False},
    }


def new_project(
    project_id: str,
    *,
    source_episode_id: str | None = None,
    title: str = "",
    fps: int = 30,
    width: int = 1920,
    height: int = 1080,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "projectId": project_id,
        "sourceEpisodeId": source_episode_id,
        "title": title,
        "fps": fps,
        "width": width,
        "height": height,
        "revision": 0,
        "assets": {},
        "tracks": _default_tracks(),
        "clips": {},
        "markers": {},
        "notes": {},
        "effects": {},
        "keyframes": {},
        "renderCache": {},
        "commandLog": [],
        "createdAt": _now(),
        "updatedAt": _now(),
    }


def project_from_abn_timeline(
    project_id: str, abn_timeline: dict[str, Any], *, source_episode_id: str | None = None
) -> dict[str, Any]:
    project = new_project(
        project_id,
        source_episode_id=source_episode_id or abn_timeline.get("episodeId"),
        title=abn_timeline.get("title", ""),
        fps=int(abn_timeline.get("fps") or 30),
        width=int(abn_timeline.get("width") or 1920),
        height=int(abn_timeline.get("height") or 1080),
    )
    cursor = 0.0
    for seg_index, segment in enumerate(abn_timeline.get("segments") or []):
        segment_id = segment.get("segmentId") or f"s{seg_index}"
        duration = float(segment.get("durationSec") or 0)
        for shot_index, shot in enumerate(segment.get("shots") or []):
            src = shot.get("src")
            if not src:
                continue
            kind = _asset_kind(src, shot.get("type"))
            asset_id = _stable_id("asset", segment_id, str(shot.get("id") or shot_index), Path(str(src)).name)
            clip_id = _stable_id("clip", segment_id, str(shot.get("id") or shot_index), Path(str(src)).name)
            project["assets"][asset_id] = {
                "id": asset_id,
                "type": kind,
                "src": src,
                "metadata": {"segmentId": segment_id, "shotType": shot.get("type")},
            }
            start = cursor + float(shot.get("startSec") or 0)
            clip_duration = float(
                shot.get("durationSec")
                or max(0.0, float(shot.get("endSec") or 0) - float(shot.get("startSec") or 0))
                or duration
            )
            project["clips"][clip_id] = _clip(
                clip_id,
                asset_id,
                _track_for_asset(kind, shot.get("type")),
                start=start,
                duration=clip_duration,
                kind=shot.get("type") or kind,
                source_start=float(shot.get("clipStartSec") or 0),
                metadata={"segmentId": segment_id, "shot": shot},
            )

        vo = ((segment.get("audio") or {}).get("vo") or {})
        if vo.get("src"):
            asset_id = _stable_id("asset", segment_id, "vo", Path(str(vo["src"])).name)
            clip_id = _stable_id("clip", segment_id, "vo", Path(str(vo["src"])).name)
            project["assets"][asset_id] = {
                "id": asset_id,
                "type": "audio",
                "src": vo["src"],
                "metadata": {"segmentId": segment_id, "role": "voiceover"},
            }
            project["clips"][clip_id] = _clip(
                clip_id,
                asset_id,
                "audio_1",
                start=cursor,
                duration=float(vo.get("duration") or duration),
                kind="voiceover",
                metadata={"segmentId": segment_id},
            )

        for lt_index, lower in enumerate(segment.get("lowerThirds") or []):
            asset_id = _stable_id("asset", segment_id, "lower", str(lt_index))
            clip_id = _stable_id("clip", segment_id, "lower", str(lt_index))
            project["assets"][asset_id] = {
                "id": asset_id,
                "type": "title",
                "src": "",
                "metadata": {
                    "text": lower.get("headline", ""),
                    "sourceUrl": lower.get("sourceUrl", ""),
                },
            }
            project["clips"][clip_id] = _clip(
                clip_id,
                asset_id,
                "titles_1",
                start=cursor + float(lower.get("startSec") or 0),
                duration=float(lower.get("durationSec") or 0),
                kind="lower_third",
                metadata={"segmentId": segment_id, "lowerThird": lower},
            )
        cursor += duration
    return project


def _stable_id(prefix: str, *parts: str) -> str:
    clean = "_".join(
        "".join(ch.lower() if ch.isalnum() else "_" for ch in part).strip("_")
        for part in parts
        if part
    )
    clean = "_".join(part for part in clean.split("_") if part)
    return f"{prefix}_{clean[:80]}" if clean else _uid(prefix)


def _asset_kind(src: str, shot_type: str | None = None) -> str:
    suffix = Path(str(src)).suffix.lower()
    if suffix in {".wav", ".mp3", ".m4a", ".aac", ".flac"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".webm", ".mkv"}:
        return "video"
    if shot_type in {"title", "lower_third"}:
        return "title"
    return "image"


def _track_for_asset(asset_kind: str, shot_type: str | None = None) -> str:
    if asset_kind == "audio":
        return "audio_1"
    if shot_type in {"artifact", "card", "hook"} or asset_kind == "image":
        return "graphics_1"
    return "video_1"


def _clip(
    clip_id: str,
    asset_id: str,
    track_id: str,
    *,
    start: float,
    duration: float,
    kind: str,
    source_start: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": clip_id,
        "assetId": asset_id,
        "trackId": track_id,
        "kind": kind,
        "start": float(start),
        "duration": float(duration),
        "sourceStart": float(source_start),
        "enabled": True,
        "muted": False,
        "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [],
        "keyframes": [],
        "metadata": metadata or {},
    }


class TimelineStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, project_id: str) -> Path:
        return self.root / f"{project_id}.json"

    def load(self, project_id: str) -> dict[str, Any]:
        path = self.path_for(project_id)
        if not path.exists():
            raise FileNotFoundError(project_id)
        return json.loads(path.read_text())

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        project = copy.deepcopy(project)
        path = self.path_for(project["projectId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(project, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)
        return project

    def apply_command(self, project_id: str, command: dict[str, Any]) -> dict[str, Any]:
        project = self.load(project_id)
        project = apply_command(project, command)
        return self.save(project)

    def revert_last_command(self, project_id: str, *, actor: str) -> dict[str, Any]:
        project = self.load(project_id)
        if not project.get("commandLog"):
            raise CommandValidationError("project has no command to revert")
        last = project["commandLog"][-1]
        before = last.get("before") or {}
        if last.get("op") == "clip.trim" and before.get("clip"):
            clip = before["clip"]
            command = {
                "op": "clip.update",
                "actor": actor,
                "expectedRevision": project["revision"],
                "payload": {
                    "clipId": clip["id"],
                    "patch": {
                        "start": clip["start"],
                        "duration": clip["duration"],
                        "sourceStart": clip.get("sourceStart", 0),
                    },
                },
            }
            return self.apply_command(project_id, command)
        raise CommandValidationError(f"cannot revert command {last.get('op')}")


def apply_command(project: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    project = copy.deepcopy(project)
    expected = command.get("expectedRevision")
    if expected is None:
        raise CommandValidationError("expectedRevision is required")
    if int(expected) != int(project.get("revision", 0)):
        raise RevisionConflict(
            f"expected revision {expected}, current revision {project.get('revision', 0)}"
        )

    op = command.get("op")
    payload = command.get("payload") or {}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if op == "asset.import":
        asset = _asset_from_payload(payload)
        project["assets"][asset["id"]] = asset
        after["asset"] = copy.deepcopy(asset)
    elif op == "track.create":
        track = _track_from_payload(payload)
        project["tracks"][track["id"]] = track
        after["track"] = copy.deepcopy(track)
    elif op == "clip.create":
        clip = _clip_from_payload(project, payload)
        project["clips"][clip["id"]] = clip
        after["clip"] = copy.deepcopy(clip)
    elif op == "clip.split":
        clip = _require_clip(project, payload.get("clipId"))
        before["clip"] = copy.deepcopy(clip)
        split_at = _non_negative(payload.get("at"), "at")
        split_offset = split_at - float(clip["start"])
        if split_offset <= 0 or split_offset >= float(clip["duration"]):
            raise CommandValidationError("split point must be inside the clip")
        new_clip_id = payload.get("newClipId") or _uid("clip")
        if new_clip_id in project["clips"]:
            raise CommandValidationError(f"clip already exists: {new_clip_id}")
        second = copy.deepcopy(clip)
        second["id"] = str(new_clip_id)
        second["start"] = split_at
        second["duration"] = float(clip["duration"]) - split_offset
        second["sourceStart"] = float(clip.get("sourceStart", 0)) + split_offset
        second["metadata"] = {
            **(second.get("metadata") or {}),
            "splitFrom": clip["id"],
        }
        clip["duration"] = split_offset
        project["clips"][second["id"]] = second
        after["clip"] = copy.deepcopy(clip)
        after["createdClip"] = copy.deepcopy(second)
    elif op in {"clip.move", "clip.trim", "clip.update", "clip.hide", "clip.show", "clip.mute", "clip.unmute", "clip.transform", "clip.opacity", "clip.volume"}:
        clip = _require_clip(project, payload.get("clipId"))
        before["clip"] = copy.deepcopy(clip)
        _mutate_clip(op, clip, payload)
        after["clip"] = copy.deepcopy(clip)
    elif op == "marker.add":
        marker_id = payload.get("markerId") or _uid("marker")
        marker = {
            "id": marker_id,
            "time": _non_negative(payload.get("time"), "time"),
            "label": str(payload.get("label") or ""),
            "metadata": payload.get("metadata") or {},
        }
        project["markers"][marker_id] = marker
        after["marker"] = copy.deepcopy(marker)
    elif op == "note.add":
        note_id = payload.get("noteId") or _uid("note")
        note = {
            "id": note_id,
            "target": payload.get("target") or {},
            "text": str(payload.get("text") or ""),
            "suggestedCommand": payload.get("suggestedCommand"),
            "metadata": payload.get("metadata") or {},
        }
        if not note["text"]:
            raise CommandValidationError("note text is required")
        project["notes"][note_id] = note
        after["note"] = copy.deepcopy(note)
    else:
        raise CommandValidationError(f"unsupported command op: {op}")

    project["revision"] = int(project.get("revision", 0)) + 1
    entry = {
        "id": command.get("id") or _uid("cmd"),
        "op": op,
        "actor": command.get("actor") or "unknown",
        "expectedRevision": expected,
        "revision": project["revision"],
        "payload": payload,
        "before": before,
        "after": after,
        "ts": _now(),
    }
    project.setdefault("commandLog", []).append(entry)
    project["updatedAt"] = _now()
    return project


def replay_project(project: dict[str, Any]) -> dict[str, Any]:
    base = new_project(
        project["projectId"],
        source_episode_id=project.get("sourceEpisodeId"),
        title=project.get("title", ""),
        fps=int(project.get("fps") or 30),
        width=int(project.get("width") or 1920),
        height=int(project.get("height") or 1080),
    )
    base["tracks"] = copy.deepcopy(project.get("tracks") or _default_tracks())
    for entry in project.get("commandLog") or []:
        command = {
            "id": entry.get("id"),
            "op": entry["op"],
            "actor": entry.get("actor"),
            "expectedRevision": base["revision"],
            "payload": entry.get("payload") or {},
        }
        base = apply_command(base, command)
    return base


def _asset_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    asset_id = payload.get("assetId") or payload.get("id")
    src = payload.get("src")
    kind = payload.get("type")
    if not asset_id:
        raise CommandValidationError("assetId is required")
    if not kind:
        raise CommandValidationError("asset type is required")
    if src is None:
        raise CommandValidationError("asset src is required")
    return {
        "id": str(asset_id),
        "type": str(kind),
        "src": str(src),
        "metadata": payload.get("metadata") or {},
    }


def _track_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    track_id = payload.get("trackId") or payload.get("id")
    if not track_id:
        raise CommandValidationError("trackId is required")
    return {
        "id": str(track_id),
        "kind": str(payload.get("kind") or "video"),
        "name": str(payload.get("name") or track_id),
        "index": int(payload.get("index") or 0),
        "locked": bool(payload.get("locked") or False),
    }


def _clip_from_payload(project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    clip_id = payload.get("clipId") or payload.get("id")
    asset_id = payload.get("assetId")
    track_id = payload.get("trackId")
    if not clip_id:
        raise CommandValidationError("clipId is required")
    if asset_id not in project.get("assets", {}):
        raise CommandValidationError(f"asset does not exist: {asset_id}")
    if track_id not in project.get("tracks", {}):
        raise CommandValidationError(f"track does not exist: {track_id}")
    duration = _positive(payload.get("duration"), "duration")
    return _clip(
        str(clip_id),
        str(asset_id),
        str(track_id),
        start=_non_negative(payload.get("start"), "start"),
        duration=duration,
        kind=str(payload.get("kind") or project["assets"][asset_id]["type"]),
        source_start=_non_negative(payload.get("sourceStart", 0), "sourceStart"),
        metadata=payload.get("metadata") or {},
    )


def _require_clip(project: dict[str, Any], clip_id: str | None) -> dict[str, Any]:
    if not clip_id:
        raise CommandValidationError("clipId is required")
    try:
        return project["clips"][clip_id]
    except KeyError as exc:
        raise CommandValidationError(f"clip does not exist: {clip_id}") from exc


def _mutate_clip(op: str, clip: dict[str, Any], payload: dict[str, Any]) -> None:
    if op == "clip.move":
        clip["start"] = _non_negative(payload.get("start"), "start")
    elif op == "clip.trim":
        clip["start"] = _non_negative(payload.get("start", clip["start"]), "start")
        clip["duration"] = _positive(payload.get("duration", clip["duration"]), "duration")
        if "sourceStart" in payload:
            clip["sourceStart"] = _non_negative(payload.get("sourceStart"), "sourceStart")
    elif op == "clip.update":
        patch = payload.get("patch") or {}
        for key in ("start", "duration", "sourceStart", "trackId", "enabled", "muted", "volume"):
            if key in patch:
                if key in {"start", "sourceStart"}:
                    clip[key] = _non_negative(patch[key], key)
                elif key == "duration":
                    clip[key] = _positive(patch[key], key)
                else:
                    clip[key] = patch[key]
    elif op == "clip.hide":
        clip["enabled"] = False
    elif op == "clip.show":
        clip["enabled"] = True
    elif op == "clip.mute":
        clip["muted"] = True
    elif op == "clip.unmute":
        clip["muted"] = False
    elif op == "clip.transform":
        clip.setdefault("transform", {}).update(payload.get("transform") or {})
    elif op == "clip.opacity":
        clip.setdefault("transform", {})["opacity"] = _bounded(
            payload.get("opacity"), "opacity", 0.0, 1.0
        )
    elif op == "clip.volume":
        clip["volume"] = _non_negative(payload.get("volume"), "volume")


def _non_negative(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise CommandValidationError(f"{name} must be numeric") from exc
    if out < 0:
        raise CommandValidationError(f"{name} must be non-negative")
    return out


def _positive(value: Any, name: str) -> float:
    out = _non_negative(value, name)
    if out <= 0:
        raise CommandValidationError(f"{name} must be positive")
    return out


def _bounded(value: Any, name: str, low: float, high: float) -> float:
    out = _non_negative(value, name)
    if out < low or out > high:
        raise CommandValidationError(f"{name} must be between {low} and {high}")
    return out
