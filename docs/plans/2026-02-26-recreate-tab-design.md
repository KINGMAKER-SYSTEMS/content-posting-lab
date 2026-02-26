# Recreate Tab — Design Document

**Date:** 2026-02-26
**Status:** Approved

## Purpose

Extract first and last frames from a TikTok reference video, remove burned-in text overlays using FLUX.1 Kontext on Replicate, and save the clean frames as reference material for I2V video generation (wan-i2v / wan-i2v-fast).

## Input / Output

- **Input:** TikTok video URL (single video, not batch)
- **Output:** Clean first + last frame images saved to `projects/{name}/recreate/{job_id}/`
- **Handoff:** Manual — user downloads or picks up frames themselves for the Generate tab

## Backend

### Router: `routers/recreate.py` — mounted at `/api/recreate/*`

**WebSocket endpoint:** `/api/recreate/ws/{job_id}`

Client sends:
```json
{"action": "start", "video_url": "https://tiktok.com/@user/video/123", "project": "my-project"}
```

**Pipeline stages (streamed via WebSocket):**

1. `downloading` — yt-dlp downloads video via `scraper/frame_extractor.download_video()`
2. `extracting_frames` — ffprobe gets duration, ffmpeg extracts first (t=0.0s) and last (t=duration-0.1s) frames via `extract_frame()`
3. `frames_ready` — sends original frames as base64 previews to client
4. `removing_text` — sends each frame to FLUX.1 Kontext text-removal on Replicate (sequential, sends `text_removing` per frame)
5. `complete` — saves clean frames, sends base64 previews + file paths

Error at any stage sends `{"type": "error", "message": "..."}` and closes WebSocket.

**REST endpoints:**
- `GET /api/recreate/jobs?project=` — list completed recreate jobs
- `DELETE /api/recreate/jobs/{job_id}?project=` — delete a job directory

**File storage:**
```
projects/{name}/recreate/{job_id}/
├── source_video.mp4
├── first_frame_original.jpg
├── last_frame_original.jpg
├── first_frame_clean.png
└── last_frame_clean.png
```

### Replicate Integration

**Model:** `flux-kontext-apps/text-removal`

**Input:** `{"input_image": "<data_uri>"}` — aspect_ratio defaults to `match_input_image`, output_format defaults to `png`.

**New function:** `providers/replicate.py:remove_text(image_data_uri, client)` — same polling pattern as `generate()`, 120s timeout per frame.

**Cost:** ~$0.04/frame, ~$0.08/job.

## Frontend

### Page: `pages/Recreate.tsx`

**Left panel:**
- TikTok URL input with URL validation
- "Extract & Clean" button
- Real-time status log (pipeline events as they stream)

**Right panel:**
- Empty state when no job running
- 2x2 frame preview grid: originals (top), cleaned (bottom)
- Download links beneath each clean frame
- Job history list below (from GET endpoint) with thumbnails, delete buttons

### Tab Integration

- New nav tab: `{ path: '/recreate', label: 'Recreate' }` between Captions and Burn
- CSS display toggling — always mounted
- Zustand: `recreateJobActive` boolean for "LIVE" badge

### WebSocket

Reuses `useWebSocket.ts` with `shouldReconnect` guard (same pattern as Captions tab).

## Constraints

- Single video at a time (no batch)
- Requires `REPLICATE_API_TOKEN` in .env (already used by video generation)
- Requires `yt-dlp` and `ffmpeg` on PATH (already required by other tabs)
