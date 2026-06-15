# Editor Bay v2 Timeline Core

Status: CONT-13 implementation scaffold
Scope: renderer-agnostic project graph, command log, revision checks, replay, and API access.

## Boundary

This slice does not replace the existing Remotion review path. It creates the non-destructive editing
state that future Editor Bay UI, agent, and render workers will share.

Current legacy flow:

```text
ABN generated assets -> Remotion props timeline -> flattened MP4 -> review notes
```

New control-plane flow:

```text
ABN generated assets -> editor timeline project -> commands -> renderer adapter / UI / agent
```

## Project Model

Project files are stored under:

```text
agenticnews_assets/editor_timelines/<projectId>.json
```

Core entities:

- `assets`: source media ingredients such as image, video, audio, and title assets.
- `tracks`: ordered lanes for video, graphics, titles, voice, and music.
- `clips`: non-destructive placements of assets on tracks.
- `markers`: timeline markers for review and navigation.
- `notes`: HITL notes with optional suggested commands.
- `commandLog`: append-only mutation history with actor, expected revision, resulting revision, before/after snapshots, and payload.
- `renderCache`: reserved for later preview/export cache slices.

## Revision Rule

Every command must include `expectedRevision`. If it does not match the current project revision,
the API returns `409`. This gives human and agent clients an explicit conflict boundary instead of
silent clobbering.

## API

Create an empty project:

```bash
curl -X POST http://127.0.0.1:8000/api/agenticnews/editor-timelines \
  -H 'Content-Type: application/json' \
  -d '{"projectId":"ep_001_edit","title":"Episode 1 Edit"}'
```

Import an ABN Remotion timeline fixture as atomic assets/clips:

```bash
curl -X POST http://127.0.0.1:8000/api/agenticnews/editor-timelines/ep_001_edit/import-abn \
  -H 'Content-Type: application/json' \
  -d '{"sourceEpisodeId":"ep_001","timeline":{...}}'
```

Load current graph:

```bash
curl http://127.0.0.1:8000/api/agenticnews/editor-timelines/ep_001_edit
```

Apply one command:

```bash
curl -X POST http://127.0.0.1:8000/api/agenticnews/editor-timelines/ep_001_edit/commands \
  -H 'Content-Type: application/json' \
  -d '{
    "op":"clip.move",
    "actor":"human",
    "expectedRevision":4,
    "payload":{"clipId":"card_042","start":18.72}
  }'
```

## Command Surface

Implemented commands:

- `asset.import`
- `track.create`
- `clip.create`
- `clip.move`
- `clip.trim`
- `clip.split`
- `clip.update`
- `clip.hide`
- `clip.show`
- `clip.mute`
- `clip.unmute`
- `clip.transform`
- `clip.opacity`
- `clip.volume`
- `marker.add`
- `note.add`

## Replay And Revert

`services.editor_timeline.replay_project(project)` rebuilds the graph from the command log and is
covered by tests. `TimelineStore.revert_last_command()` currently supports inverse revert for trim
commands by appending a `clip.update` command that restores the prior timing. Broader undo/redo
should build on that append-only pattern instead of mutating history.

## Verification

Focused tests:

```bash
python -m pytest tests/test_editor_timeline.py tests/test_editor_timeline_api.py -q
```

These cover:

- ABN timeline fixture import into atomic assets/clips/tracks.
- Revision-checked commands and stale-client conflicts.
- Persistence and reload.
- Command replay.
- Trim revert as append-only inverse command.
- Split, marker, note, opacity, and validation failure paths.
- API create/load/import/command behavior.
