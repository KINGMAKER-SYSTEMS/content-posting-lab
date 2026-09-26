"""Route-auth CI guard (criteria §10 and §10b; route truth zcode/review-ds_labsec.md).

Every (method, path) the app serves -- APIRoutes, websockets, plain routes,
mounts, the SPA fallback -- must appear in TABLE below with a class:

  MONEY / WRITE / PII-READ / READ  must authenticate through services.route_auth,
                                   admitting exactly the listed caller classes
                                   (A = Cloudflare Access JWT, H = Hub X-API-Key,
                                   W = Worker bearer); fail closed (503) when the
                                   env is missing
  TOKEN       a pre-existing machine credential inside the route (bearer,
              per-job token, agent key)
  HMAC        Telegram initData (the Mini App)
  PUBLIC      keyless by design, with a one-line justification
  KNOWN-OPEN  open pending an owner decision; carries an owner and a PR

A new or unclassified route fails CI. The checks are behavioural: requests go
through the real app with APP_API_KEY unset (the production posture), and
for must-auth routes the handler is replaced by a sentinel so "refused" also
means "the handler never ran". docs/route-auth-table.md is the reviewed copy
of this table (kept in sync by test_docs_table_matches).

Mutation proof (criteria §10.5, §10b.6): deleting the dependency from any one
route turns test_must_auth_* red; the replayed commands are in the PR body.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketDisconnect

import app as app_module
from app import app
from services import route_auth
from tests import route_auth_support as s

pytestmark = pytest.mark.real_route_auth

MUST = {"MONEY", "WRITE", "PII-READ", "READ"}
KINDS = MUST | {"TOKEN", "HMAC", "PUBLIC", "KNOWN-OPEN"}
ABBR = {"A": route_auth.ACCESS, "H": route_auth.HUB, "W": route_auth.WORKER}
# Registered only when frontend/dist exists at import time.
OPTIONAL = {("GET", "/{full_path:path}")}

# (method, path) -> (class, callers, justification / provider call / owner+PR)
TABLE = {
    ('GET', '/api/control-plane/v1/capabilities'): ('READ', 'W', 'page recipe catalog (no PII); the Worker and manual tools send the bearer (Access bypasses /api/control-plane/*)'),
    ('GET', '/api/control-plane/v1/format-contracts'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('GET', '/api/control-plane/v1/roster'): ('PII-READ', 'W', 'roster/poster/account data'),
    ('POST', '/api/control-plane/v1/roster/refresh'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('POST', '/api/control-plane/v1/source-imports'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('POST', '/api/control-plane/v1/jobs'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('GET', '/api/control-plane/v1/jobs/{job_id}'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('GET', '/api/control-plane/v1/jobs/{job_id}/artifacts'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('GET', '/api/control-plane/v1/jobs/{job_id}/download/{index}'): ('TOKEN', '', 'per-job download token (?token=)'),
    ('GET', '/api/control-plane/v1/jobs/{job_id}/thumbnail/{index}'): ('TOKEN', '', 'per-job download token (?token=)'),
    ('POST', '/api/control-plane/v1/jobs/{job_id}/visual-admission/{index}'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('POST', '/api/control-plane/v1/post-renders'): ('WRITE', 'W', 'control-plane machine write; bearer now also a dependency (the body was validated before the inline check: 422 to anonymous)'),
    ('GET', '/api/control-plane/v1/post-renders/{job_id}'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (post_renders)'),
    ('POST', '/api/control-plane/v1/post-renders/{job_id}/retry'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (post_renders)'),
    ('POST', '/api/control-plane/v1/post-renders/{job_id}/provenance'): ('WRITE', 'W', 'control-plane machine write; bearer now also a dependency (was 422 to anonymous)'),
    ('GET', '/api/control-plane/v1/post-renders/{job_id}/artifacts/{kind}'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (post_renders)'),
    ('POST', '/api/control-plane/v1/dossier-ingredients'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer (inline)'),
    ('POST', '/api/control-plane/v1/recipes'): ('WRITE', 'W', 'control-plane machine write; bearer now also a dependency (was 422 to anonymous)'),
    ('GET', '/api/control-plane/v1/source-libraries/{library_id}'): ('TOKEN', '', 'CONTROL_PLANE_TOKEN bearer'),
    ('PUT', '/api/control-plane/v1/source-libraries/{library_id}/clips/{clip_sha256}'): ('WRITE', 'W', 'control-plane machine write; bearer now also a dependency (was 422 to anonymous)'),
    ('POST', '/api/control-plane/v1/source-libraries/{library_id}/finalize'): ('WRITE', 'W', 'control-plane machine write; bearer now also a dependency (was 422 to anonymous)'),
    ('GET', '/api/debug/logs'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug; fail-closed on Railway)'),
    ('GET', '/api/debug/stream'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug)'),
    ('GET', '/api/debug/jobs/{job_id}'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug)'),
    ('GET', '/api/debug/errors'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug)'),
    ('GET', '/api/debug/health'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug)'),
    ('POST', '/api/debug/clear'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (_gate_debug)'),
    ('GET', '/api/video/providers'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/video/provider-schemas'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/video/prompts'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/video/prompts'): ('WRITE', 'A', ''),
    ('DELETE', '/api/video/file'): ('WRITE', 'A', ''),
    ('POST', '/api/video/generate'): ('MONEY', 'A', 'Replicate/xAI: providers/base.py:322 mod.generate -> providers/replicate.py:279, providers/grok.py:29'),
    ('GET', '/api/video/jobs'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/video/jobs/{job_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/video/jobs/{job_id}'): ('WRITE', 'A', ''),
    ('GET', '/api/video/jobs/{job_id}/download-all'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/video/bulk-download'): ('WRITE', 'A', ''),
    ('POST', '/api/video/bulk-delete'): ('WRITE', 'A', ''),
    ('POST', '/api/video/color-correct'): ('WRITE', 'A', ''),
    ('POST', '/api/video/color-correct/bulk'): ('WRITE', 'A', ''),
    ('WS', '/api/captions/ws/{job_id}'): ('MONEY', 'A', "OpenAI: 'start' -> scraper/caption_extractor.py:74, scraper/sentiment_analyzer.py:86"),
    ('GET', '/api/captions/export/{username}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/captions/history'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/captions/rename-batch'): ('WRITE', 'A', ''),
    ('GET', '/api/burn/videos'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/burn/captions'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/burn/import-tos'): ('WRITE', 'A', ''),
    ('GET', '/api/burn/fonts'): ('PUBLIC', '', 'font catalog (names/files); the Worker font proxy calls it with no headers'),
    ('POST', '/api/burn/caption-render/v1'): ('WRITE', 'A', ''),
    ('POST', '/api/burn/overlay'): ('WRITE', 'A', ''),
    ('GET', '/api/burn/batch-status/{batch_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/burn/batches'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('PATCH', '/api/burn/batches/{batch_id}/rename'): ('WRITE', 'A', ''),
    ('PATCH', '/api/burn/folders/rename'): ('WRITE', 'A', ''),
    ('GET', '/api/burn/zip/{batch_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/projects/'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/projects/'): ('WRITE', 'A', ''),
    ('POST', '/api/projects'): ('WRITE', 'A', ''),
    ('GET', '/api/projects/videos/recent'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/projects/{name}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/projects/{name}'): ('WRITE', 'A', ''),
    ('GET', '/api/projects/{name}/stats'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/projects/{name}/videos'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/projects/import-legacy'): ('WRITE', 'A', ''),
    ('POST', '/api/recreate/generate-prompt'): ('MONEY', 'A', 'OpenAI: routers/recreate.py:293 chat.completions.create'),
    ('WS', '/api/recreate/ws/{job_id}'): ('MONEY', 'A', "Replicate: 'start' -> routers/recreate.py:199/212 remove_text -> providers/replicate.py:550"),
    ('GET', '/api/recreate/jobs'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/recreate/jobs/{job_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/r2/upload-init'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/r2/upload-complete'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/upload-stream'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/stage-streamed'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/upload'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/upload-batch'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/download-url'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/trim-batch'): ('WRITE', 'A', ''),
    ('POST', '/api/clipper/process-batch'): ('WRITE', 'A', ''),
    ('GET', '/api/clipper/process-batch/{job_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('WS', '/api/clipper/ws/{job_id}'): ('WRITE', 'A', ''),
    ('GET', '/api/clipper/jobs'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('PATCH', '/api/clipper/jobs/{job_id}/rename'): ('WRITE', 'A', ''),
    ('DELETE', '/api/clipper/jobs/{job_id}'): ('WRITE', 'A', ''),
    ('GET', '/api/clipper/jobs/{job_id}/download-all'): ('WRITE', 'A', 'uploads missing clips to R2 (routers/clipper.py:1591)'),
    ('GET', '/api/clipper/cookies/status'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/clipper/cookies'): ('WRITE', 'A', ''),
    ('DELETE', '/api/clipper/cookies'): ('WRITE', 'A', ''),
    ('GET', '/api/roster/'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('GET', '/api/roster/project/{project_name}'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('PUT', '/api/roster/{integration_id}'): ('WRITE', 'A', ''),
    ('DELETE', '/api/roster/{integration_id}'): ('TOKEN', '', 'require_roster_auth (CONTROL_PLANE_TOKEN or APP_API_KEY; 503 unset)'),
    ('GET', '/api/roster/duplicates'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/roster/dedup'): ('WRITE', 'A', ''),
    ('GET', '/api/roster/sync-notion/status'): ('PUBLIC', '', 'last-sync status only'),
    ('POST', '/api/roster/sync-notion'): ('WRITE', 'A', ''),
    ('POST', '/api/roster/sync'): ('WRITE', 'A', ''),
    ('GET', '/api/pages/'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('GET', '/api/pages/{page_id}/content'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/pages/{page_id}/project'): ('WRITE', 'A', ''),
    ('POST', '/api/pages/{page_id}/clips/move'): ('WRITE', 'A', ''),
    ('POST', '/api/slideshow/upload'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/images'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/slideshow/images/{filename}'): ('WRITE', 'A', ''),
    ('DELETE', '/api/slideshow/images'): ('WRITE', 'A', ''),
    ('POST', '/api/slideshow/render'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/job/{job_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/slideshow/renders'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/slideshow/renders/{filename}'): ('WRITE', 'A', ''),
    ('POST', '/api/slideshow/audio/upload'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/audio'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/slideshow/audio/{filename}'): ('WRITE', 'A', ''),
    ('POST', '/api/slideshow/render-v2'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/project-videos'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/slideshow/captions'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/slideshow/sounds/prepare'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/sounds/{sound_id}/audio'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/slideshow/render-meme'): ('WRITE', 'A', ''),
    ('POST', '/api/slideshow/formats'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/formats'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/slideshow/formats/{name}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('DELETE', '/api/slideshow/formats/{name}'): ('WRITE', 'A', ''),
    ('GET', '/api/slideshow/broll-montage/spec'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/slideshow/broll-montage'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/status'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('PUT', '/api/telegram/bot-token'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/bot-token'): ('WRITE', 'A', ''),
    ('PUT', '/api/telegram/staging-group'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/staging-group'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/telegram/staging-group/sync-topics'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/staging-group/scan-inventory'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/staging-group/scan-inventory'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/telegram/staging-group/scan-inventory/{integration_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/staging-group/discover-topics'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/staging-group/discover-topics'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('DELETE', '/api/telegram/staging-group/topics/{integration_id}'): ('WRITE', 'A', ''),
    ('PUT', '/api/telegram/staging-group/topics'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/posters'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('POST', '/api/telegram/posters'): ('WRITE', 'A', ''),
    ('PUT', '/api/telegram/posters/{poster_id}'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/posters/{poster_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/posters/{poster_id}/users'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/posters/{poster_id}/users/{user_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/posters/reset-defaults'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/posters/{poster_id}/pages'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/posters/{poster_id}/pages/{integration_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/posters/{poster_id}/sync-topics'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/posters/{poster_id}/discover-topics'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/posters/{poster_id}/discover-topics'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/telegram/send'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/send-batch'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/assign-batch'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/forward/{integration_id}'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/inventory/{integration_id}'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('GET', '/api/telegram/inventory'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('DELETE', '/api/telegram/inventory/scan'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/log'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('GET', '/api/telegram/sounds'): ('READ', 'A|H', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/telegram/sounds'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/sounds/all'): ('WRITE', 'A', ''),
    ('DELETE', '/api/telegram/sounds/{sound_id}'): ('WRITE', 'A', ''),
    ('PUT', '/api/telegram/sounds/{sound_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/sounds/sync-notion'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/sounds/sync-hub'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/sounds/sync'): ('WRITE', 'A|H', ''),
    ('POST', '/api/telegram/sounds/forward/{poster_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/sounds/forward-all'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/schedule'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('PUT', '/api/telegram/schedule'): ('WRITE', 'A', ''),
    ('POST', '/api/telegram/batch/run'): ('WRITE', 'A', ''),
    ('GET', '/api/telegram/pages/{integration_id}/playlist'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('PUT', '/api/telegram/pages/{integration_id}/playlist'): ('WRITE', 'A|H', ''),
    ('POST', '/api/telegram/pages/{integration_id}/playlist/songs'): ('WRITE', 'A|H', ''),
    ('DELETE', '/api/telegram/pages/{integration_id}/playlist/songs/{sound_id}'): ('WRITE', 'A|H', ''),
    ('DELETE', '/api/telegram/pages/{integration_id}/playlist'): ('WRITE', 'A|H', ''),
    ('GET', '/api/telegram/playlists'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('GET', '/api/telegram/posters/{poster_id}/preview'): ('PII-READ', 'A|H', 'roster/poster/account data'),
    ('POST', '/api/telegram/sound-assignments/send/{poster_id}'): ('WRITE', 'A|H', ''),
    ('POST', '/api/telegram/sound-assignments/send-all'): ('WRITE', 'A|H', ''),
    ('GET', '/api/miniapp/me'): ('HMAC', '', 'Telegram initData HMAC (services/miniapp_auth.py)'),
    ('GET', '/api/miniapp/videos'): ('HMAC', '', 'Telegram initData HMAC (services/miniapp_auth.py)'),
    ('GET', '/api/miniapp/requests'): ('HMAC', '', 'Telegram initData HMAC (services/miniapp_auth.py)'),
    ('POST', '/api/miniapp/requests'): ('HMAC', '', 'Telegram initData HMAC (services/miniapp_auth.py)'),
    ('GET', '/api/miniapp/agent/requests'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (X-Agent-Key); 503 when unset'),
    ('PATCH', '/api/miniapp/agent/requests/{request_id}'): ('TOKEN', '', 'MINIAPP_AGENT_KEY (X-Agent-Key); 503 when unset'),
    ('GET', '/api/email/status'): ('PUBLIC', '', 'configured flag + mail domain only'),
    ('DELETE', '/api/email/rules/{rule_id}'): ('WRITE', 'A|W', 'deletes a CF rule; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4)'),
    ('POST', '/api/email/auto-create'): ('WRITE', 'A|W', 'creates a CF rule; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4)'),
    ('GET', '/api/email/destinations'): ('PII-READ', 'A|W', 'team inboxes; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4)'),
    ('POST', '/api/email/destinations'): ('WRITE', 'A|W', 'adds a CF destination; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4)'),
    ('POST', '/api/pipeline/mint-alias'): ('WRITE', 'A', 'mints a CF alias + Notion placeholder; operator-only (lead decision 5)'),
    ('POST', '/api/pipeline/intake'): ('WRITE', 'A', 'Notion + roster writes; operator-only (lead decision 5); the #177 tamper guard stays'),
    ('GET', '/api/pipeline/stages'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/pipeline/{integration_id}/setup'): ('WRITE', 'A', ''),
    ('POST', '/api/pipeline/{integration_id}/transition'): ('WRITE', 'A', ''),
    ('GET', '/api/pipeline/{integration_id}/workspace'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/pipeline/{integration_id}/upload-presign'): ('WRITE', 'A', ''),
    ('POST', '/api/pipeline/{integration_id}/forward-to-topic'): ('WRITE', 'A', ''),
    ('GET', '/api/pipeline/{integration_id}/health'): ('PUBLIC', '', 'setup checks: booleans, R2 object count, cookie status, Telegram topic name; no credentials (triggers one R2 list)'),
    ('POST', '/api/upload/submit'): ('WRITE', 'A', ''),
    ('GET', '/api/upload/jobs'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('GET', '/api/upload/jobs/{job_id}'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/upload/jobs/{job_id}/cancel'): ('WRITE', 'A', ''),
    ('GET', '/api/upload/cookies'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('GET', '/api/upload/cookies/{account_name}'): ('PII-READ', 'A', 'roster/poster/account data'),
    ('POST', '/api/upload/login/{account_name}'): ('WRITE', 'A', ''),
    ('GET', '/api/upload/stats'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/stream'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/factory/state'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/factory/pause'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/factory/resume'): ('MONEY', 'A', 'resumes the autonomous factory: Codex image_gen services/abn_factory.py:112'),
    ('POST', '/api/agenticnews/tools/approve'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/tools/reject'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/videos'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/videos'): ('WRITE', 'A', ''),
    ('PATCH', '/api/agenticnews/videos/{vid}'): ('WRITE', 'A', ''),
    ('DELETE', '/api/agenticnews/videos/{vid}'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/videos/{vid}/move'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/jobs'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/jobs'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/jobs/{jid}/claim'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/jobs/{jid}/complete'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/jobs/{jid}/fail'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/tools/tts'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/tools/cards'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/tools/assemble'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/tools/scrape'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/episodes/{ep_id}/qa'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/patterns'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/episodes/{ep_id}/publish-package'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/episodes/{ep_id}/publish'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/gc'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/memory'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/workshop'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/scratch-metrics'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/stats'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/editor-timelines'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/editor-timelines/{project_id}/import-abn'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/editor-timelines/{project_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/editor-timelines/{project_id}/asset-health'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/editor-timelines/{project_id}/commands'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/editor-timelines/{project_id}/commands/revert-last'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/editor-timelines/{project_id}/openshot'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/agenticnews/editor-render/capabilities'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/editor-render/{project_id}/render'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/editor-render/{project_id}/frame'): ('WRITE', 'A', ''),
    ('GET', '/api/agenticnews/editor/{ep_id}'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('POST', '/api/agenticnews/editor/{ep_id}/notes'): ('WRITE', 'A', ''),
    ('DELETE', '/api/agenticnews/editor/{ep_id}/notes/{note_id}'): ('WRITE', 'A', ''),
    ('POST', '/api/agenticnews/editor/{ep_id}/apply'): ('WRITE', 'A', ''),
    ('GET', '/api/projects'): ('READ', 'A', 'operator read, no PII/spend (gated with its router)'),
    ('GET', '/api/health'): ('PUBLIC', '', 'liveness; presence flags only (no values)'),
    ('MOUNT', '/fonts'): ('PUBLIC', '', 'font files; read by the Worker font proxy and slideshow-agent'),
    ('MOUNT', '/agenticnews-assets'): ('PUBLIC', '', 'rendered ABN media (public by design, WATCH)'),
    ('GET', '/workspace'): ('PUBLIC', '', 'static HTML shell; its APIs are gated'),
    ('GET', '/factory'): ('PUBLIC', '', 'static HTML shell; its APIs are gated'),
    ('GET', '/abn-editor/{ep_id}'): ('PUBLIC', '', 'static HTML shell; its APIs are gated'),
    ('GET', '/abn-editor'): ('PUBLIC', '', 'static HTML shell; its APIs are gated'),
    ('MOUNT', '/projects'): ('PUBLIC', '', '#172 allowlist: <project>/<media kind>/.../<media file> only'),
    ('MOUNT', '/output'): ('PUBLIC', '', 'generated media, public by design (WATCH)'),
    ('MOUNT', '/caption-output'): ('PUBLIC', '', 'generated media, public by design (WATCH)'),
    ('MOUNT', '/burn-output'): ('PUBLIC', '', 'generated media, public by design (WATCH)'),
    ('GET', '/font-preview'): ('PUBLIC', '', 'static HTML page'),
    ('GET', '/{full_path:path}'): ('PUBLIC', '', 'SPA fallback; only files inside frontend/dist (traversal-guarded)'),
}


def _callers(code: str) -> frozenset[str]:
    return frozenset(ABBR[c] for c in code.split("|") if c)


def _rows(*kinds):
    return sorted(k for k, (kind, _, _) in TABLE.items() if kind in kinds)


MUST_KEYS = _rows(*MUST)
TOKEN_KEYS = _rows("TOKEN")
HMAC_KEYS = _rows("HMAC")
KNOWN_OPEN_KEYS = _rows("KNOWN-OPEN")
LANE = {"X-RT-Lane": "content-bucket-control-plane", "X-RT-Page-Id": "dummy-page"}


def _route_auth_callers(route) -> list[frozenset[str]]:
    found = []

    def walk(dependant):
        for dep in dependant.dependencies:
            callers = getattr(dep.call, "route_auth_callers", None)
            if callers:
                found.append(callers)
            walk(dep)

    walk(route.dependant)
    return found


# ── 1. the table covers the app exactly ─────────────────────────────


def test_every_route_is_classified():
    keys = s.route_keys(app)
    assert len(keys) == len(set(keys)), "duplicate (method, path) registrations"
    unclassified = [k for k in keys if k not in TABLE]
    assert not unclassified, f"classify these routes in TABLE: {unclassified}"
    stale = [k for k in TABLE if k not in keys and k not in OPTIONAL]
    assert not stale, f"TABLE rows for routes that no longer exist: {stale}"
    for key, (kind, callers, note) in TABLE.items():
        assert kind in KINDS, key
        assert bool(callers) == (kind in MUST), f"{key}: callers only (and always) on must-auth rows"
        if kind in ("PUBLIC", "TOKEN", "HMAC", "KNOWN-OPEN"):
            assert note.strip(), f"{key}: {kind} needs a one-line justification"
        if kind == "KNOWN-OPEN":
            assert "owner:" in note and re.search(r"PR #\d+", note), f"{key}: KNOWN-OPEN needs an owner and a PR"
        if kind == "MONEY":
            assert re.search(r"\.py:\d+", note), f"{key}: MONEY names the provider call (file:line)"


def test_keyless_allowlist_is_the_reviewed_one():
    """PUBLIC grows only by editing this list too (criteria §10b.4 + the font catalog)."""
    assert set(_rows("PUBLIC")) == {
        ("GET", "/api/health"),
        ("GET", "/workspace"), ("GET", "/factory"), ("GET", "/abn-editor"), ("GET", "/abn-editor/{ep_id}"),
        ("GET", "/font-preview"),
        ("MOUNT", "/fonts"), ("MOUNT", "/agenticnews-assets"), ("MOUNT", "/projects"),
        ("MOUNT", "/output"), ("MOUNT", "/caption-output"), ("MOUNT", "/burn-output"),
        ("GET", "/{full_path:path}"),
        ("GET", "/api/email/status"),
        ("GET", "/api/pipeline/{integration_id}/health"), ("GET", "/api/roster/sync-notion/status"),
        ("GET", "/api/burn/fonts"),
    }
    # Empty since lead decision 5 (mint-alias and intake are operator-only). A new
    # KNOWN-OPEN row must carry an owner and a PR and be added here deliberately.
    assert set(KNOWN_OPEN_KEYS) == set()


def test_must_auth_routes_declare_exactly_their_callers():
    for key in MUST_KEYS:
        route = s.find_route(app, *key)
        declared = _route_auth_callers(route)
        assert declared == [_callers(TABLE[key][1])], f"{key}: declared {declared}, table {TABLE[key][1]}"
    for key, (kind, _, _) in TABLE.items():
        if kind in MUST or key[0] == "MOUNT":
            continue
        try:
            route = s.find_route(app, *key)
        except KeyError:
            continue
        assert _route_auth_callers(route) == [], f"{key} is {kind} but carries a route_auth dependency"


# ── 2. behaviour: must-auth routes, handler replaced by a sentinel ──


@pytest.fixture
def sentinel(monkeypatch):
    """Swap every must-auth handler for a recorder; returns the call log."""
    calls: list[tuple[str, str]] = []
    for key in MUST_KEYS:
        route = s.find_route(app, *key)
        dependant = route.dependant
        if isinstance(route, APIWebSocketRoute):
            ws_param = dependant.websocket_param_name

            async def record_ws(__key=key, __ws=ws_param, **kwargs):
                calls.append(__key)
                await kwargs[__ws].accept()
                await kwargs[__ws].close()

            monkeypatch.setattr(dependant, "call", record_ws)
        elif asyncio.iscoroutinefunction(dependant.call):

            async def record_async(__key=key, **kwargs):
                calls.append(__key)
                return PlainTextResponse("route-auth-sentinel")

            monkeypatch.setattr(dependant, "call", record_async)
        else:

            def record_sync(__key=key, **kwargs):
                calls.append(__key)
                return PlainTextResponse("route-auth-sentinel")

            monkeypatch.setattr(dependant, "call", record_sync)
    return calls


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.delenv("APP_API_KEY", raising=False)
    s.configure_all(monkeypatch)


def _send(client, key, headers):
    """Status for an HTTP route; for a websocket 101 if accepted, else the close code."""
    method, path = key
    url = s.fill(path)
    if method == "WS":
        try:
            with client.websocket_connect(url, headers=headers):
                return 101
        except WebSocketDisconnect as exc:
            return exc.code
    return client.request(method, url, headers=headers).status_code


REFUSED = {401, 403, 1008}


def _wrong_sets_for(key):
    """Wrong credentials for this route. The email-routing routes keep #176's
    CONTROL_PLANE_TOKEN-in-X-API-Key form, so that one set is right for them."""
    sets = s.wrong_header_sets()
    if key in EMAIL_ROUTING:
        sets.pop("worker-token-as-x-api-key")
    return sets


EMAIL_ROUTING = {
    ("DELETE", "/api/email/rules/{rule_id}"), ("POST", "/api/email/auto-create"),
    ("GET", "/api/email/destinations"), ("POST", "/api/email/destinations"),
}


@pytest.mark.parametrize("key", MUST_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_must_auth_refuses_missing_and_wrong_credentials(key, configured, sentinel):
    client = TestClient(app, raise_server_exceptions=False)
    assert _send(client, key, {}) in REFUSED, f"{key} without credentials"
    for label, headers in _wrong_sets_for(key).items():
        status = _send(client, key, headers)
        assert status in REFUSED, f"{key} with {label} -> {status}"
    assert sentinel == [], f"{key}: handler ran without a valid credential"


@pytest.mark.parametrize("key", MUST_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_must_auth_admits_exactly_the_listed_callers(key, configured, sentinel):
    client = TestClient(app, raise_server_exceptions=False)
    allowed = _callers(TABLE[key][1])
    for caller in route_auth.CALLER_CLASSES:
        before = len(sentinel)
        status = _send(client, key, s.valid_headers(caller))
        if caller in allowed:
            # Past auth: the sentinel answers, or (no body sent) FastAPI's body
            # validation, which runs only after every dependency passed, says 422.
            assert status in (200, 101, 422), f"{key}: {caller} refused ({status})"
            assert len(sentinel) == before + (status != 422), key
        else:
            assert status in REFUSED and len(sentinel) == before, f"{key}: {caller} admitted ({status})"


@pytest.mark.parametrize("key", MUST_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_must_auth_fails_closed_when_unconfigured(key, monkeypatch, sentinel):
    monkeypatch.delenv("APP_API_KEY", raising=False)
    s.unconfigure_all(monkeypatch)
    s.install_idp(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    expected = 1008 if key[0] == "WS" else 503
    assert _send(client, key, {}) == expected
    assert _send(client, key, s.all_valid_headers()) == expected
    assert sentinel == []


def test_app_api_key_set_does_not_replace_route_auth(configured, sentinel, monkeypatch):
    """/api/telegram in both APP_API_KEY modes (criteria §10.3): unset = route auth only;
    set = middleware AND route auth; the (browser-bundlable) APP_API_KEY alone never passes."""
    key = ("PUT", "/api/telegram/bot-token")
    client = TestClient(app, raise_server_exceptions=False)
    assert _send(client, key, {}) == 401
    monkeypatch.setattr(app_module, "_APP_API_KEY", s.APP_KEY)
    assert _send(client, key, {}) == 401
    assert _send(client, key, {"X-API-Key": s.APP_KEY}) == 401
    headers = {"X-API-Key": s.APP_KEY, **s.valid_headers(route_auth.ACCESS)}
    assert client.put("/api/telegram/bot-token", json={"token": "dummy"}, headers=headers).status_code == 200
    assert sentinel == [key]


# ── 3. behaviour: the real handlers, with every side effect watched ─


@pytest.fixture
def side_effects(monkeypatch, tmp_path, isolated_projects_root):
    """Record (and refuse) outbound network, subprocesses and provider calls."""
    import providers.grok as grok
    import providers.replicate as replicate
    import routers.captions as captions
    import routers.recreate as recreate
    import routers.video as video
    import services.abn_factory as factory
    import services.roster as roster
    import services.telegram as tg
    from services import json_store

    log: list[str] = []

    def refuse(label):
        def _refuse(*args, **kwargs):
            log.append(label)
            raise OSError(f"{label} refused by the route-auth guard")

        return _refuse

    async def refuse_async(*args, **kwargs):
        log.append("async")
        raise OSError("refused by the route-auth guard")

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("dns"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse("httpx"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    # provider clients (criteria §10b.5: money routes make ZERO provider calls)
    for module, name in (
        (video, "generate_one"), (replicate, "generate"), (grok, "generate"), (replicate, "remove_text"),
        (recreate, "_get_openai"), (recreate, "_run_pipeline"), (captions, "_run_pipeline"),
        (factory, "resume"), (factory, "_codex_image"),
    ):
        monkeypatch.setattr(module, name, refuse(f"provider {module.__name__}.{name}"))
    # roster / Telegram state the destructive routes would mutate
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    roster.set_page("acct:dummy", {"name": "dummy", "provider": "tiktok", "status": "Live", "project": "dummy-project"})
    (isolated_projects_root / "dummy-project" / "videos").mkdir(parents=True)
    (isolated_projects_root / "dummy-project" / "videos" / "a.mp4").write_bytes(b"dummy")

    def snapshot():
        files = {}
        for root in (tmp_path,):
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    files[str(p)] = p.read_bytes()
        return files

    before = snapshot()
    yield log
    assert snapshot() == before, "a refused request changed state on disk"


def _body(method):
    return {"json": {}} if method in ("POST", "PUT", "PATCH") else {}


def test_must_auth_real_handlers_refuse_with_no_side_effect(configured, side_effects):
    client = TestClient(app, raise_server_exceptions=False)
    bad = []
    for key in MUST_KEYS:
        method, path = key
        for label, headers in {"none": {}, **_wrong_sets_for(key)}.items():
            if method == "WS":
                status = _send(client, key, headers)
            else:
                status = client.request(method, s.fill(path), headers=headers, **_body(method)).status_code
            if status not in REFUSED:
                bad.append((key, label, status))
    assert not bad, bad[:20]
    assert side_effects == [], side_effects[:20]


def test_must_auth_real_handlers_503_with_no_side_effect(monkeypatch, side_effects):
    monkeypatch.delenv("APP_API_KEY", raising=False)
    s.unconfigure_all(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    for key in MUST_KEYS:
        method, path = key
        if method == "WS":
            assert _send(client, key, {}) == 1008
        else:
            assert client.request(method, s.fill(path), **_body(method)).status_code == 503, key
    assert side_effects == []


def test_money_route_with_the_right_credential_reaches_the_provider(configured, monkeypatch):
    """Non-vacuous: the mock the refusal tests watch is on the real generate path."""
    import routers.video as video

    provider_id = next(iter(video.PROVIDERS))
    monkeypatch.setitem(video.API_KEYS, video.PROVIDERS[provider_id]["key_id"], "dummy-provider-key")
    called = []

    async def fake_generate_one(job_id, index, *args, jobs=None, **kwargs):
        called.append(job_id)

    monkeypatch.setattr(video, "generate_one", fake_generate_one)
    form = {"prompt": "dummy", "provider": provider_id, "count": "1", "project": "dummy-project"}
    with TestClient(app) as client:
        assert client.post("/api/video/generate", data=form).status_code == 401
        assert called == []
        r = client.post("/api/video/generate", data=form, headers=s.valid_headers(route_auth.ACCESS))
        assert r.status_code == 200, r.text
        for _ in range(100):
            if called:
                break
            time.sleep(0.01)
    assert called == [r.json()["job_id"]]


def test_destructive_and_pii_routes_keep_their_behaviour_with_the_right_credential(configured, isolated_projects_root, monkeypatch, tmp_path):
    import services.roster as roster
    from services import json_store

    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    roster.set_page("acct:dummy", {"name": "dummy", "provider": "tiktok", "status": "Live", "project": "dummy-project"})
    for sub in ("videos", "captions"):
        (isolated_projects_root / "dummy-project" / sub).mkdir(parents=True)
    client = TestClient(app, raise_server_exceptions=False)
    hub, access, worker = (s.valid_headers(c) for c in (route_auth.HUB, route_auth.ACCESS, route_auth.WORKER))

    r = client.get("/api/roster/", headers=hub)  # the Hub's roster read
    assert r.status_code == 200 and any(p.get("integration_id") == "acct:dummy" for p in r.json()["pages"])
    r = client.get("/api/control-plane/v1/roster", headers={**worker, **LANE})
    assert r.status_code == 200 and r.json()["schema"], r.text
    assert client.get("/api/control-plane/v1/roster", headers=LANE).status_code == 401

    assert client.delete("/api/projects/dummy-project").status_code == 401
    assert (isolated_projects_root / "dummy-project").exists()
    assert client.delete("/api/projects/dummy-project", headers=access).status_code == 200
    assert not (isolated_projects_root / "dummy-project").exists()


# ── 4. pre-existing TOKEN / HMAC routes and the KNOWN-OPEN ones ─────


@pytest.mark.parametrize("key", TOKEN_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_token_routes_refuse_missing_and_wrong_credentials(key, configured, monkeypatch, side_effects):
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "dummy-agent-key-not-a-secret")
    client = TestClient(app, raise_server_exceptions=False)
    method, path = key
    # Per-job download tokens are looked up after the job: an unknown id is 404
    # before the token compare (tests/test_control_plane_jobs.py covers the 403).
    ok = REFUSED | ({404} if "{index}" in path and method == "GET" else set())
    wrong = {k: v for k, v in s.wrong_header_sets().items() if k != "worker-token-as-x-api-key"}
    for label, headers in {"none": {}, **wrong, "agent-wrong": {"X-Agent-Key": "wrong"}}.items():
        status = client.request(method, s.fill(path), headers={**LANE, **headers}, **_body(method)).status_code
        assert status in ok, f"{key} with {label} -> {status}"
    assert side_effects == []


@pytest.mark.parametrize("key", HMAC_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_hmac_routes_refuse_missing_and_forged_init_data(key, configured, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:dummy-bot-token-not-a-secret")
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    method, path = key
    for headers in ({}, {"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&hash=00"}, {"Authorization": "tma forged"},
                    s.all_valid_headers()):
        status = client.request(method, path, headers=headers, **_body(method)).status_code
        assert status in (401, 403), f"{key} -> {status}"


@pytest.mark.parametrize("key", KNOWN_OPEN_KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_known_open_routes_are_still_open(key, configured, monkeypatch):
    """Asserts CURRENT behaviour: closing the hole flips this case (criteria §10.3)."""
    route = s.find_route(app, *key)
    assert _route_auth_callers(route) == []

    async def record(**kwargs):
        return PlainTextResponse("open")

    monkeypatch.setattr(route.dependant, "call", record)
    # No body: an anonymous caller gets past every dependency to body validation.
    status = TestClient(app).request(key[0], key[1]).status_code
    assert status not in (401, 403, 503), "KNOWN-OPEN route now refuses: reclassify it in TABLE"


# ── 5. named holes (criteria §10.3) ─────────────────────────────────


def test_named_holes_are_closed(configured, side_effects):
    client = TestClient(app, raise_server_exceptions=False)
    for method, path in (("POST", "/api/email/auto-create"), ("GET", "/api/email/destinations"),
                         ("POST", "/api/email/destinations"), ("PUT", "/api/telegram/bot-token"),
                         ("GET", "/api/control-plane/v1/roster"), ("POST", "/api/roster/dedup"),
                         ("DELETE", "/api/projects/dummy-project"), ("POST", "/api/agenticnews/gc"),
                         ("POST", "/api/video/generate"), ("POST", "/api/pipeline/mint-alias"),
                         ("POST", "/api/pipeline/intake"), ("DELETE", "/api/email/rules/dummy-rule")):
        assert client.request(method, path, headers=LANE, **_body(method)).status_code == 401, path
    assert side_effects == []


@pytest.mark.parametrize("key", sorted(EMAIL_ROUTING), ids=lambda k: f"{k[0]} {k[1]}")
def test_email_routing_admits_access_and_the_control_plane_token_in_either_slot(key, configured, sentinel):
    """Lead decision 4: the UI's Access login works on the email buttons, and the
    #176 machine credential (CONTROL_PLANE_TOKEN as Bearer OR X-API-Key) still does."""
    client = TestClient(app, raise_server_exceptions=False)
    for headers in (s.valid_headers(route_auth.ACCESS), s.valid_headers(route_auth.WORKER),
                    {"X-API-Key": s.WORKER_TOKEN}):
        assert _send(client, key, headers) in (200, 422), (key, headers.keys())
    for headers in (s.valid_headers(route_auth.HUB), {"X-API-Key": s.APP_KEY}):
        assert _send(client, key, headers) == 401, (key, headers.keys())


@pytest.mark.parametrize("key", [("GET", "/api/miniapp/agent/requests"),
                                 ("PATCH", "/api/miniapp/agent/requests/{request_id}")],
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_miniapp_agent_routes_fail_closed_without_the_agent_key(key, configured, monkeypatch, side_effects, tmp_path_factory):
    """Lead decision 6 (ds_labsec F4): MINIAPP_AGENT_KEY unset is 503, never open."""
    import services.content_requests as content_requests

    # outside tmp_path, which the side_effects fixture byte-compares
    monkeypatch.setattr(content_requests, "REQUESTS_PATH", tmp_path_factory.mktemp("requests") / "r.json")
    content_requests.add_request(poster_id="p", poster_name="P", text="dummy", page_id=None,
                                 page_name=None, quantity=None, source="miniapp")
    before = content_requests.list_requests(status=None)
    monkeypatch.delenv("MINIAPP_AGENT_KEY", raising=False)
    client = TestClient(app, raise_server_exceptions=False)
    method, path = key
    for headers in ({}, {"X-Agent-Key": "anything"}, s.all_valid_headers()):
        r = client.request(method, s.fill(path), headers=headers, **_body(method))
        assert r.status_code == 503, (key, r.status_code)
    assert content_requests.list_requests(status=None) == before
    assert side_effects == []


# ── 5b. CSRF: an edge-added Access JWT rides the login cookie (review D1) ─

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
ACCESS_KEYS = [k for k in MUST_KEYS if "A" in TABLE[k][1].split("|")]
ACCESS_UNSAFE = [k for k in ACCESS_KEYS if k[0] in UNSAFE]
ACCESS_WS = [k for k in ACCESS_KEYS if k[0] == "WS"]
JWT = lambda: {"Cf-Access-Jwt-Assertion": s.mint()}  # noqa: E731


@pytest.mark.parametrize("key", ACCESS_UNSAFE, ids=lambda k: f"{k[0]} {k[1]}")
def test_cross_origin_access_writes_are_refused(key, configured, sentinel):
    """A cross-site form POST carrying the operator's cookie (so the edge adds a valid
    JWT) is 403 and never reaches the handler; the UI's same-origin fetch passes."""
    client = TestClient(app, raise_server_exceptions=False)
    method, path = key
    form = {"data": {"x": "1"}} if method != "DELETE" else {}
    for label, headers in {
        "foreign origin": {**JWT(), "Origin": "https://evil.example"},
        "cross-site fetch metadata": {**JWT(), "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        "same-site sibling": {**JWT(), "Sec-Fetch-Site": "same-site", "Origin": s.LAB_ORIGIN},
        "no origin signal": JWT(),
        "null origin": {**JWT(), "Origin": "null"},
    }.items():
        r = client.request(method, s.fill(path), headers=headers, **form)
        assert r.status_code == 403, f"{key} {label} -> {r.status_code}"
    assert sentinel == []
    for headers in ({**JWT(), "Sec-Fetch-Site": "same-origin"}, {**JWT(), "Origin": s.LAB_ORIGIN}):
        assert _send(client, key, headers) in (200, 422)


@pytest.mark.parametrize("key", ACCESS_WS, ids=lambda k: f"{k[0]} {k[1]}")
def test_cross_origin_access_websockets_are_refused(key, configured, sentinel):
    client = TestClient(app, raise_server_exceptions=False)
    assert _send(client, key, {**JWT(), "Origin": "https://evil.example"}) == 1008
    assert _send(client, key, JWT()) == 1008
    assert sentinel == []
    assert _send(client, key, {**JWT(), "Origin": s.LAB_ORIGIN}) == 101
    assert sentinel == [key]


def test_machine_callers_need_no_origin(configured, sentinel):
    """Bearer and Hub-key callers are not cookie-borne, so no Origin is required."""
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/roster/dedup", headers=JWT()).status_code == 403
    # past auth (422: the empty body then fails validation)
    assert client.post("/api/control-plane/v1/recipes", headers=s.valid_headers(route_auth.WORKER)).status_code in (200, 422)
    assert client.post("/api/telegram/sounds/sync", headers=s.valid_headers(route_auth.HUB)).status_code == 200
    # a safe method with the cookie JWT is not a CSRF write
    assert client.get("/api/roster/", headers={**JWT(), "Sec-Fetch-Site": "cross-site"}).status_code == 200


# ── 5c. auth runs before the body is read (review D3) ───────────────


def test_anonymous_malformed_bodies_are_401_not_422(configured, sentinel):
    client = TestClient(app, raise_server_exceptions=False)
    bad = []
    for method, path in MUST_KEYS:
        if method not in ("POST", "PUT", "PATCH"):
            continue
        for body in ({"content": b"{not json", "headers": {"content-type": "application/json"}},
                     {"content": b"--x\r\nbroken", "headers": {"content-type": "multipart/form-data; boundary=x"}}):
            status = client.request(method, s.fill(path), **body).status_code
            if status != 401:
                bad.append((method, path, status))
    assert not bad, bad[:20]
    assert sentinel == []


def test_anonymous_multipart_upload_is_not_parsed_or_spooled(configured, monkeypatch, tmp_path_factory):
    import tempfile

    from starlette import formparsers

    spool = tmp_path_factory.mktemp("spool")
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    parsed = []
    real_parse = formparsers.MultiPartParser.parse

    async def spy(self):
        parsed.append(1)
        return await real_parse(self)

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", spy)
    client = TestClient(app, raise_server_exceptions=False)
    big = b"\0" * (2 * 1024 * 1024)  # past SpooledTemporaryFile's in-memory limit
    for path in ("/api/video/generate", "/api/clipper/upload", "/api/slideshow/upload", "/api/slideshow/audio/upload"):
        r = client.post(path, files={"file": ("a.mp4", big, "video/mp4")}, data={"prompt": "x"})
        assert r.status_code == 401, (path, r.status_code)
    assert parsed == []
    assert list(spool.iterdir()) == []


# ── 6. mounts and the SPA fallback: traversal ───────────────────────


def test_mounts_do_not_traverse(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    secret = "route-auth-traversal-sentinel"
    (tmp_path / "secret.txt").write_text(secret)
    (tmp_path / "projects" / "p1" / "videos").mkdir(parents=True)
    (tmp_path / "projects" / "page_roster.json").write_text(secret)
    (tmp_path / "projects" / "p1" / "videos" / "a.mp4").write_bytes(b"media")
    for mount_dir in ("output", "caption_output", "burn_output", "fonts"):
        (tmp_path / mount_dir).mkdir()
    (tmp_path / "output" / "a.mp4").write_bytes(b"media")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/projects/p1/videos/a.mp4").content == b"media"
    assert client.get("/output/a.mp4").content == b"media"
    for url in (
        "/projects/page_roster.json", "/projects/%2e%2e/secret.txt", "/projects//page_roster.json",
        "/projects/p1/videos/..%2f..%2fpage_roster.json", "/projects/p1/videos/%2e%2e/%2e%2e/page_roster.json",
        "/projects/p1/VIDEOS/..%2F..%2Fpage_roster.json", "/PROJECTS/page_roster.json",
        "/output/%2e%2e/secret.txt", "/output/..%2fsecret.txt", "/output//secret.txt", "/OUTPUT/../secret.txt",
        "/caption-output/%2e%2e/secret.txt", "/burn-output/%2e%2e/secret.txt", "/fonts/%2e%2e/secret.txt",
        "/agenticnews-assets/%2e%2e/secret.txt", "/%2e%2e/secret.txt", "//secret.txt", "/..%2fsecret.txt",
    ):
        r = client.get(url)
        assert secret not in r.text, f"{url} served the sentinel ({r.status_code})"
        if url.lower().startswith(("/projects/", "/output/", "/caption-output/", "/burn-output/", "/fonts/", "/agenticnews-assets/")) and url.split("/")[1] in ("projects", "output", "caption-output", "burn-output", "fonts", "agenticnews-assets"):
            assert r.status_code == 404, f"{url} -> {r.status_code}"


def test_spa_fallback_is_traversal_guarded(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("index")
    (tmp_path / "secret.txt").write_text("route-auth-spa-sentinel")
    for bad in ("../secret.txt", "/etc/passwd", "..%2fsecret.txt", "a/../../secret.txt", "", "./index.html"):
        assert app_module._frontend_file(bad, dist) is None, bad
    assert app_module._frontend_file("index.html", dist) == (dist / "index.html").resolve()


# ── 7. the Worker's contentLabClient calls still pass (criteria §10b.7) ─

# (method, path, extra headers) exactly as control-plane-worker/src/adapters/contentLabClient.js
# and postRenderClient.js send them (Authorization: Bearer <CONTENT_LAB_TOKEN> on every call,
# X-RT-Lane always, X-RT-Page-Id when page-scoped); the font proxy
# (src/ops/dossierGateway.js:384-388) sends no headers at all.
WORKER_CALLS = [
    ("GET", "/api/control-plane/v1/capabilities", {"X-RT-Page-Id": "dummy-page"}),
    ("GET", "/api/control-plane/v1/format-contracts", {}),
    ("POST", "/api/control-plane/v1/dossier-ingredients", {"X-RT-Page-Id": "dummy-page"}),
    ("GET", "/api/control-plane/v1/roster", {}),
    ("POST", "/api/control-plane/v1/roster/refresh", {}),
    ("POST", "/api/control-plane/v1/source-imports", {"X-RT-Page-Id": "dummy-page", "Idempotency-Key": "k1"}),
    ("POST", "/api/control-plane/v1/jobs", {"X-RT-Page-Id": "dummy-page", "Idempotency-Key": "k2"}),
    ("POST", "/api/control-plane/v1/recipes", {"X-RT-Page-Id": "dummy-page", "Idempotency-Key": "k3"}),
    ("GET", "/api/control-plane/v1/jobs/{job_id}", {"X-RT-Page-Id": "dummy-page"}),
    ("GET", "/api/control-plane/v1/jobs/{job_id}/artifacts", {"X-RT-Page-Id": "dummy-page"}),
    ("POST", "/api/control-plane/v1/jobs/{job_id}/visual-admission/{index}", {"X-RT-Page-Id": "dummy-page"}),
    ("POST", "/api/control-plane/v1/post-renders", {"X-RT-Page-Id": "dummy-page", "Idempotency-Key": "k4"}),
    ("GET", "/api/control-plane/v1/post-renders/{job_id}", {"X-RT-Page-Id": "dummy-page"}),
    ("POST", "/api/control-plane/v1/post-renders/{job_id}/retry", {"X-RT-Page-Id": "dummy-page"}),
    ("POST", "/api/control-plane/v1/post-renders/{job_id}/provenance", {"X-RT-Page-Id": "dummy-page"}),
    ("GET", "/api/control-plane/v1/post-renders/{job_id}/artifacts/{kind}", {"X-RT-Page-Id": "dummy-page"}),
]
WORKER_FONT_CALLS = [("GET", "/api/burn/fonts"), ("GET", "/fonts/dummy.ttf")]


@pytest.mark.parametrize("method,path,extra", WORKER_CALLS, ids=lambda v: v if isinstance(v, str) else "")
def test_worker_calls_pass_route_auth_with_the_bearer(method, path, extra, configured, monkeypatch, tmp_path):
    """With the Worker's own headers every call gets past authentication (whatever the
    handler then says about the dummy body); without the bearer it is refused."""
    import routers.post_renders as post_renders
    import services.notion_pages as notion_pages

    # Configure the services behind the calls so a 503 can only mean auth.
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_ROOT", str(tmp_path / "post-renders"))
    monkeypatch.setattr(post_renders, "_service", None)
    monkeypatch.setattr(notion_pages, "is_configured", lambda: True)

    async def no_sync():
        return {"added": 0, "updated": 0, "errors": []}

    monkeypatch.setattr(notion_pages, "sync_into_roster", no_sync)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-RT-Lane": "content-bucket-control-plane", "X-Content-Lab-Contract": "v1",
               "X-Request-Id": "dummy-request", **extra}
    body = {"json": {}} if method == "POST" else {}
    url = s.fill(path)
    with_bearer = client.request(method, url, headers={**headers, **s.valid_headers(route_auth.WORKER)}, **body)
    assert with_bearer.status_code not in (401, 403, 503), (path, with_bearer.status_code, with_bearer.text[:200])
    without = client.request(method, url, headers=headers, **body)
    assert without.status_code in (401, 403, 422), (path, without.status_code)
    if TABLE[(method, path)][0] in MUST:
        assert without.status_code == 401


@pytest.mark.parametrize("method,path", WORKER_FONT_CALLS)
def test_worker_font_proxy_needs_no_credential(method, path, configured, monkeypatch, tmp_path):
    client = TestClient(app, raise_server_exceptions=False)
    assert client.request(method, path).status_code not in (401, 403, 503)


# ── 8. the reviewed docs copy stays in sync ─────────────────────────


def test_docs_table_matches():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "route-auth-table.md").read_text()
    rows = {}
    for line in doc.splitlines():
        m = re.match(r"\| `([A-Z]+)` \| `([^`]+)` \| ([A-Z-]+) \| ([^|]*) \|", line)
        if m:
            rows[(m.group(1), m.group(2))] = (m.group(3), m.group(4).strip())
    cred = {"Access JWT": "A", "Hub x-api-key": "H", "Worker bearer": "W"}
    expected = {}
    for key, (kind, callers, _) in TABLE.items():
        names = [n for n, c in cred.items() if c in callers.split("|")] if callers else []
        expected[key] = (kind, " or ".join(names) or "-")
    assert rows == expected
