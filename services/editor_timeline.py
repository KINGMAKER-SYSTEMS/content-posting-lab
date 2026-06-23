"""Non-destructive Editor Bay timeline command core.

The UI, agents, and render workers should all mutate project state through this
module. The render backend is deliberately out of scope for this slice.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from services.json_store import atomic_load, atomic_save


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
            clip = _clip(
                clip_id,
                asset_id,
                _track_for_asset(kind, shot.get("type")),
                start=start,
                duration=clip_duration,
                kind=shot.get("type") or kind,
                source_start=float(shot.get("clipStartSec") or 0),
                metadata={"segmentId": segment_id, "shot": shot},
            )
            # Promote shot-level effects/kenBurns onto the active clip fields the
            # compiler reads (clip.effects / clip.keyframes). The raw shot stays in
            # metadata.shot for lossless re-import; this just wires it through so
            # factory crossfades and Ken-Burns reach OpenShot without an extra edit.
            clip["effects"] = _shot_effects(shot)
            clip["keyframes"] = _merge_keyframes(
                _ken_burns_keyframes(shot.get("kenBurns"), clip_duration),
                _shot_keyframes(shot),
            )
            project["clips"][clip_id] = clip

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
            vo_clip = _clip(
                clip_id,
                asset_id,
                "audio_1",
                start=cursor,
                duration=float(vo.get("duration") or duration),
                kind="voiceover",
                metadata={"segmentId": segment_id},
            )
            # A VO track can ship its own ducking envelope (keyframes /
            # keyframesEnvelope / duckingKeyframes), same shape the music bed and
            # video/graphics shots accept. Without promoting it the envelope is
            # silently dropped at import (the flat volume=1.0 wins) and the render
            # loses the ducking. _shot_keyframes reads exactly those fields; the VO
            # clip keeps the default flat volume=1.0 so the envelope's absolute
            # gains pass through _volume_filter unflattened.
            vo_keyframes = _shot_keyframes(vo)
            if vo_keyframes:
                vo_clip["keyframes"] = vo_keyframes
            project["clips"][clip_id] = vo_clip

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
        # A bed may be a bare path string (today) or {src, keyframes} (a factory that
        # attaches its own ducking envelope). Normalize to a src string for the asset.
        bed_src = music_bed.get("src") if isinstance(music_bed, dict) else music_bed
        asset_id = _stable_id("asset", "music", Path(str(bed_src)).name)
        clip_id = _stable_id("clip", "music", Path(str(bed_src)).name)
        project["assets"][asset_id] = {
            "id": asset_id,
            "type": "audio",
            "src": bed_src,
            "metadata": {"role": "music_bed"},
        }
        bed_clip = _clip(
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
        # If the factory hands us a real keyframed ducking envelope (musicBedKeyframes,
        # or `keyframes` on a dict-shaped bed) instead of the flat 0.22 gain, promote
        # it onto the clip so _volume_filter / openshot_bridge animate it. Without this
        # the envelope is silently dropped (the flat volume wins).
        bed_keyframes = _validated_keyframes_lenient(
            abn_timeline.get("musicBedKeyframes")
            or (music_bed.get("keyframes") if isinstance(music_bed, dict) else None)
        )
        if bed_keyframes:
            bed_clip["keyframes"] = bed_keyframes
            # The envelope's points are ABSOLUTE gains (e.g. 0.6 under intros, 0.22
            # under VO) — they already encode the ducking. But editor_render._volume_filter
            # SCALES a volume envelope by the clip's flat `volume`, and openshot_bridge's
            # static volume seeds the same key, so leaving the flat 0.22 here would
            # double-attenuate the bed (0.6 -> 0.132). Neutralize the flat gain to 1.0
            # when a real envelope drives the ducking so its absolute values pass through.
            if any(t["property"] == "volume" for t in bed_keyframes):
                bed_clip["volume"] = 1.0
        project["clips"][clip_id] = bed_clip
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


def _shot_effects(shot: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote an ABN shot's `effects` (and inter-clip `transitionSec`) to clip effects.

    The shot effect shape already matches the editor effect contract, so each is
    run through `_validated_effect`. Any effect outside the supported vocabulary
    (EFFECT_TYPES) is skipped rather than aborting the import — the raw shot is
    still preserved under clip.metadata.shot, so nothing is lost.

    A shot's `transitionSec` is the factory's choreographed crossfade into this clip
    from the previous one (abn_factory._build_timeline tags shot boundaries with it).
    It's synthesized into a start-anchored `crossfade` effect here so OpenShot dissolves
    the shot boundary instead of hard-cutting. An explicit `effects` crossfade wins —
    if one is already present we don't add a duplicate.
    """

    out: list[dict[str, Any]] = []
    for effect in shot.get("effects") or []:
        try:
            out.append(_validated_effect(effect))
        except CommandValidationError:
            continue
    transition = shot.get("transitionSec")
    if transition not in (None, "") and not any(e["type"] == "crossfade" for e in out):
        try:
            transition_sec = float(transition)
        except (TypeError, ValueError):
            transition_sec = 0.0
        # transitionSec <= 0 is not a dissolve: 0 is a no-op (hard cut) and a
        # zero-duration crossfade effect is pure noise downstream; negative is
        # invalid. Only a positive transition becomes a synthesized crossfade.
        if transition_sec > 0:
            try:
                out.append(
                    _validated_effect(
                        {"id": "xf_shot", "type": "crossfade", "params": {"duration": transition_sec}}
                    )
                )
            except (CommandValidationError, TypeError, ValueError):
                pass
    return out


def _ken_burns_keyframes(ken_burns: Any, duration: float) -> list[dict[str, Any]]:
    """Translate an ABN `kenBurns` envelope to editor scale/x/y keyframe tracks.

    kenBurns = {startScale,endScale,startX,startY,endX,endY,easing}. Only axes that
    actually move (start != end) become tracks, each a 2-point linear envelope from
    clip start (t=0) to clip end (t=duration). openshot_bridge maps scale/x/y to the
    OpenShot scale_x/scale_y/location_x/location_y Point lists.
    """

    if not isinstance(ken_burns, dict):
        return []
    span = max(0.0, float(duration))
    tracks: list[dict[str, Any]] = []
    for prop, start_key, end_key in (
        ("scale", "startScale", "endScale"),
        ("x", "startX", "endX"),
        ("y", "startY", "endY"),
    ):
        if start_key not in ken_burns and end_key not in ken_burns:
            continue
        start_val = ken_burns.get(start_key, ken_burns.get(end_key))
        end_val = ken_burns.get(end_key, ken_burns.get(start_key))
        try:
            start_f = float(start_val)
            end_f = float(end_val)
        except (TypeError, ValueError):
            continue
        if start_f == end_f:
            continue
        tracks.append(
            {
                "property": prop,
                "points": [
                    {"t": 0.0, "value": start_f, "interp": "linear"},
                    {"t": span, "value": end_f, "interp": "linear"},
                ],
            }
        )
    return tracks


def _shot_keyframes(shot: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote an ABN shot's keyframe envelopes (e.g. a volume ducking track the
    factory baked) onto the clip.

    Two sources are accepted, mirroring the music bed (which reads both an explicit
    `keyframes` and a sibling `musicBedKeyframes`):
      * `keyframes` — hand-authored on the shot itself.
      * `keyframesEnvelope` / `duckingKeyframes` — a factory-attached envelope passed
        as a SEPARATE field (e.g. a ducking track abn_factory adds dynamically).
    Without the second source a factory ducking envelope is silently dropped at import
    and the render loses the ducking it intended (editor_render volume filter has
    nothing to animate). Both are merged with the same precedence used elsewhere — an
    explicit per-property `keyframes` track wins over the envelope.

    `_ken_burns_keyframes` only synthesizes scale/x/y tracks from a `kenBurns` block,
    so a shot that ships a `keyframes`/envelope (volume/opacity/...) was previously
    dropped. Each is run through the same lenient validator as the bed so a malformed
    envelope is skipped — not fatal — since the raw shot is still preserved under
    clip.metadata.shot for re-import.
    """

    explicit = _validated_keyframes_lenient(shot.get("keyframes"))
    envelope = _validated_keyframes_lenient(
        shot.get("keyframesEnvelope") or shot.get("duckingKeyframes")
    )
    return _merge_keyframes(explicit, envelope)


def _merge_keyframes(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate keyframe-track groups, keeping the first track seen per property.

    kenBurns-derived tracks take precedence over an explicit shot envelope on the
    same property (kenBurns is the active motion contract); other explicit-only
    properties (volume/opacity) pass through untouched.
    """

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for track in group or []:
            prop = str(track.get("property") or "")
            if prop in seen:
                continue
            seen.add(prop)
            out.append(track)
    return out


def _validated_keyframes_lenient(value: Any) -> list[dict[str, Any]]:
    """`_validated_keyframes` that drops a bad envelope instead of raising.

    Import is best-effort: a malformed factory envelope must not abort the whole
    ABN import (the raw shot/bed stays in metadata for re-import), so swallow the
    CommandValidationError and treat it as "no envelope"."""

    try:
        return _validated_keyframes(value)
    except CommandValidationError:
        return []


class TimelineStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # apply_command / revert_last_command are read-modify-write on a project
        # file. The revision check is optimistic concurrency, but two threads can
        # both read revision N between load() and save() and the second clobbers
        # the first (lost update). Serialize the RMW per project so the second
        # caller observes the first's write and either bumps the revision or
        # raises RevisionConflict. RLock because revert_last_command holds the
        # lock and then calls apply_command, which re-acquires it.
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()

    def _lock_for(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks[project_id]

    def path_for(self, project_id: str) -> Path:
        return self.root / f"{project_id}.json"

    def load(self, project_id: str) -> dict[str, Any]:
        path = self.path_for(project_id)
        if not path.exists():
            raise FileNotFoundError(project_id)
        # Robustified like json_store.atomic_load: a truncated/corrupt project
        # file (e.g. a crash mid-write before save() was atomized) must not raise
        # JSONDecodeError and 500 the editor. We use atomic_load with a sentinel
        # default so corrupt-but-present is reported as the same FileNotFoundError
        # the callers already handle (-> 404) instead of an unhandled crash.
        project = atomic_load(path, default=None)
        if not isinstance(project, dict):
            raise FileNotFoundError(project_id)
        return project

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        project = copy.deepcopy(project)
        path = self.path_for(project["projectId"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # Persist through the proven atomic_save (tmp + flush + fsync + os.replace)
        # so a CPU crash mid-write can't truncate/lose a project's edits.
        atomic_save(path, project)
        return project

    def apply_command(self, project_id: str, command: dict[str, Any]) -> dict[str, Any]:
        with self._lock_for(project_id):
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
        with self._lock_for(project_id):
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
        # Keyframe `t` is clip-local seconds, so the split-off clip's envelope must
        # be rebased to its new t=0 (the split point); the first clip keeps only the
        # points before the split. Without this the second clip fires the parent's
        # keyframes at the wrong times. (CON ticket: split must preserve animation.)
        clip["keyframes"] = _keyframes_before(clip.get("keyframes"), split_offset)
        second["keyframes"] = _keyframes_after(second.get("keyframes"), split_offset)
        # Boundary-anchored effects: the head keeps start-anchored fades whole and
        # drops fadeOut; the tail keeps every effect but re-fits its start-anchored
        # fades for the front the split trimmed (front_trim=split_offset), matching
        # editor_render._refit_effects so the timeline and the compiled video agree.
        clip["effects"] = _effects_for_split_half(clip.get("effects"), "head")
        second["effects"] = _effects_for_split_half(
            second.get("effects"),
            "tail",
            split_offset=split_offset,
            half_duration=second["duration"],
        )
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
    elif op in {"clip.move", "clip.trim", "clip.update", "clip.hide", "clip.show", "clip.mute", "clip.unmute", "clip.transform", "clip.opacity", "clip.volume", "clip.keyframes", "clip.effect.add", "clip.effect.update", "clip.effect.delete"}:
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
    if op == "clip.keyframes" and before.get("clip"):
        return {
            "op": "clip.keyframes",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "clipId": before["clip"]["id"],
                "keyframes": copy.deepcopy(before["clip"].get("keyframes") or []),
            },
        }
    if op in {
        "clip.effect.add",
        "clip.effect.update",
        "clip.effect.delete",
    } and before.get("clip"):
        return {
            "op": "clip.update",
            "actor": actor,
            "expectedRevision": expected_revision,
            "revertsCommandId": entry.get("id"),
            "payload": {
                "clipId": before["clip"]["id"],
                "patch": {"effects": copy.deepcopy(before["clip"].get("effects") or [])},
            },
        }
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
                    "effects",
                    "keyframes",
                )
                if key in payload
            }
        if not isinstance(patch, dict):
            raise CommandValidationError("patch must be an object")
        for key in ("start", "duration", "sourceStart", "trackId", "enabled", "muted", "volume", "transform", "effects", "keyframes"):
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
                elif key == "effects":
                    raw = patch.get("effects")
                    if not isinstance(raw, list):
                        raise CommandValidationError("effects must be a list")
                    clip["effects"] = [_validated_effect(e) for e in raw]
                elif key == "keyframes":
                    clip["keyframes"] = _validated_keyframes(patch.get("keyframes"))
                    # Mirror clip.keyframes (~line 1134): a volume envelope's points are
                    # absolute gains, so neutralize any flat base gain to 1.0 to avoid
                    # double-attenuation against the import-time duck.
                    if any(t["property"] == "volume" for t in clip["keyframes"]):
                        clip["volume"] = 1.0
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
    elif op == "clip.keyframes":
        clip["keyframes"] = _validated_keyframes(payload.get("keyframes"))
        # Mirror the import-time neutralization (see project_from_abn_timeline ~line
        # 230): a music bed imports with a flat volume=0.22 duck. If a user later adds
        # a volume envelope via this command, the clip would carry BOTH the flat 0.22
        # AND an absolute envelope — exactly the double-attenuation state the importer
        # guards against. The envelope's points are absolute gains, so neutralize the
        # flat gain to 1.0 so render backends (openshot_bridge overwrites the volume
        # key; editor_render._volume_filter ignores base_volume) and the timeline state
        # all agree the envelope drives ducking.
        if any(t["property"] == "volume" for t in clip["keyframes"]):
            clip["volume"] = 1.0
    elif op == "clip.effect.add":
        effect = _validated_effect(payload.get("effect") or payload)
        effects = clip.setdefault("effects", [])
        if any(str(e.get("id")) == effect["id"] for e in effects):
            raise CommandValidationError(f"effect already exists: {effect['id']}")
        effects.append(effect)
    elif op == "clip.effect.update":
        effect = _validated_effect(payload.get("effect") or payload)
        effects = clip.setdefault("effects", [])
        for index, existing in enumerate(effects):
            if str(existing.get("id")) == effect["id"]:
                effects[index] = effect
                break
        else:
            raise CommandValidationError(f"effect does not exist: {effect['id']}")
    elif op == "clip.effect.delete":
        effect_id = str(payload.get("effectId") or "")
        effects = clip.setdefault("effects", [])
        kept = [e for e in effects if str(e.get("id")) != effect_id]
        if len(kept) == len(effects):
            raise CommandValidationError(f"effect does not exist: {effect_id}")
        clip["effects"] = kept


# clip.split helpers. Keyframe `t` is clip-local seconds (see _validated_keyframes),
# so splitting a clip at `offset` seconds means the first (head) clip keeps points
# with t <= offset and the second (tail) clip keeps points at t >= offset rebased to
# t -= offset. Boundary value is preserved on both sides so the envelope is continuous.

# fadeOut is end-anchored, so it cannot live on the head half (whose end is the split
# point, not the original clip end) — it is dropped there. fadeIn/crossfade are start-
# anchored (both map to OpenShot's `in` Fade, see openshot_bridge._FADE_DIRECTION_MAP):
# the head keeps them whole; the tail keeps them re-fit for its trimmed front. Whole-
# clip effects (brightness/saturation) copy to both halves unchanged.
_TAIL_ANCHORED_EFFECTS = frozenset({"fadeOut"})


def _value_at(points: list[dict[str, Any]], t: float) -> tuple[float, str] | None:
    """Linearly interpolate (value, interp) of a sorted point list at time ``t``.

    Returns ``None`` when ``t`` falls outside the envelope (no point to anchor on).
    The interp returned is the END point's of the crossing segment (OpenShot
    convention: a segment's interpolation belongs to its later point)."""
    ordered = sorted(points, key=lambda p: float(p.get("t") or 0.0))
    if not ordered:
        return None
    if t <= float(ordered[0]["t"]):
        return float(ordered[0].get("value") or 0.0), str(ordered[0].get("interp") or "linear")
    if t >= float(ordered[-1]["t"]):
        return float(ordered[-1].get("value") or 0.0), str(ordered[-1].get("interp") or "linear")
    for p0, p1 in zip(ordered, ordered[1:]):
        t0, t1 = float(p0["t"]), float(p1["t"])
        if t0 <= t <= t1:
            v0 = float(p0.get("value") or 0.0)
            v1 = float(p1.get("value") or 0.0)
            interp = str(p1.get("interp") or "linear")
            span = t1 - t0
            if span <= 0 or interp == "constant":
                return v0, interp
            return v0 + (v1 - v0) * ((t - t0) / span), interp
    return None


def _keyframes_before(tracks: Any, offset: float) -> list[dict[str, Any]]:
    """Trim a keyframe envelope to the head half of a split (t <= offset).

    If the envelope crosses the split without a point exactly at ``offset``, a synthetic
    end point is appended at ``offset`` carrying the interpolated boundary value, so the
    head's last value matches what the un-split clip held there (continuity at the cut)."""
    out: list[dict[str, Any]] = []
    for track in tracks or []:
        src = track.get("points") or []
        points = [p for p in src if p["t"] <= offset]
        if points and not any(abs(float(p["t"]) - offset) < 1e-9 for p in points):
            boundary = _value_at(src, offset)
            if boundary is not None:
                value, interp = boundary
                points = [*points, {"t": offset, "value": value, "interp": interp}]
        if points:
            out.append({**track, "points": points})
    return out


def _keyframes_after(tracks: Any, offset: float) -> list[dict[str, Any]]:
    """Rebase a keyframe envelope to the tail half of a split (t >= offset → t -= offset).

    If the envelope crosses the split without a point exactly at ``offset``, a synthetic
    start point is prepended at the rebased t=0 carrying the interpolated boundary value,
    so the tail begins at the value the un-split clip held at the cut (continuity)."""
    out: list[dict[str, Any]] = []
    for track in tracks or []:
        src = track.get("points") or []
        points = [
            {**p, "t": p["t"] - offset}
            for p in src
            if p["t"] >= offset
        ]
        if points and not any(abs(float(p["t"])) < 1e-9 for p in points):
            boundary = _value_at(src, offset)
            if boundary is not None:
                value, interp = boundary
                points = [{"t": 0.0, "value": value, "interp": interp}, *points]
        if points:
            out.append({**track, "points": points})
    return out


def _effects_for_split_half(
    effects: Any,
    half: str,
    *,
    split_offset: float = 0.0,
    half_duration: float = 0.0,
) -> list[dict[str, Any]]:
    """Route boundary-anchored effects to the split half that owns their boundary.

    The HEAD owns the original start: it keeps start-anchored fades (fadeIn/crossfade)
    at full duration and drops the end-anchored fadeOut. The TAIL owns the original
    end: it keeps fadeOut, but its source front is trimmed by ``split_offset``, which
    eats into any start-anchored fade. So the tail's start-anchored fades must be
    RE-FIT exactly like the render path does (``editor_render._refit_effects`` with
    ``front_trim=split_offset`` and ``windowed_duration=half_duration``) — otherwise
    the timeline state and the compiled OpenShot fade disagree. Whole-clip effects
    (brightness/saturation) copy to both halves unchanged.
    """
    if half == "head":
        kept = [
            e for e in (effects or []) if e.get("type") not in _TAIL_ANCHORED_EFFECTS
        ]
        return [copy.deepcopy(e) for e in kept]
    # Tail: keep everything (fadeOut included), then re-fit the start-anchored fades
    # that the trimmed front shortened. Reuse the render path's _refit_effects so the
    # two code paths can never drift.
    from services.editor_render import _refit_effects

    return _refit_effects(
        [copy.deepcopy(e) for e in (effects or [])],
        front_trim=split_offset,
        windowed_duration=half_duration,
    )


# Editor-property names a clip-level keyframe track may animate. These map 1:1 to
# OpenShot clip-JSON keys in openshot_bridge.clip_json; anything else is rejected
# here so a bad envelope never reaches the compiler.
KEYFRAME_PROPERTIES = frozenset(
    {"volume", "opacity", "scale", "x", "y", "rotation"}
)
KEYFRAME_INTERPOLATIONS = frozenset({"linear", "constant", "bezier"})


def _validated_keyframes(value: Any) -> list[dict[str, Any]]:
    """Validate a clip's keyframe envelope list.

    Shape: ``[{"property": "volume", "points": [{"t": 0.0, "value": 1.0,
    "interp": "linear"}, ...]}, ...]``. ``t`` is seconds relative to the clip
    start; ``value`` is in editor units (0..1 for volume/opacity). Translation to
    OpenShot multi-Point keyframe JSON happens in openshot_bridge.
    """

    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise CommandValidationError("keyframes must be a list")
    out: list[dict[str, Any]] = []
    for track in value:
        if not isinstance(track, dict):
            raise CommandValidationError("keyframe track must be an object")
        prop = str(track.get("property") or "")
        if prop not in KEYFRAME_PROPERTIES:
            raise CommandValidationError(f"unsupported keyframe property: {prop}")
        raw_points = track.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            raise CommandValidationError(f"keyframe track {prop} requires points")
        points: list[dict[str, Any]] = []
        for point in raw_points:
            if not isinstance(point, dict):
                raise CommandValidationError("keyframe point must be an object")
            interp = str(point.get("interp") or "linear")
            if interp not in KEYFRAME_INTERPOLATIONS:
                raise CommandValidationError(f"unsupported keyframe interp: {interp}")
            points.append(
                {
                    "t": _non_negative(point.get("t"), "keyframe.t"),
                    "value": _finite_number(point.get("value"), "keyframe.value"),
                    "interp": interp,
                }
            )
        points.sort(key=lambda p: p["t"])
        out.append({"property": prop, "points": points})
    return out


# Closed clip-effect vocabulary. Each entry maps an editor effect `type` to the
# numeric params it accepts (param name -> (low, high) inclusive bounds). These
# translate 1:1 to OpenShot effect JSON in openshot_bridge.effect_json; any type
# or param outside this table is rejected here so a bad effect never reaches the
# compiler. Fades/crossfades cover ducking-tail and clip-boundary compositing
# that abn_factory depends on (see music-bed ducking comment above).
EFFECT_TYPES: dict[str, dict[str, tuple[float, float]]] = {
    "fadeIn": {"duration": (0.0, 600.0)},
    "fadeOut": {"duration": (0.0, 600.0)},
    "crossfade": {"duration": (0.0, 600.0)},
    "brightness": {"value": (-1.0, 1.0)},
    "saturation": {"value": (0.0, 4.0)},
}


def _validated_effect(value: Any) -> dict[str, Any]:
    """Validate a single clip effect.

    Shape: ``{"id": "fx_...", "type": "fadeIn", "params": {"duration": 0.5}}``.
    ``type`` must be in EFFECT_TYPES; every declared param must be present, numeric
    and within bounds. Unknown params are rejected. Translation to OpenShot effect
    JSON happens in openshot_bridge.
    """

    if not isinstance(value, dict):
        raise CommandValidationError("effect must be an object")
    effect_id = value.get("id") or value.get("effectId")
    if not effect_id:
        raise CommandValidationError("effect id is required")
    effect_type = str(value.get("type") or "")
    spec = EFFECT_TYPES.get(effect_type)
    if spec is None:
        raise CommandValidationError(f"unsupported effect type: {effect_type}")
    raw_params = value.get("params") or {}
    if not isinstance(raw_params, dict):
        raise CommandValidationError("effect params must be an object")
    unknown = set(raw_params) - set(spec)
    if unknown:
        raise CommandValidationError(f"unsupported effect params: {sorted(unknown)}")
    params: dict[str, float] = {}
    for name, (low, high) in spec.items():
        if name not in raw_params:
            raise CommandValidationError(f"effect param required: {name}")
        params[name] = _bounded_signed(raw_params[name], f"effect.{name}", low, high)
    return {"id": str(effect_id), "type": effect_type, "params": params}


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


def _bounded_signed(value: Any, name: str, low: float, high: float) -> float:
    """Like _bounded but allows negative values (effect params such as brightness)."""
    out = _finite_number(value, name)
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
