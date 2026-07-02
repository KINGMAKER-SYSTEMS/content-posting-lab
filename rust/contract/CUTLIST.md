# Endpoint Cut-List

> Generated 2026-07-02 from the frozen `openapi.json` (195 paths) diffed against
> every `/api` and `/ws` literal in `frontend/src`, plus a verified allowlist of
> endpoints with external callers (Campaign Hub, mini-app agent, admin ops).
> KEEP buckets are the Rust parity target. PROXY = ABN factory, reverse-proxied.

## KEEP — 72

- `GET /api/burn/batch-status/{batch_id}`
- `GET /api/burn/batches`
- `PATCH /api/burn/batches/{batch_id}/rename`
- `GET /api/burn/captions`
- `PATCH /api/burn/folders/rename`
- `GET /api/burn/fonts`
- `POST /api/burn/overlay`
- `GET /api/burn/videos`
- `GET /api/burn/zip/{batch_id}`
- `GET /api/captions/export/{username}`
- `GET /api/captions/history`
- `POST /api/captions/rename-batch`
- `POST,DELETE /api/clipper/cookies`
- `GET /api/clipper/cookies/status`
- `POST /api/clipper/download-url`
- `GET /api/clipper/jobs`
- `DELETE /api/clipper/jobs/{job_id}`
- `GET /api/clipper/jobs/{job_id}/download-all`
- `PATCH /api/clipper/jobs/{job_id}/rename`
- `POST /api/clipper/process-batch`
- `GET /api/clipper/process-batch/{job_id}`
- `POST /api/clipper/r2/upload-complete`
- `POST /api/clipper/r2/upload-init`
- `POST /api/clipper/stage-streamed`
- `POST /api/email/auto-create`
- `GET,POST /api/email/destinations`
- `DELETE /api/email/rules/{rule_id}`
- `GET /api/email/status`
- `GET /api/health`
- `GET,POST /api/projects/`
- `GET,DELETE /api/projects/{name}`
- `POST /api/recreate/generate-prompt`
- `GET /api/recreate/jobs`
- `DELETE /api/recreate/jobs/{job_id}`
- `GET /api/roster/`
- `POST /api/roster/dedup`
- `GET /api/roster/duplicates`
- `GET /api/roster/project/{project_name}`
- `POST /api/roster/sync`
- `POST /api/roster/sync-notion`
- `PUT,DELETE /api/roster/{integration_id}`
- `POST /api/telegram/assign-batch`
- `POST /api/telegram/batch/run`
- `PUT,DELETE /api/telegram/bot-token`
- `POST /api/telegram/forward/{integration_id}`
- `GET,POST /api/telegram/posters`
- `PUT,DELETE /api/telegram/posters/{poster_id}`
- `POST /api/telegram/posters/{poster_id}/pages`
- `DELETE /api/telegram/posters/{poster_id}/pages/{integration_id}`
- `POST /api/telegram/posters/{poster_id}/sync-topics`
- `GET,PUT /api/telegram/schedule`
- `POST /api/telegram/send`
- `GET,POST /api/telegram/sounds`
- `POST /api/telegram/sounds/forward-all`
- `POST /api/telegram/sounds/forward/{poster_id}`
- `POST /api/telegram/sounds/sync`
- `DELETE,PUT /api/telegram/sounds/{sound_id}`
- `GET,PUT /api/telegram/staging-group`
- `POST /api/telegram/staging-group/sync-topics`
- `GET /api/telegram/status`
- `POST /api/video/color-correct`
- `POST /api/video/color-correct/bulk`
- `DELETE /api/video/file`
- `POST /api/video/generate`
- `GET /api/video/jobs`
- `GET,DELETE /api/video/jobs/{job_id}`
- `GET,DELETE /api/video/prompts`
- `GET /api/video/provider-schemas`
- `GET /api/video/providers`

## KEEP (mini app) — 3

- `GET /api/miniapp/me`
- `GET,POST /api/miniapp/requests`
- `GET /api/miniapp/videos`

## KEEP (external client) — 11

- `POST /api/burn/import-tos`
- `GET /api/miniapp/agent/requests`
- `PATCH /api/miniapp/agent/requests/{request_id}`
- `GET,PUT,DELETE /api/telegram/pages/{integration_id}/playlist`
- `POST /api/telegram/pages/{integration_id}/playlist/songs`
- `DELETE /api/telegram/pages/{integration_id}/playlist/songs/{sound_id}`
- `GET /api/telegram/playlists`
- `POST /api/telegram/posters/{poster_id}/users`
- `DELETE /api/telegram/posters/{poster_id}/users/{user_id}`
- `POST /api/telegram/sound-assignments/send-all`
- `POST /api/telegram/sound-assignments/send/{poster_id}`

## PROXY — 49

- `GET /api/agenticnews/editor-render/capabilities`
- `POST /api/agenticnews/editor-render/{project_id}/frame`
- `POST /api/agenticnews/editor-render/{project_id}/render`
- `POST /api/agenticnews/editor-timelines`
- `GET /api/agenticnews/editor-timelines/{project_id}`
- `GET /api/agenticnews/editor-timelines/{project_id}/asset-health`
- `POST /api/agenticnews/editor-timelines/{project_id}/commands`
- `POST /api/agenticnews/editor-timelines/{project_id}/commands/revert-last`
- `POST /api/agenticnews/editor-timelines/{project_id}/import-abn`
- `GET /api/agenticnews/editor-timelines/{project_id}/openshot`
- `GET /api/agenticnews/editor/{ep_id}`
- `POST /api/agenticnews/editor/{ep_id}/apply`
- `POST /api/agenticnews/editor/{ep_id}/notes`
- `DELETE /api/agenticnews/editor/{ep_id}/notes/{note_id}`
- `POST /api/agenticnews/episodes/{ep_id}/publish`
- `GET /api/agenticnews/episodes/{ep_id}/publish-package`
- `GET /api/agenticnews/episodes/{ep_id}/qa`
- `POST /api/agenticnews/factory/pause`
- `POST /api/agenticnews/factory/resume`
- `GET /api/agenticnews/factory/state`
- `POST /api/agenticnews/gc`
- `GET,POST /api/agenticnews/jobs`
- `POST /api/agenticnews/jobs/{jid}/claim`
- `POST /api/agenticnews/jobs/{jid}/complete`
- `POST /api/agenticnews/jobs/{jid}/fail`
- `GET /api/agenticnews/memory`
- `GET /api/agenticnews/patterns`
- `GET /api/agenticnews/scratch-metrics`
- `GET /api/agenticnews/stats`
- `GET /api/agenticnews/stream`
- `POST /api/agenticnews/tools/approve`
- `POST /api/agenticnews/tools/assemble`
- `POST /api/agenticnews/tools/cards`
- `POST /api/agenticnews/tools/reject`
- `POST /api/agenticnews/tools/scrape`
- `POST /api/agenticnews/tools/tts`
- `GET,POST /api/agenticnews/videos`
- `PATCH,DELETE /api/agenticnews/videos/{vid}`
- `POST /api/agenticnews/videos/{vid}/move`
- `GET /api/agenticnews/workshop`
- `POST /api/pipeline/intake`
- `POST /api/pipeline/mint-alias`
- `GET /api/pipeline/stages`
- `POST /api/pipeline/{integration_id}/forward-to-topic`
- `GET /api/pipeline/{integration_id}/health`
- `POST /api/pipeline/{integration_id}/setup`
- `POST /api/pipeline/{integration_id}/transition`
- `POST /api/pipeline/{integration_id}/upload-presign`
- `GET /api/pipeline/{integration_id}/workspace`

## CUT (feature dropped) — 28

- `GET /api/slideshow/audio`
- `POST /api/slideshow/audio/upload`
- `DELETE /api/slideshow/audio/{filename}`
- `POST /api/slideshow/broll-montage`
- `GET /api/slideshow/broll-montage/spec`
- `GET /api/slideshow/captions`
- `POST,GET /api/slideshow/formats`
- `GET,DELETE /api/slideshow/formats/{name}`
- `GET,DELETE /api/slideshow/images`
- `DELETE /api/slideshow/images/{filename}`
- `GET /api/slideshow/job/{job_id}`
- `GET /api/slideshow/project-videos`
- `POST /api/slideshow/render`
- `POST /api/slideshow/render-meme`
- `POST /api/slideshow/render-v2`
- `GET /api/slideshow/renders`
- `DELETE /api/slideshow/renders/{filename}`
- `POST /api/slideshow/sounds/prepare`
- `GET /api/slideshow/sounds/{sound_id}/audio`
- `POST /api/slideshow/upload`
- `GET /api/upload/cookies`
- `GET /api/upload/cookies/{account_name}`
- `GET /api/upload/jobs`
- `GET /api/upload/jobs/{job_id}`
- `POST /api/upload/jobs/{job_id}/cancel`
- `POST /api/upload/login/{account_name}`
- `GET /api/upload/stats`
- `POST /api/upload/submit`

## CUT (replaced by tracing) — 6

- `POST /api/debug/clear`
- `GET /api/debug/errors`
- `GET /api/debug/health`
- `GET /api/debug/jobs/{job_id}`
- `GET /api/debug/logs`
- `GET /api/debug/stream`

## CUT (nothing calls it) — 26

- `POST /api/clipper/trim-batch`
- `POST /api/clipper/upload`
- `POST /api/clipper/upload-batch`
- `POST /api/clipper/upload-stream`
- `POST /api/projects/import-legacy`
- `GET /api/projects/videos/recent`
- `GET /api/projects/{name}/stats`
- `GET /api/projects/{name}/videos`
- `GET /api/roster/sync-notion/status`
- `GET /api/telegram/inventory`
- `GET /api/telegram/inventory/{integration_id}`
- `GET /api/telegram/log`
- `POST /api/telegram/posters/reset-defaults`
- `POST,GET /api/telegram/posters/{poster_id}/discover-topics`
- `GET /api/telegram/posters/{poster_id}/preview`
- `POST /api/telegram/send-batch`
- `DELETE /api/telegram/sounds/all`
- `POST /api/telegram/sounds/sync-hub`
- `POST /api/telegram/sounds/sync-notion`
- `POST /api/telegram/staging-group/scan-inventory/{integration_id}`
- `PUT /api/telegram/staging-group/topics`
- `DELETE /api/telegram/staging-group/topics/{integration_id}`
- `POST /api/video/bulk-delete`
- `POST /api/video/bulk-download`
- `GET /api/video/jobs/{job_id}/download-all`
- `GET /font-preview`
