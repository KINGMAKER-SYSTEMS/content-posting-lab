# Editor Bay v2 Render Backend

Status: CONT-14 implementation scaffold
Scope: renderer adapter boundary, backend capability detection, layered ffmpeg fallback, and API render entrypoints.

## Boundary

OpenShot/libopenshot remains the preferred long-term backend for a real NLE-grade renderer. This
slice does not claim OpenShot parity yet because the local Python bindings are not importable in
this environment.

Local backend check:

```text
openshot module: missing
ffmpeg: /opt/homebrew/bin/ffmpeg
ffprobe: /opt/homebrew/bin/ffprobe
mlt melt: missing
blender: missing
```

The slice therefore exposes a stable renderer interface and ships a working ffmpeg implementation
so Editor Bay can render real project artifacts now while keeping OpenShot as the first adapter slot.

## Renderer Interface

Implemented in `services/editor_render.py`:

- `detect_render_backends()` reports OpenShot, ffmpeg, MLT, and Blender availability.
- `choose_renderer(output_dir, asset_root=...)` selects OpenShot when importable, then ffmpeg.
- `OpenShotRenderer` declares the preferred adapter boundary and raises a clear local wiring error.
- `FFmpegLayeredRenderer.render(project, output_path=...)` exports an MP4 from timeline clips.
- `FFmpegLayeredRenderer.render_frame(project, at=..., output_path=...)` exports a preview PNG.

Supported fallback clip behavior:

- image, title, and video assets as visual overlays
- audio assets mixed with start offsets, trim, volume, and mute
- track ordering by timeline track index
- non-destructive clip placement using `start`, `duration`, `sourceStart`, and `transform`
- transform `x`, `y`, `scale`, and `opacity`
- `/agenticnews-assets/...` source resolution against the local asset root

## API

Mounted under `/api/agenticnews`:

```bash
curl http://127.0.0.1:8000/api/agenticnews/editor-render/capabilities
```

```bash
curl -X POST http://127.0.0.1:8000/api/agenticnews/editor-render/ep_001_edit/render
```

```bash
curl -X POST http://127.0.0.1:8000/api/agenticnews/editor-render/ep_001_edit/frame \
  -H 'Content-Type: application/json' \
  -d '{"at":12.5}'
```

Artifacts are written under:

```text
agenticnews_assets/editor_renders/
```

The API also writes the latest video/frame result into the project's `renderCache` so UI and agent
clients have a discoverable render artifact path after the request completes.

## Why This Slice Matters

The first Editor Bay prototype had a fake-feeling timeline because the final Remotion file flattened
the pieces too early. This slice proves that a commanded timeline project can produce a real artifact
from atomic assets without regenerating every upstream asset. Moving a clip changes the rendered
frame while preserving the original asset path, and replacing one asset changes the frame without
changing the clip.

## Known Gaps

- OpenShot bindings need installation/build validation before this can become the primary renderer.
- The ffmpeg fallback is intentionally small; it is not a full timeline engine.
- Text/title generation is still asset-driven. Rich editable titles should become a later slice.
- Render jobs are synchronous API calls for now. Background job state belongs in the queue slice.

## Verification

Focused tests:

```bash
python -m pytest tests/test_editor_render_api.py tests/test_editor_render.py -q
```

Current result:

```text
5 passed in 1.09s
```

These cover:

- backend capability reporting with OpenShot marked preferred
- layered MP4 export
- preview frame export
- clip movement changing preview output without regenerating the source asset
- single-asset replacement changing rendered output while preserving clip identity
- FastAPI render capability, MP4 render, and frame render endpoints
