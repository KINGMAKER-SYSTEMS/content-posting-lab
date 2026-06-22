# `editor-timeline/v1` — Canonical Timeline & Edit-Command Schema

This is the shared contract for the agent-native Editor Bay. The UI, agents, and
render workers all read and mutate project state through one document shape and
one command vocabulary, so an edit issued in any lane behaves the same everywhere.

**Source of truth:** [`services/editor_timeline.py`](services/editor_timeline.py)
(`SCHEMA = "editor-timeline/v1"`). The OpenShot/ffmpeg render translation lives in
`openshot_bridge`; this document describes the editor-facing schema, not the
compiler.

**Why this doc exists:** three lanes were independently building the same atomic
edit system — the ABN Editor Bay (this repo), the history lane (`timeline.json`
EDL), and the Aviation/Premiere lane. `editor-timeline/v1` is a superset of all
three, so this is the convergence target. Map your lane in once (see
[Cross-lane interop](#cross-lane-interop)); every edit after that is the same
command contract.

---

## 1. Document shape

A project is one JSON document:

```jsonc
{
  "schema": "editor-timeline/v1",
  "projectId": "string",
  "sourceEpisodeId": "string | null",
  "title": "string",
  "fps": 30,
  "width": 1920,
  "height": 1080,
  "revision": 0,            // bumped once per applied command (optimistic concurrency)
  "assets":   { "<assetId>":  Asset },
  "tracks":   { "<trackId>":  Track },
  "clips":    { "<clipId>":   Clip },
  "markers":  { "<markerId>": Marker },
  "notes":    { "<noteId>":   Note },
  "effects":  {},           // reserved (project-level); clip effects live on the clip
  "keyframes": {},          // reserved (project-level); clip keyframes live on the clip
  "renderCache": {},        // content-addressed; edit one clip → only it re-renders
  "commandLog": [ CommandLogEntry ],
  "createdAt": 0.0,
  "updatedAt": 0.0,
  "metadata": { }           // present on imported projects
}
```

Collections are **keyed maps**, not arrays — every entity has a stable id.

### Default tracks

A new project starts with five tracks (lower `index` renders under higher):

| id           | kind       | name    | index |
|--------------|------------|---------|-------|
| `video_1`    | `video`    | Video 1 | 10    |
| `graphics_1` | `graphics` | Graphics 1 | 20 |
| `titles_1`   | `title`    | Titles 1 | 30    |
| `audio_1`    | `audio`    | Voice   | 40    |
| `music_1`    | `audio`    | Music   | 50    |

`Track = { id, kind, name, index, locked }`.

---

## 2. Entities

### Asset

```jsonc
{
  "id": "asset_...",
  "type": "still | video | audio | title",
  "src": "path/or/url",
  "source": "string?",          // provenance (e.g. higgsfield, nano-banana), optional
  "metadata": { "segmentId": "...", "shotType": "..." }
}
```

### Clip

A placement of an asset on a track. This is the unit every `clip.*` op targets.

```jsonc
{
  "id": "clip_...",
  "assetId": "asset_...",
  "trackId": "video_1",
  "kind": "video | still | audio | title",
  "start": 0.0,                 // timeline seconds
  "duration": 4.0,              // timeline seconds
  "sourceStart": 0.0,           // in-point into the asset
  "enabled": true,              // clip.hide / clip.show
  "muted": false,               // clip.mute / clip.unmute
  "volume": 1.0,
  "transform": { "x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0 },
  "effects":   [ Effect ],
  "keyframes": [ KeyframeEnvelope ],
  "metadata": { }
}
```

**`transform`** is the static zoom/pan: `x`/`y` are normalized center coordinates
(`0.5, 0.5` = centered), `scale > 0` (zoom), `opacity` in `0..1`. For *animated*
zoom/pan (Ken Burns), use `keyframes` instead (§ Keyframes).

### Keyframe envelope

Animates a single clip property over clip-local time.

```jsonc
{
  "property": "scale",          // volume | opacity | scale | x | y | rotation
  "points": [
    { "t": 0.0, "value": 1.0, "interp": "linear" },   // t = seconds from clip start
    { "t": 4.0, "value": 1.1, "interp": "linear" }    // interp: linear | constant | bezier
  ]
}
```

Points are sorted by `t`. A `scale`/`x`/`y` envelope **is** a Ken Burns move.

### Effect

Closed vocabulary — unknown type or param is rejected before it reaches the compiler.

```jsonc
{ "id": "fx_...", "type": "fadeIn", "params": { "duration": 0.5 } }
```

| type        | params (inclusive bounds)      |
|-------------|--------------------------------|
| `fadeIn`    | `duration` `0..600`            |
| `fadeOut`   | `duration` `0..600`            |
| `crossfade` | `duration` `0..600`            |
| `brightness`| `value` `-1..1`                |
| `saturation`| `value` `0..4`                 |

### Marker

```jsonc
{ "id": "marker_...", "time": 12.5, "label": "string", "metadata": {} }
```

### Note — the frame-note annotation

A review annotation pinned to a target, optionally carrying its **own proposed fix**.

```jsonc
{
  "id": "note_...",
  "target": { },                 // frame / timecode / clip reference
  "text": "clip is over-zoomed here",
  "suggestedCommand": { "op": "clip.transform", "payload": { "...": "..." } },
  "metadata": { }
}
```

`suggestedCommand` is the key idea: a frame-note isn't just a timecode + comment,
it can carry the exact command that resolves it — so "draw on the frame → fix" is
one round trip.

---

## 3. Edit-command contract

Every mutation is a command applied through `apply_command` /
`TimelineStore.apply_command`. State is never edited in place by callers.

### Command envelope

```jsonc
{
  "id": "cmd_...",               // optional; generated if omitted, must be unique
  "op": "clip.transform",
  "payload": { },
  "expectedRevision": 7,         // REQUIRED — must equal current project.revision
  "actor": "glitch-claude",      // optional, recorded in the log
  "revertsCommandId": "cmd_..."  // optional; set by revert
}
```

`expectedRevision` gives **optimistic concurrency**: if it doesn't match the
current revision the command is rejected with `RevisionConflict` (no lost-update
races between agents and the UI). On success, `revision += 1` and an entry is
appended to `commandLog`:

```jsonc
{ "id", "op", "actor", "payload", "before"?, "after"?, "timestamp", "revertsCommandId"? }
```

### Operations

| op | payload | notes |
|----|---------|-------|
| `asset.import` | `Asset` | register an asset |
| `track.create` | `Track` | |
| `clip.create` | clip fields | place an asset on a track |
| `clip.split` | `{ clipId, at, newClipId? }` | `at` is timeline seconds, inside the clip; keyframes/effects split correctly across both halves |
| `clip.unsplit` | `{ clip, createdClipId }` | inverse of split |
| `clip.move` | `{ clipId, start }` | |
| `clip.trim` | `{ clipId, start?, duration?, sourceStart? }` | |
| `clip.update` | `{ clipId, patch:{ start,duration,sourceStart,trackId,enabled,muted,volume,transform,effects } }` | general patch |
| `clip.hide` / `clip.show` | `{ clipId }` | toggles `enabled` |
| `clip.mute` / `clip.unmute` | `{ clipId }` | |
| **`clip.transform`** | `{ clipId, transform:{ x?, y?, scale?, opacity? } }` | **static zoom / pan** (merges into existing transform) |
| `clip.opacity` | `{ clipId, opacity }` | `0..1` |
| `clip.volume` | `{ clipId, volume }` | |
| **`clip.keyframes`** | `{ clipId, keyframes:[ KeyframeEnvelope ] }` | **animated zoom/pan — Ken Burns** |
| `clip.effect.add` / `clip.effect.update` | `{ clipId, effect:{ id, type, params } }` | |
| `clip.effect.delete` | `{ clipId, effectId }` | |
| `marker.add` | `{ markerId?, time, label, metadata? }` | |
| `marker.delete` | `{ markerId }` | |
| **`note.add`** | `{ noteId?, target, text, suggestedCommand?, metadata? }` | **frame-note annotation** |
| `note.delete` | `{ noteId }` | |

### Revert

`TimelineStore.revert_last_command(projectId, actor, expectedRevision?)` finds the
last revertable entry, builds its inverse, and applies it **as a normal command**
— so a revert is itself logged and revertible. The full history is replayable.

---

## 4. Cross-lane interop

The schema ships with importers so an existing lane joins without a rewrite:

- **`project_from_abn_timeline(project_id, abn_timeline, source_episode_id=None)`**
  Builds a fresh `v1` project from an ABN / EDL timeline shaped like
  `{ segments[].shots[], lowerThirds, durationSec, fps, width, height, title, episodeId }`.
  Maps `segments → clips`, each shot's Ken Burns into `keyframes`, and segment
  transitions into `effects`.
- **`reimport_abn_timeline_preserving_commands(...)`**
  Re-imports a regenerated base cut while **replaying the existing `commandLog`**,
  so agent/review edits survive a re-render of the underlying timeline.

So a history-lane `timeline.json` EDL or the Aviation/Premiere lane imports once,
then every edit flows through the command contract above. `renderCache` is
content-addressed, so editing one clip only re-renders that segment.

### Schema mapping (prior lane schemas → `editor-timeline/v1`)

| prior concept | `editor-timeline/v1` |
|---------------|----------------------|
| `segments[].shots[]` (EDL) | `clips{}` keyed by id, on `tracks{}` |
| `durationSec`, `start/end` | clip `start` + `duration` (timeline seconds) |
| `zoom:[a,b]`, `pan` (static) | clip `transform { x, y, scale }` |
| `zoom:[a,b]` (animated / Ken Burns) | `keyframes` envelope on `scale`/`x`/`y` |
| `transition_in: cut\|crossfade\|hold` | clip `effects` (`crossfade`, fades) |
| `lowerThirds[]` | `title`-kind clips on `titles_1` |
| `edit-queue: { action, ... }` | command `op` + `payload` + `commandLog` |
| `apply` / incremental rebuild | `apply_command` + content-addressed `renderCache` |
| `frame-notes: { t, kind, text, frameImg }` | `note.add { target, text, suggestedCommand }` |
| `vo_span:[a,b]` | clip `metadata` (carry-through) |

---

## 5. Storage

`TimelineStore(directory)` persists one JSON document per `projectId`
(`load` / `save`, atomic writes via `services.json_store.atomic_save`).
`SCHEMA = "editor-timeline/v1"`, `ABN_IMPORT_VERSION = 2`.
