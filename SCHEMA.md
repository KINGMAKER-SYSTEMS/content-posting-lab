# `editor-timeline/v1` — Canonical Timeline & Edit-Command Schema

This describes the current Editor Bay project document and edit commands. Clients
submit commands with a project revision; the backend validates and logs each edit.

**Source of truth:** [`services/editor_timeline.py`](services/editor_timeline.py)
(`SCHEMA = "editor-timeline/v1"`). HTTP command, import, and render-cache behavior
lives in [`routers/agenticnews.py`](routers/agenticnews.py). Render translation
lives in [`services/openshot_bridge.py`](services/openshot_bridge.py) and
[`services/editor_render.py`](services/editor_render.py); accepting an edit does
not guarantee every render backend can reproduce it.

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
  "renderCache": {},        // optional, revision-bound render results; see § 5
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
  "type": "image",             // importer: image | video | audio | title
  "src": "path/or/url",
  "source": "string?",          // optional production source tag on imported assets
  "metadata": { "segmentId": "...", "shotType": "..." }
}
```

The importer derives media type from the source suffix and shot type; images go
on `graphics_1`. `asset.import` requires `id` (or `assetId`), `type`, and `src`,
and stores `metadata`. Its `type` is a string, not a closed enum; it does not
retain a caller-supplied `source` field. A production source tag such as `still`
is distinct from the imported media type `image`.

### Clip

A placement of an asset on a track. This is the unit every `clip.*` op targets.

```jsonc
{
  "id": "clip_...",
  "assetId": "asset_...",
  "trackId": "video_1",
  "kind": "image",             // string: asset type or imported shot/role name
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

Points are sorted by `t`; times must be nonnegative and values finite. A
`scale`/`x`/`y` envelope describes a Ken Burns move. The ffmpeg fallback reports
unsupported envelopes (currently `scale`) in render warnings; schema acceptance
alone does not establish animated-render parity.

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

`suggestedCommand` is stored with the note; adding a note does not validate or
execute that proposed edit. Submit it separately through the command endpoint.

---

## 3. Edit-command contract

Editor edits use `apply_command` / `TimelineStore.apply_command`. Project import
and render-cache maintenance are separate backend operations.

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
current revision the command is rejected with `RevisionConflict`. The store
serializes command updates per project within that store instance. On success,
`revision += 1` and an entry is
appended to `commandLog`:

```jsonc
{
  "id", "op", "actor", "expectedRevision", "revision", "payload",
  "before", "after", "ts", "revertsCommandId"?
}
```

### Operations

| op | payload | notes |
|----|---------|-------|
| `asset.import` | `Asset` | register an asset |
| `track.create` | `Track` | |
| `clip.create` | `{ id (or clipId), assetId, trackId, start, duration, sourceStart?, kind?, metadata? }` | place an existing asset on an existing track with default transform/effects/keyframes |
| `clip.split` | `{ clipId, at, newClipId? }` | `at` is timeline seconds inside the clip; effects are split and clip-local keyframes rebased |
| `clip.unsplit` | `{ clip, createdClipId }` | requires `revertsCommandId` for the recorded split; both halves must still match its output |
| `clip.move` | `{ clipId, start }` | |
| `clip.trim` | `{ clipId, start?, duration?, sourceStart? }` | |
| `clip.update` | `{ clipId, patch:{ start?,duration?,sourceStart?,trackId?,enabled?,muted?,volume?,transform?,effects?,keyframes? } }` | supplied fields replace their current values; transform replacement differs from `clip.transform` |
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

`clip.keyframes` replaces the envelope list. A volume envelope supplied through
that command or `clip.update` sets the clip's flat `volume` to `1.0`, so its
absolute gains are not attenuated twice. `clip.volume` accepts nonnegative values.

### Revert

`TimelineStore.revert_last_command(project_id, actor=..., expected_revision=...)`
selects the latest entry that is neither an inverse nor already reverted, builds
its supported inverse, and logs it as a normal command. The HTTP revert endpoint
requires `expectedRevision`.

Revert may fail: `asset.import`, `track.create`, and `clip.create` have no inverse,
and `clip.unsplit` requires unchanged split outputs. A `revertsCommandId` must
reference an unreverted original command and carry its exact inverse payload;
it cannot reference an inverse command. There is no redo-through-revert promise.
Use ABN reimport below to replay edits on an imported base; `replay_project`
starts from an empty project and cannot reconstruct unlogged imported assets.

---

## 4. ABN import and migration

The current importer accepts the ABN timeline structure:

- **`project_from_abn_timeline(project_id, abn_timeline, source_episode_id=None)`**
  Builds a fresh `v1` project from an ABN / EDL timeline shaped like
  `{ segments[].{segmentId,durationSec,shots[],audio,lowerThirds[]}, musicBed,
  totalSec, fps, width, height, title, episodeId }`.
  Maps shots to assets/clips, shot `kenBurns` to keyframes, and shot effects or
  positive `transitionSec` to effects. It imports segment voiceover/lower thirds
  and the music bed; malformed effect/keyframe inputs may be skipped.
- **`reimport_abn_timeline_preserving_commands(...)`**
  Rebuilds the imported base and replays the existing `commandLog` in order with
  updated expected revisions. Replay is best effort: commands raising
  `TimelineError` are skipped and recorded as `{commandId, op, reason}` in
  `metadata.migrationWarnings`. Only compatible edits are retained. Existing
  markers and notes are copied onto the rebuilt project, and metadata records
  `migratedFromRevision`.

The load route automatically reimports an existing project when a real ABN
timeline is available and its `metadata.abnImportVersion` differs from the
current importer version. It does not reimport on every base-file change. The
explicit `/editor-timelines/{project_id}/import-abn` route builds a fresh project;
it does not call the command-preserving migration helper.

### ABN field mapping

| ABN field | `editor-timeline/v1` |
|---------------|----------------------|
| `segments[].shots[]` | assets and clips keyed by stable imported ids |
| shot `startSec`, `durationSec` / `endSec`; segment `durationSec` | clip timeline start and duration within the accumulated segment layout |
| shot `clipStartSec` | clip `sourceStart` |
| shot `kenBurns` | moving `startScale/endScale`, `startX/endX`, `startY/endY` pairs become keyframe envelopes |
| shot `effects[]`, positive `transitionSec` | validated clip `effects` (`crossfade`, fades, grades) |
| segment `lowerThirds[]` | title assets and `lower_third` clips on `titles_1` |
| segment `audio.vo` | audio asset and `voiceover` clip on `audio_1` |
| `musicBed` | audio asset and `music_bed` clip on `music_1` |
| raw shot | clip `metadata.shot` |

---

## 5. Render cache

The HTTP layer stores full video results in `renderCache.video`, time-window
results in `renderCache.windows`, and frame results in `renderCache.frames`.
Entries carry the project revision used to render them. A result is not cached
if the stored project revision changed during its render (`cacheSkipped: true`).

After asset, track, or clip commands, the HTTP layer invalidates the entire
`renderCache`. Marker/note commands preserve existing results and refresh their
revision tags. Loading a project prunes entries with missing output files or
mismatched revision tags and stamps untagged legacy entries. This is global,
revision-bound invalidation; there is no content-addressed per-clip reuse contract.

## 6. Storage

`TimelineStore(directory)` persists one JSON document per `projectId`
(`load` / `save`, atomic writes via `services.json_store.atomic_save`).
`SCHEMA = "editor-timeline/v1"`, `ABN_IMPORT_VERSION = 2`.
