"""Non-destructive Editor Bay timeline command core.

The UI, agents, and render workers should all mutate project state through this
module. The render backend is deliberately out of scope for this slice.
"""

from __future__ import annotations

import copy
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA = "editor-timeline/v1"
ABN_IMPORT_VERSION = 2


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
    project["metadata"] = {
        "abnImportVersion": ABN_IMPORT_VERSION,
        "source": "abn_timeline",
    }
    cursor = 0.0
    for seg_index, segment in enumerate(abn_timeline.get("segments") or []):
        segment_id = segment.get("segmentId") or f"s{seg_index}"
        duration = float(segment.get("durationSec") or 0)
        shots = segment.get("shots") or []
        for shot_index, shot in enumerate(shots):
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
                **({"source": src_source} if (src_source := _shot_source(shot.get("type"))) else {}),
                "metadata": {"segmentId": segment_id, "shotType": shot.get("type")},
            }
            start = cursor + float(shot.get("startSec") or 0)
            clip_duration = _shot_duration(segment, shots, shot_index, duration)
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

    music_bed = abn_timeline.get("musicBed")
    if music_bed:
        asset_id = _stable_id("asset", "music", Path(str(music_bed)).name)
        clip_id = _stable_id("clip", "music", Path(str(music_bed)).name)
        project["assets"][asset_id] = {
            "id": asset_id,
            "type": "audio",
            "src": music_bed,
            "metadata": {"role": "music_bed"},
        }
        project["clips"][clip_id] = _clip(
            clip_id,
            asset_id,
            "music_1",
            start=0,
            duration=float(abn_timeline.get("totalSec") or cursor or 0.1),
            kind="music_bed",
            # ponytail: bed imports pre-ducked to the factory's 0.22 convention
            # (abn_factory sidechain duck pass) so music sits under VO, not over it.
            # A full keyframed ducking envelope can replace this flat gain later if needed.
            volume=0.22,
            metadata={"role": "music_bed"},
        )
    return project


def reimport_abn_timeline_preserving_commands(
    project: dict[str, Any],
    abn_timeline: dict[str, Any],
    *,
    source_episode_id: str | None = None,
) -> dict[str, Any]:
    """Rebuild imported ABN structure and replay command-log edits on top."""

    migrated = project_from_abn_timeline(
        project["projectId"],
        abn_timeline,
        source_episode_id=source_episode_id or project.get("sourceEpisodeId"),
    )
    warnings: list[dict[str, str]] = []
    for command in project.get("commandLog") or []:
        replay = {
            "id": command.get("id"),
            "op": command.get("op"),
            "actor": command.get("actor") or "migration",
            "expectedRevision": migrated.get("revision", 0),
            "payload": copy.deepcopy(command.get("payload") or {}),
        }
        if command.get("revertsCommandId"):
            replay["revertsCommandId"] = command.get("revertsCommandId")
        try:
            migrated = apply_command(migrated, replay)
        except TimelineError as exc:
            warnings.append({
                "commandId": str(command.get("id") or ""),
                "op": str(command.get("op") or ""),
                "reason": str(exc),
            })

    migrated.setdefault("markers", {}).update(copy.deepcopy(project.get("markers") or {}))
    migrated.setdefault("notes", {}).update(copy.deepcopy(project.get("notes") or {}))
    migrated["metadata"] = {
        **(migrated.get("metadata") or {}),
        "migratedFromRevision": project.get("revision", 0),
        "migrationWarnings": warnings,
    }
    return migrated


def _shot_duration(
    segment: dict[str, Any],
    shots: list[dict[str, Any]],
    shot_index: int,
    segment_duration: float,
) -> float:
    shot = shots[shot_index]
    if shot.get("durationSec") not in {None, ""}:
        return max(0.001, float(shot.get("durationSec") or 0))

    start = float(shot.get("startSec") or 0)
    if shot.get("endSec") not in {None, ""}:
        return max(0.001, float(shot.get("endSec") or 0) - start)

    future_starts = [
        float(next_shot.get("startSec") or 0)
        for next_shot in shots[shot_index + 1 :]
        if next_shot.get("src") and float(next_shot.get("startSec") or 0) > start
    ]
    end = min(future_starts) if future_starts else segment_duration
    return max(0.001, end - start)


def _stable_id(prefix: str, *parts: str) -> str:
    clean = "_".join(
        "".join(ch.lower() if ch.isalnum() else "_" for ch in part).strip("_")
        for part in parts
        if part
    )
    clean = "_".join(part for part in clean.split("_") if part)
    return f"{prefix}_{clean[:80]}" if clean else _uid(prefix)


def _shot_source(shot_type: str | None) -> str | None:
    """Preserve the shot's production source (remotion/webscroll/css/broll/...) for OpenShot.

    A shot's `type` doubles as its source vocabulary at the factory level. When it's a
    known SOURCE_TYPES key, carry it onto the asset as `source` so openshot_bridge can
    map it to the right media kind AND keep the layers distinguishable. Unknown types
    (artifact/card/hook/title) have no source tag and fall back to media `kind`.
    """
    from services.openshot_bridge import SOURCE_TYPES

    key = str(shot_type or "").lower()
    return key if key in SOURCE_TYPES else None


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
    volume: float = 1.0,
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
        "volume": float(volume),
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

    def revert_last_command(
        self,
        project_id: str,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        project = self.load(project_id)
        if expected_revision is not None and int(expected_revision) != int(project.get("revision", 0)):
            raise RevisionConflict(
                f"expected revision {expected_revision}, current revision {project.get('revision', 0)}"
            )
        if not project.get("commandLog"):
            raise CommandValidationError("project has no command to revert")
        last = _last_revertable_command(project.get("commandLog") or [])
        command = _inverse_command(last, actor=actor, expected_revision=int(project["revision"]))
        return self.apply_command(project_id, command)


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
    command_id = command.get("id") or _uid("cmd")
    if any(str(entry.get("id") or "") == str(command_id) for entry in project.get("commandLog") or []):
        raise CommandValidationError(f"command id already exists: {command_id}")
    payload = copy.deepcopy(command.get("payload") or {})
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    command_for_validation = {**command, "payload": payload}
    _validate_reverts_command_id(project, command_for_validation)

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
    elif op == "clip.unsplit":
        original = payload.get("clip")
        created_clip_id = payload.get("createdClipId")
        if not isinstance(original, dict) or not original.get("id"):
            raise CommandValidationError("clip.unsplit requires original clip")
        if not created_clip_id:
            raise CommandValidationError("clip.unsplit requires createdClipId")
        _validate_unsplit_reverts_recorded_split(project, command, original, str(created_clip_id))
        current = _require_clip(project, str(original.get("id")))
        created = _require_clip(project, str(created_clip_id))
        before["clip"] = copy.deepcopy(current)
        before["createdClip"] = copy.deepcopy(created)
        project["clips"][str(original["id"])] = copy.deepcopy(original)
        project["clips"].pop(str(created_clip_id), None)
        after["clip"] = copy.deepcopy(project["clips"][str(original["id"])])
        after["deletedClipId"] = str(created_clip_id)
    elif op in {"clip.move", "clip.trim", "clip.update", "clip.hide", "clip.show", "clip.mute", "clip.unmute", "clip.transform", "clip.opacity", "clip.volume"}:
        clip = _require_clip(project, payload.get("clipId"))
        before["clip"] = copy.deepcopy(clip)
        _mutate_clip(project, op, clip, payload)
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
    elif op == "marker.delete":
        marker_id = str(payload.get("markerId") or "")
        if not marker_id or marker_id not in project.get("markers", {}):
            raise CommandValidationError(f"marker does not exist: {marker_id}")
        marker = project["markers"].pop(marker_id)
        before["marker"] = copy.deepcopy(marker)
        after["deletedMarkerId"] = marker_id
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
    elif op == "note.delete":
        note_id = str(payload.get("noteId") or "")
        if not note_id or note_id not in project.get("notes", {}):
            raise CommandValidationError(f"note does not exist: {note_id}")
        note = project["notes"].pop(note_id)
        before["note"] = copy.deepcopy(note)
        after["deletedNoteId"] = note_id
    else:
        raise CommandValidationError(f"unsupported command op: {op}")

    project["revision"] = int(project.get("revision", 0)) + 1
    entry = {
        "id": command_id,
        "op": op,
        "actor": command.get("actor") or "unknown",
        "expectedRevision": expected,
        "revision": project["revision"],
        "payload": payload,
        "before": before,
        "after": after,
        "ts": _now(),
    }
    if command.get("revertsCommandId"):
        entry["revertsCommandId"] = command["revertsCommandId"]
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
        if entry.get("revertsCommandId"):
            command["revertsCommandId"] = entry.get("revertsCommandId")
        base = apply_command(base, command)
    return base


def _validate_reverts_command_id(project: dict[str, Any], command: dict[str, Any]) -> None:
    reverts_command_id = command.get("revertsCommandId")
    if not reverts_command_id:
        return
    for entry in project.get("commandLog") or []:
        if str(entry.get("revertsCommandId") or "") == str(reverts_command_id):
            raise CommandValidationError("revertsCommandId already reverted")
    target = next(
        (
            entry
            for entry in project.get("commandLog") or []
            if str(entry.get("id") or "") == str(reverts_command_id)
        ),
        None,
    )
    if not target:
        raise CommandValidationError("revertsCommandId does not reference a command")
    if target.get("revertsCommandId"):
        raise CommandValidationError("revertsCommandId cannot reference an inverse command")
    expected = _inverse_command(
        target,
        actor=str(command.get("actor") or "unknown"),
        expected_revision=int(project.get("revision", 0)),
    )
    if command.get("op") != expected.get("op"):
        raise CommandValidationError("revertsCommandId op does not match inverse command")
    if command.get("payload") != expected.get("payload"):
        raise CommandValidationError("revertsCommandId payload does not match inverse command")


def _clip_values_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in (
        "id",
        "assetId",
        "trackId",
        "kind",
        "start",
        "duration",
        "sourceStart",
        "enabled",
        "muted",
        "volume",
        "transform",
        "effects",
        "keyframes",
        "metadata",
    ):
        if left.get(key) != right.get(key):
            return False
    return True


def _validate_unsplit_reverts_recorded_split(
    project: dict[str, Any],
    command: dict[str, Any],
    original: dict[str, Any],
    created_clip_id: str,
) -> None:
    reverts_command_id = command.get("revertsCommandId")
    if not reverts_command_id:
        raise CommandValidationError("clip.unsplit requires revertsCommandId")
    split_entry = next(
        (
            entry
            for entry in project.get("commandLog") or []
            if str(entry.get("id") or "") == str(reverts_command_id)
        ),
        None,
    )
    if not split_entry or split_entry.get("op") != "clip.split":
        raise CommandValidationError("clip.unsplit must revert a recorded clip.split")
    split_before = (split_entry.get("before") or {}).get("clip") or {}
    split_after = split_entry.get("after") or {}
    split_after_clip = split_after.get("clip") or {}
    split_created = split_after.get("createdClip") or {}
    if str(split_before.get("id") or "") != str(original.get("id") or ""):
        raise CommandValidationError("clip.unsplit original clip does not match split command")
    if str(split_created.get("id") or "") != created_clip_id:
        raise CommandValidationError("clip.unsplit created clip does not match split command")
    if not _clip_values_match(original, split_before):
        raise CommandValidationError("clip.unsplit original payload does not match split baseline")

    current = _require_clip(project, str(original.get("id")))
    created = _require_clip(project, created_clip_id)
    if not _clip_values_match(current, split_after_clip):
        raise CommandValidationError("clip.unsplit current original clip no longer matches split output")
    if not _clip_values_match(created, split_created):
        raise CommandValidationError("clip.unsplit current created clip no longer matches split output")


def _last_revertable_command(command_log: list[dict[str, Any]]) -> dict[str, Any]:
    reverted_ids = {
        str(entry.get("revertsCommandId"))
        for entry in command_log
        if entry.get("revertsCommandId")
    }
    for entry in reversed(command_log):
        command_id = str(entry.get("id") or "")
        if entry.get("revertsCommandId") or command_id in reverted_ids:
            continue
        return entry
    raise CommandValidationError("project has no command to revert")


def _inverse_command(entry: dict[str, Any], *, actor: str, expected_revision: int) -> dict[str, Any]:
    op = str(entry.get("op") or "")
    before = entry.get("before") or {}
    after = entry.get("after") or {}
    if op in {
        "clip.move",
        "clip.trim",
        "clip.update",
        "clip.hide",
        "clip.show",
        "clip.mute",
        "clip.unmute",
        "clip.transform",
        "clip.opacity",
        "clip.volume",
    } and before.get("clip"):
        return {
            "op": "clip.update",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": _clip_restore_payload(before["clip"]),
        }
    if op == "clip.split" and before.get("clip") and after.get("createdClip"):
        return {
            "op": "clip.unsplit",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "clipId": before["clip"]["id"],
                "createdClipId": after["createdClip"]["id"],
                "clip": copy.deepcopy(before["clip"]),
            },
        }
    if op == "clip.unsplit" and before.get("createdClip"):
        return {
            "op": "clip.split",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "clipId": before["clip"]["id"],
                "at": before["createdClip"]["start"],
                "newClipId": before["createdClip"]["id"],
            },
        }
    if op == "marker.add" and after.get("marker"):
        return {
            "op": "marker.delete",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {"markerId": after["marker"]["id"]},
        }
    if op == "marker.delete" and before.get("marker"):
        marker = before["marker"]
        return {
            "op": "marker.add",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "markerId": marker["id"],
                "time": marker["time"],
                "label": marker.get("label", ""),
                "metadata": copy.deepcopy(marker.get("metadata") or {}),
            },
        }
    if op == "note.add" and after.get("note"):
        return {
            "op": "note.delete",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {"noteId": after["note"]["id"]},
        }
    if op == "note.delete" and before.get("note"):
        note = before["note"]
        return {
            "op": "note.add",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "noteId": note["id"],
                "target": copy.deepcopy(note.get("target") or {}),
                "text": note.get("text", ""),
                "suggestedCommand": copy.deepcopy(note.get("suggestedCommand")),
                "metadata": copy.deepcopy(note.get("metadata") or {}),
            },
        }
    raise CommandValidationError(f"cannot revert command {op}")


def _clip_restore_payload(clip: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key in ("start", "duration", "sourceStart", "trackId", "enabled", "muted", "volume", "transform"):
        if key in clip:
            patch[key] = copy.deepcopy(clip[key])
    return {"clipId": clip["id"], "patch": patch}


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


def _mutate_clip(project: dict[str, Any], op: str, clip: dict[str, Any], payload: dict[str, Any]) -> None:
    if op == "clip.move":
        clip["start"] = _non_negative(payload.get("start"), "start")
    elif op == "clip.trim":
        clip["start"] = _non_negative(payload.get("start", clip["start"]), "start")
        clip["duration"] = _positive(payload.get("duration", clip["duration"]), "duration")
        if "sourceStart" in payload:
            clip["sourceStart"] = _non_negative(payload.get("sourceStart"), "sourceStart")
    elif op == "clip.update":
        patch = payload.get("patch")
        if patch is None:
            patch = {
                key: payload[key]
                for key in (
                    "start",
                    "duration",
                    "sourceStart",
                    "trackId",
                    "enabled",
                    "muted",
                    "volume",
                    "transform",
                )
                if key in payload
            }
        if not isinstance(patch, dict):
            raise CommandValidationError("patch must be an object")
        for key in ("start", "duration", "sourceStart", "trackId", "enabled", "muted", "volume", "transform"):
            if key in patch:
                if key in {"start", "sourceStart"}:
                    clip[key] = _non_negative(patch[key], key)
                elif key == "duration":
                    clip[key] = _positive(patch[key], key)
                elif key == "volume":
                    clip[key] = _non_negative(patch[key], key)
                elif key == "trackId":
                    if patch[key] not in project.get("tracks", {}):
                        raise CommandValidationError(f"track does not exist: {patch[key]}")
                    clip[key] = patch[key]
                elif key == "transform":
                    transform = patch.get("transform") or {}
                    if not isinstance(transform, dict):
                        raise CommandValidationError("transform must be an object")
                    clip["transform"] = _validated_transform(transform)
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
        transform = payload.get("transform") or {}
        if not isinstance(transform, dict):
            raise CommandValidationError("transform must be an object")
        clip.setdefault("transform", {}).update(_validated_transform(transform))
    elif op == "clip.opacity":
        clip.setdefault("transform", {})["opacity"] = _bounded(
            payload.get("opacity"), "opacity", 0.0, 1.0
        )
    elif op == "clip.volume":
        clip["volume"] = _non_negative(payload.get("volume"), "volume")


def _non_negative(value: Any, name: str) -> float:
    out = _finite_number(value, name)
    if out < 0:
        raise CommandValidationError(f"{name} must be non-negative")
    return out


def _finite_number(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise CommandValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(out):
        raise CommandValidationError(f"{name} must be numeric")
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


def _validated_transform(transform: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in transform.items():
        name = f"transform.{key}"
        if key == "opacity":
            out[key] = _bounded(value, name, 0.0, 1.0)
        elif key == "scale":
            out[key] = _positive(value, name)
        else:
            out[key] = _finite_number(value, name)
    return out
