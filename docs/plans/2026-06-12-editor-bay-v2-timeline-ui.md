# Editor Bay v2 Timeline UI

Status: CONT-15 implementation scaffold
Scope: browser timeline workstation backed by the commanded timeline model.

## Boundary

This slice replaces the legacy Editor Bay review scratchpad with a v2-first timeline UI under:

```text
/editor/<projectId>
```

The page now reads:

```text
GET /api/agenticnews/editor-timelines/<projectId>
```

and every edit action sends:

```text
POST /api/agenticnews/editor-timelines/<projectId>/commands
```

The UI does not maintain a private mutation path. It uses optimistic selection/playhead state only;
project graph changes come from backend command responses.

## Implemented Surface

- Real track rows from `project.tracks`.
- Real clip blocks from `project.clips`.
- Clip selection and inspector sync.
- Clip nudge using `clip.move`.
- Timing edit using `clip.trim`.
- Split using `clip.split`.
- Hide/show using `clip.hide` and `clip.show`.
- Mute/unmute using `clip.mute` and `clip.unmute`.
- Transform/property edits using `clip.transform` and `clip.volume`.
- Marker creation using `marker.add`.
- Clip-targeted notes using `note.add`.
- Suggested command payload attached to notes for agent follow-up.
- Preview frame request through `POST /api/agenticnews/editor-render/<projectId>/frame`.
- Responsive stacking for mobile while the timeline pane keeps its own horizontal scroll.

## Browser QA Flow

Seeded a local `demo_edit` project through the same backend API:

1. `POST /api/agenticnews/editor-timelines`
2. `asset.import` for image and audio assets.
3. `clip.create` for `card_clip` and `vo_clip`.
4. Loaded `http://127.0.0.1:8015/editor/demo_edit`.
5. Selected `card_clip`.
6. Clicked `Nudge clip later`.
7. Added a targeted note: `Browser QA note for card timing`.

Observed browser state:

```text
revision after nudge: rev 5
revision after note: rev 6
card_clip.start in backend state: 1.5
note op in backend command log: note.add
console warnings/errors: none
mobile document horizontal overflow: false
```

## Verification

Focused frontend test:

```bash
npm test -- EditorBay.test.tsx
```

Result:

```text
1 passed, 3 tests passed
```

Build:

```bash
npm run build
```

Result:

```text
tsc -b && vite build exited 0
```

Focused backend/editor suite:

```bash
python -m pytest tests/test_editor_timeline.py tests/test_editor_timeline_api.py tests/test_editor_render.py tests/test_editor_render_api.py tests/test_ralph_loop.py -q
```

Result:

```text
17 passed in 1.67s
```

## Known Gaps

- Drag uses pointer movement and commits `clip.move` on release, but automated coverage currently focuses on the explicit nudge command path.
- Frame annotation is represented as frame/time-targeted notes; freehand drawing from the legacy scratchpad is intentionally not carried forward in this slice.
- The UI is still a control-plane scaffold, not a full media authoring suite. Rich transitions/effects and render job queue status belong to later slices.
