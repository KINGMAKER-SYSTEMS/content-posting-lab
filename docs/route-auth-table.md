# Content Lab route-auth table

This is the reviewed copy of the classification in `tests/test_route_auth_guard.py`
(`TABLE`). `test_docs_table_matches` fails CI when the two disagree, and
`test_every_route_is_classified` fails when the app serves a route that is in
neither.

## Why

`app.py`'s `check_api_key` middleware enforces `APP_API_KEY` only when it is set,
and production keeps it unset (setting it breaks the Campaign Hub proxies and the
UI, and the frontend can bake it into its public bundle). Every route that spends
provider money, changes state or returns roster PII therefore authenticates on its
own through `services/route_auth.py`, independent of `APP_API_KEY`:

- no credential, or a wrong one: **401** (websocket: close 1008), and the handler never runs;
- none of the route's caller classes configured: **503**, fail closed, the handler never runs;
- a route admits **any one** valid credential among the classes listed for it, and nothing else;
- an Access-authenticated write (POST/PUT/PATCH/DELETE) or websocket without a same-origin
  signal: **403** (CSRF, see below);
- `RouteAuthMiddleware` applies the same check before FastAPI reads the body, so an
  unauthenticated request is never parsed (no 422, no multipart spool); the per-route
  dependency repeats it. It also runs the header-only checks of the TOKEN/HMAC routes
  marked `before_body` (the Mini App agent key and initData), so no non-PUBLIC route
  answers an anonymous body with 422.

## Caller classes and what each caller must configure

Values are Eric-owned. Agents never set or print them; this repository is public
and its tests use dummy values only.

| Class | Credential the Lab checks | Lab env (Railway) | Caller side |
|---|---|---|---|
| **W**: control-plane Worker | `Authorization: Bearer <token>`, constant-time | `CONTROL_PLANE_TOKEN` (already set) | Worker secret `CONTENT_LAB_TOKEN` (already sent on every contentLabClient call; no change) |
| **H**: Campaign Hub proxies | `X-API-Key: <key>`, constant-time. Not `APP_API_KEY` | `LAB_HUB_API_KEY` (new) | Hub env `CONTENT_LAB_HUB_API_KEY` (new, same value) in the Flask and Rust Hub services. Code change needed in both: Flask `blueprints/sound_assignments.py` `_proxy` and Rust `crates/api/src/content_lab.rs` send no headers today |
| **A**: operators (and scripts) through Cloudflare Access | `Cf-Access-Jwt-Assertion`, verified: RS256 signature against `<team>/cdn-cgi/access/certs`, `aud` = the Lab Access app's AUD tag, `iss` = the team domain, `exp`/`iat` required. The unverified header, `Cf-Access-Authenticated-User-Email` or any other identity header is never trusted | `LAB_ACCESS_TEAM_DOMAIN` (`https://<team>.cloudflareaccess.com`) and `LAB_ACCESS_AUD` (the Lab Access application's AUD tag; comma-separated for several) | Browsers: an Access login on the Lab hostname. Scripts: an Access service token (`CF-Access-Client-Id/Secret`, e.g. `RT_ACCESS_CLIENT_ID/SECRET`) against the Access-protected hostname; Access then adds the JWT. Scripts also send `Origin: <Lab origin>` on writes (next section) |

No secret is baked into the frontend bundle. The UI keeps calling same-origin
`/api/*`; Cloudflare adds the JWT at the edge.

### CSRF on the Access class

The edge adds the JWT from the operator's `CF_Authorization` login cookie, so a
cross-site form POST or websocket from another page would arrive with a valid JWT.
An Access-authenticated write or websocket is therefore admitted only with
`Sec-Fetch-Site: same-origin` (every current browser sends it on the UI's own
fetches) or an `Origin` listed in `LAB_ALLOWED_ORIGINS` (Lab env, comma-separated;
set it to the Lab's Access hostname, e.g. `https://lab.<zone>`; the match is exact and
case-sensitive, as browsers send it, and never list `null`). Anything else is 403 and
the handler never runs. Plain GETs are not checked (a cross-site page cannot read the
response), except GETs that change state, which are marked `@side_effect_get` and
checked like writes: today only `GET /api/clipper/jobs/{id}/download-all` (it backfills
clips to R2). Bearer and Hub-key callers are not cookie-borne and need no Origin. Set the
Access application's cookie to **SameSite=Lax (or Strict) and HttpOnly** as defence in
depth.

## Ships only after the Cloudflare Access edge gate (a) is live

Every must-auth route the Lab UI calls accepts only an Access JWT (or, for the Hub
routes, also the Hub key). Until the Lab is served on a Cloudflare-proxied hostname
with an Access application in front of it (zcode/access-kit.md section B, steps 1-4),
no browser request carries a JWT, so **every UI screen except the Mini App, the
public pages and the email-status line would get 401**. Deploy order:

1. (a) live: custom domain on the Lab, proxied DNS, Access app with the bypass list
   (`/api/control-plane/*`, `/api/health`, `/fonts/*`, `/api/burn/fonts`, `/m*`,
   `/assets/*`, `/api/miniapp/*`, `/projects/*`), cookie SameSite=Lax or Strict and
   HttpOnly; operators log in on that hostname.
2. Hub code sends `X-API-Key` from `CONTENT_LAB_HUB_API_KEY` (both Hubs), deployed.
3. Set `LAB_HUB_API_KEY`, `LAB_ACCESS_TEAM_DOMAIN`, `LAB_ACCESS_AUD`,
   `LAB_ALLOWED_ORIGINS` on the Lab, and confirm `MINIAPP_AGENT_KEY` is set
   (Railway), then deploy this change in a Lab gap (11:40/14:40/17:40/20:40 ET).
4. Keep `APP_API_KEY` unset. It is no longer needed for protection; setting it would
   still break the Worker (it sends the control-plane bearer, not `APP_API_KEY`).

Before step 3 the new routes answer 503 (fail closed), never open.

## Counts

| Class | Routes | Credential |
|---|---|---|
| MONEY | 5 | A (5) |
| WRITE (state-changing / destructive) | 141 | A (121, incl. mint-alias and intake), A or H (7), A or W (3: email routing), W (10: control-plane writes whose inline bearer check came after the body or the lane check; the bearer is now also a dependency) |
| PII-READ | 25 | A (17), A or H (6), A or W (1: `GET /api/email/destinations`), W (1: `GET /api/control-plane/v1/roster`) |
| READ (gated with its router, no PII/spend) | 53 | A (51), A or H (1: `GET /api/telegram/sounds`), W (1: `GET /api/control-plane/v1/capabilities`) |
| TOKEN (unchanged, credential inside the route) | 18 | bearer / per-job token / agent key |
| HMAC (Telegram initData) | 4 | Mini App |
| PUBLIC (keyless allowlist) | 17 | none |
| KNOWN-OPEN | 0 | (mint-alias and intake are operator-only since lead decision 5) |
| **Total** | **263** | |

By credential set over the 224 must-auth routes: A only 194, A or H 14, A or W 4, W only 12.
"A or W" on the four email-routing routes means an Access JWT or CONTROL_PLANE_TOKEN
as Bearer **or** X-API-Key (the #176 contract is kept).

## Callers found (grep, 2026-09-26) and what changes for them

| Caller | Lab routes | Sends today | After this change |
|---|---|---|---|
| Worker `src/adapters/contentLabClient.js` (1315-1490), `postRenderClient.js` (4-116) | `/api/control-plane/v1/*` | Bearer `CONTENT_LAB_TOKEN` on every call | unchanged, passes (test_worker_calls_pass_route_auth_with_the_bearer) |
| Worker font proxy `src/ops/dossierGateway.js:384-388` | `GET /api/burn/fonts`, `/fonts/*` | no headers | unchanged (both PUBLIC) |
| Hub Flask `blueprints/sound_assignments.py:27-142` (`CONTENT_LAB_URL`) | 13 `/api/telegram/*` routes + `GET /api/roster/` | no headers | **must send `X-API-Key`** (breaks until it does) |
| Hub Rust `crates/api/src/content_lab.rs:80-808` (`CONTENT_LAB_URL`, writes need `CONTENT_LAB_ALLOW_WRITES=1`) | same GET/PUT/DELETE routes + `POST /api/telegram/sounds/sync` | no headers | **must send `X-API-Key`** |
| ShipStream Worker (branch only) | `/v1/jobs/{id}/download|thumbnail/{i}?token=` | per-job token | unchanged |
| `shipstream-internal-posting/reconcile_lab_projects.py` (launchd daily 07:15) | `GET /api/projects/`, `GET /api/pages/`, `POST /api/pages/{id}/project` | no headers | **breaks** until it uses an Access service token on the Access hostname |
| `tools/deliver_lab_clips_to_vault.sh` (manual) | `GET /api/pages/{id}/content`, `/projects/*` | none | pages call needs an Access service token; media stays public |
| `tools/supply_projection.py`, `zcode/lab-capacity-check.sh` (manual) | `GET /api/control-plane/v1/capabilities` | `X-RT-Page-Id` only | **needs the bearer** (`CONTROL_PLANE_TOKEN`): Access bypasses `/api/control-plane/*`, so no JWT is ever added there |
| Prompt/batch scripts: `agents/prompting-agent/*`, ocean content-agent harness, `content/prompt-bible/*`, `rt/content-distro/gen_pipeline.py`, `skills/telegram-distro` (manual) | `/api/video/generate`, `/api/video/jobs/{id}`, `/api/video/prompts`, `/api/projects/*` | none (the ocean harness: optional Bearer `CONTENT_LAB_API_KEY`) | **break** until they use an Access service token (these are the paid-generation routes) |
| `agents/core-core-music-editor-agent` (manual) | `/api/clipper/download-url`, `/api/clipper/trim-batch` | none | Access service token |
| `agents/slideshow-agent` (manual) | `/api/health`, `/api/slideshow/*`, `/projects/*`, `/fonts/*` | none | slideshow calls need an Access service token |
| `rt/rt-command-center/dashboard.html` (Lab iframe) | the SPA | the viewer's browser | a cross-site iframe would need the Access cookie as SameSite=None, which this change does not recommend (CSRF); open the Lab in its own tab |
| Lab UI `frontend/src` | the rows marked UI below | no credential (optional `VITE_APP_API_KEY` is unset) | works once (a) is live; see the next section |

## UI routes and the edge gate (a)

- **Work before and after (a):** `/api/health`, `/api/email/status`, `/api/burn/fonts`,
  the Mini App (`/api/miniapp/*`, HMAC), static media and the SPA shell.
- **Need (a):** every route marked UI with an Access credential below (130 rows:
  video, recreate, captions websocket, burn, clipper, projects, slideshow, pages,
  roster, the whole pipeline flow including mint-alias and intake, telegram, upload,
  agenticnews and editor, and the email-routing buttons). Before (a) is live they
  answer 401 (or 503 until the Lab env is set).
- **Fixed by (a):** the email-routing buttons (`POST /api/email/auto-create`,
  `GET`/`POST /api/email/destinations`, `DELETE /api/email/rules/{id}`), broken in
  the UI since #176/#177, accept the operator's Access JWT alongside
  `CONTROL_PLANE_TOKEN` (lead decision 4).

## KNOWN-OPEN

None. `POST /api/pipeline/mint-alias` and `POST /api/pipeline/intake` are
operator-only (Access) by default (lead decision 5). The #177 guards stay: intake
still refuses a live handle or a live page's `notion_page_id` unless the caller also
presents `CONTROL_PLANE_TOKEN` (so that override now needs Access **and** the token).
Public intake with a rate limit comes back only if Eric decides new-account intake
must stay public; there is deliberately no env switch that reopens it.

`/api/miniapp/agent/*` (TOKEN, `X-Agent-Key`) fails closed (503) when
`MINIAPP_AGENT_KEY` is unset (lead decision 6, ds_labsec F4).

## Every route

UI = the Lab frontend calls this path (grep of `frontend/src`, path-level).

| Method | Path | Class | Credential | UI | Other callers | Note |
|---|---|---|---|---|---|---|
| `GET` | `/api/control-plane/v1/capabilities` | READ | Worker bearer |  | Worker, supply_projection.py, lab-capacity-check.sh | page recipe catalog (no PII); the Worker and manual tools send the bearer (Access bypasses /api/control-plane/*) |
| `GET` | `/api/control-plane/v1/format-contracts` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (inline) |
| `GET` | `/api/control-plane/v1/roster` | PII-READ | Worker bearer |  | Worker | roster/poster/account data |
| `POST` | `/api/control-plane/v1/roster/refresh` | WRITE | Worker bearer |  | Worker | Notion sync; bearer as a dependency so auth precedes the X-RT-Lane check (was 400 to anonymous) |
| `POST` | `/api/control-plane/v1/source-imports` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer as a dependency so auth precedes the body (review D8) |
| `POST` | `/api/control-plane/v1/jobs` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer as a dependency so auth precedes the body (review D8) |
| `GET` | `/api/control-plane/v1/jobs/{job_id}` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (inline) |
| `GET` | `/api/control-plane/v1/jobs/{job_id}/artifacts` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (inline) |
| `GET` | `/api/control-plane/v1/jobs/{job_id}/download/{index}` | TOKEN | - |  | ShipStream (?token=) | per-job download token (?token=) |
| `GET` | `/api/control-plane/v1/jobs/{job_id}/thumbnail/{index}` | TOKEN | - |  | ShipStream (?token=) | per-job download token (?token=) |
| `POST` | `/api/control-plane/v1/jobs/{job_id}/visual-admission/{index}` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer as a dependency so auth precedes the body (review D8) |
| `POST` | `/api/control-plane/v1/post-renders` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer now also a dependency (the body was validated before the inline check: 422 to anonymous) |
| `GET` | `/api/control-plane/v1/post-renders/{job_id}` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (post_renders) |
| `POST` | `/api/control-plane/v1/post-renders/{job_id}/retry` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (post_renders) |
| `POST` | `/api/control-plane/v1/post-renders/{job_id}/provenance` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer now also a dependency (was 422 to anonymous) |
| `GET` | `/api/control-plane/v1/post-renders/{job_id}/artifacts/{kind}` | TOKEN | - |  | Worker | CONTROL_PLANE_TOKEN bearer (post_renders) |
| `POST` | `/api/control-plane/v1/dossier-ingredients` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer as a dependency so auth precedes the body (review D8) |
| `POST` | `/api/control-plane/v1/recipes` | WRITE | Worker bearer |  | Worker | control-plane machine write; bearer now also a dependency (was 422 to anonymous) |
| `GET` | `/api/control-plane/v1/source-libraries/{library_id}` | TOKEN | - |  |  | CONTROL_PLANE_TOKEN bearer |
| `PUT` | `/api/control-plane/v1/source-libraries/{library_id}/clips/{clip_sha256}` | WRITE | Worker bearer |  |  | control-plane machine write; bearer now also a dependency (was 422 to anonymous) |
| `POST` | `/api/control-plane/v1/source-libraries/{library_id}/finalize` | WRITE | Worker bearer |  |  | control-plane machine write; bearer now also a dependency (was 422 to anonymous) |
| `GET` | `/api/debug/logs` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug; fail-closed on Railway) |
| `GET` | `/api/debug/stream` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug) |
| `GET` | `/api/debug/jobs/{job_id}` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug) |
| `GET` | `/api/debug/errors` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug) |
| `GET` | `/api/debug/health` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug) |
| `POST` | `/api/debug/clear` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (_gate_debug) |
| `GET` | `/api/video/providers` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/video/provider-schemas` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/video/prompts` | READ | Access JWT | yes | prompt-bible scripts | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/video/prompts` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/video/file` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/video/generate` | MONEY | Access JWT | yes | prompt/batch scripts (prompting-agent, ocean harness, content-distro, telegram-distro) | Replicate/xAI: providers/base.py:322 mod.generate -> providers/replicate.py:279, providers/grok.py:29 |
| `GET` | `/api/video/jobs` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/video/jobs/{job_id}` | READ | Access JWT | yes | prompt/batch scripts (prompting-agent, ocean harness, content-distro, telegram-distro) | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/video/jobs/{job_id}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/video/jobs/{job_id}/download-all` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/video/bulk-download` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/video/bulk-delete` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/video/color-correct` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/video/color-correct/bulk` | WRITE | Access JWT | yes |  |  |
| `WS` | `/api/captions/ws/{job_id}` | MONEY | Access JWT | yes |  | OpenAI: 'start' -> scraper/caption_extractor.py:74, scraper/sentiment_analyzer.py:86 |
| `GET` | `/api/captions/export/{username}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/captions/history` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/captions/rename-batch` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/burn/videos` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/burn/captions` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/burn/import-tos` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/burn/fonts` | PUBLIC | - | yes | Worker | font catalog (names/files); the Worker font proxy calls it with no headers |
| `POST` | `/api/burn/caption-render/v1` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/burn/overlay` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/burn/batch-status/{batch_id}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/burn/batches` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `PATCH` | `/api/burn/batches/{batch_id}/rename` | WRITE | Access JWT | yes |  |  |
| `PATCH` | `/api/burn/folders/rename` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/burn/zip/{batch_id}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/projects/` | READ | Access JWT | yes | reconcile_lab_projects.py (launchd daily 07:15) | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/projects/` | WRITE | Access JWT | yes | prompt/batch scripts (prompting-agent, ocean harness, content-distro, telegram-distro) |  |
| `POST` | `/api/projects` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/projects/videos/recent` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/projects/{name}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/projects/{name}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/projects/{name}/stats` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/projects/{name}/videos` | READ | Access JWT |  | prompt-bible scripts | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/projects/import-legacy` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/recreate/generate-prompt` | MONEY | Access JWT | yes |  | OpenAI: routers/recreate.py:293 chat.completions.create |
| `WS` | `/api/recreate/ws/{job_id}` | MONEY | Access JWT | yes |  | Replicate: 'start' -> routers/recreate.py:199/212 remove_text -> providers/replicate.py:550 |
| `GET` | `/api/recreate/jobs` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/recreate/jobs/{job_id}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/clipper/r2/upload-init` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/clipper/r2/upload-complete` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/clipper/upload-stream` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/clipper/stage-streamed` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/clipper/upload` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/clipper/upload-batch` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/clipper/download-url` | WRITE | Access JWT | yes | core-core music editor |  |
| `POST` | `/api/clipper/trim-batch` | WRITE | Access JWT |  | core-core music editor |  |
| `POST` | `/api/clipper/process-batch` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/clipper/process-batch/{job_id}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `WS` | `/api/clipper/ws/{job_id}` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/clipper/jobs` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `PATCH` | `/api/clipper/jobs/{job_id}/rename` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/clipper/jobs/{job_id}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/clipper/jobs/{job_id}/download-all` | WRITE | Access JWT | yes |  | uploads missing clips to R2 (routers/clipper.py:1593); side-effect GET, so Access callers get the CSRF origin check too |
| `GET` | `/api/clipper/cookies/status` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/clipper/cookies` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/clipper/cookies` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/roster/` | PII-READ | Access JWT or Hub x-api-key | yes | Hub Flask+Rust | roster/poster/account data |
| `GET` | `/api/roster/project/{project_name}` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `PUT` | `/api/roster/{integration_id}` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/roster/{integration_id}` | TOKEN | - |  |  | require_roster_auth (CONTROL_PLANE_TOKEN or APP_API_KEY; 503 unset) |
| `GET` | `/api/roster/duplicates` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `POST` | `/api/roster/dedup` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/roster/sync-notion/status` | PUBLIC | - |  |  | last-sync status only |
| `POST` | `/api/roster/sync-notion` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/roster/sync` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/pages/` | PII-READ | Access JWT | yes | reconcile_lab_projects.py (launchd daily 07:15) | roster/poster/account data |
| `GET` | `/api/pages/{page_id}/content` | PII-READ | Access JWT | yes | deliver_lab_clips_to_vault.sh | roster/poster/account data |
| `POST` | `/api/pages/{page_id}/project` | WRITE | Access JWT | yes | reconcile_lab_projects.py (launchd daily 07:15) |  |
| `POST` | `/api/pages/{page_id}/clips/move` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/slideshow/upload` | WRITE | Access JWT | yes | slideshow-agent |  |
| `GET` | `/api/slideshow/images` | READ | Access JWT | yes | slideshow-agent | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/slideshow/images/{filename}` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/slideshow/images` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/slideshow/render` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/slideshow/job/{job_id}` | READ | Access JWT | yes | slideshow-agent | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/slideshow/renders` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/slideshow/renders/{filename}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/slideshow/audio/upload` | WRITE | Access JWT | yes | slideshow-agent |  |
| `GET` | `/api/slideshow/audio` | READ | Access JWT | yes | slideshow-agent | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/slideshow/audio/{filename}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/slideshow/render-v2` | WRITE | Access JWT | yes | slideshow-agent |  |
| `GET` | `/api/slideshow/project-videos` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/slideshow/captions` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/slideshow/sounds/prepare` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/slideshow/sounds/{sound_id}/audio` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/slideshow/render-meme` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/slideshow/formats` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/slideshow/formats` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/slideshow/formats/{name}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `DELETE` | `/api/slideshow/formats/{name}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/slideshow/broll-montage/spec` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/slideshow/broll-montage` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/telegram/status` | PII-READ | Access JWT or Hub x-api-key | yes | Hub Flask+Rust | roster/poster/account data |
| `PUT` | `/api/telegram/bot-token` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/telegram/bot-token` | WRITE | Access JWT | yes |  |  |
| `PUT` | `/api/telegram/staging-group` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/staging-group` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `POST` | `/api/telegram/staging-group/sync-topics` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/staging-group/scan-inventory` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/staging-group/scan-inventory` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `POST` | `/api/telegram/staging-group/scan-inventory/{integration_id}` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/staging-group/discover-topics` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/staging-group/discover-topics` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `DELETE` | `/api/telegram/staging-group/topics/{integration_id}` | WRITE | Access JWT |  |  |  |
| `PUT` | `/api/telegram/staging-group/topics` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/telegram/posters` | PII-READ | Access JWT or Hub x-api-key | yes | Hub Flask+Rust | roster/poster/account data |
| `POST` | `/api/telegram/posters` | WRITE | Access JWT | yes |  |  |
| `PUT` | `/api/telegram/posters/{poster_id}` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/telegram/posters/{poster_id}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/posters/{poster_id}/users` | WRITE | Access JWT |  |  |  |
| `DELETE` | `/api/telegram/posters/{poster_id}/users/{user_id}` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/posters/reset-defaults` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/posters/{poster_id}/pages` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/telegram/posters/{poster_id}/pages/{integration_id}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/posters/{poster_id}/sync-topics` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/posters/{poster_id}/discover-topics` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/telegram/posters/{poster_id}/discover-topics` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `POST` | `/api/telegram/send` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/send-batch` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/assign-batch` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/forward/{integration_id}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/inventory/{integration_id}` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `GET` | `/api/telegram/inventory` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `DELETE` | `/api/telegram/inventory/scan` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/log` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `GET` | `/api/telegram/sounds` | READ | Access JWT or Hub x-api-key | yes | Hub Flask+Rust | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/telegram/sounds` | WRITE | Access JWT | yes |  |  |
| `DELETE` | `/api/telegram/sounds/all` | WRITE | Access JWT |  |  |  |
| `DELETE` | `/api/telegram/sounds/{sound_id}` | WRITE | Access JWT | yes |  |  |
| `PUT` | `/api/telegram/sounds/{sound_id}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/sounds/sync-notion` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/sounds/sync-hub` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/telegram/sounds/sync` | WRITE | Access JWT or Hub x-api-key | yes | Hub Flask+Rust |  |
| `POST` | `/api/telegram/sounds/forward/{poster_id}` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/sounds/forward-all` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/schedule` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `PUT` | `/api/telegram/schedule` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/telegram/batch/run` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/telegram/pages/{integration_id}/playlist` | PII-READ | Access JWT or Hub x-api-key |  | Hub Flask+Rust | roster/poster/account data |
| `PUT` | `/api/telegram/pages/{integration_id}/playlist` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask+Rust |  |
| `POST` | `/api/telegram/pages/{integration_id}/playlist/songs` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask+Rust |  |
| `DELETE` | `/api/telegram/pages/{integration_id}/playlist/songs/{sound_id}` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask+Rust |  |
| `DELETE` | `/api/telegram/pages/{integration_id}/playlist` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask+Rust |  |
| `GET` | `/api/telegram/playlists` | PII-READ | Access JWT or Hub x-api-key |  | Hub Flask+Rust | roster/poster/account data |
| `GET` | `/api/telegram/posters/{poster_id}/preview` | PII-READ | Access JWT or Hub x-api-key |  | Hub Flask+Rust | roster/poster/account data |
| `POST` | `/api/telegram/sound-assignments/send/{poster_id}` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask+Rust |  |
| `POST` | `/api/telegram/sound-assignments/send-all` | WRITE | Access JWT or Hub x-api-key |  | Hub Flask |  |
| `GET` | `/api/miniapp/me` | HMAC | - | yes |  | Telegram initData HMAC (services/miniapp_auth.py) |
| `GET` | `/api/miniapp/videos` | HMAC | - | yes |  | Telegram initData HMAC (services/miniapp_auth.py) |
| `GET` | `/api/miniapp/requests` | HMAC | - | yes |  | Telegram initData HMAC (services/miniapp_auth.py) |
| `POST` | `/api/miniapp/requests` | HMAC | - | yes |  | Telegram initData HMAC (services/miniapp_auth.py) |
| `GET` | `/api/miniapp/agent/requests` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (X-Agent-Key); 503 when unset |
| `PATCH` | `/api/miniapp/agent/requests/{request_id}` | TOKEN | - |  |  | MINIAPP_AGENT_KEY (X-Agent-Key); 503 when unset |
| `GET` | `/api/email/status` | PUBLIC | - | yes |  | configured flag + mail domain only |
| `DELETE` | `/api/email/rules/{rule_id}` | WRITE | Access JWT or Worker bearer | yes |  | deletes a CF rule; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4) |
| `POST` | `/api/email/auto-create` | WRITE | Access JWT or Worker bearer | yes |  | creates a CF rule; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4) |
| `GET` | `/api/email/destinations` | PII-READ | Access JWT or Worker bearer | yes |  | team inboxes; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4) |
| `POST` | `/api/email/destinations` | WRITE | Access JWT or Worker bearer | yes |  | adds a CF destination; CONTROL_PLANE_TOKEN (Bearer or X-API-Key) or Access (lead decision 4) |
| `POST` | `/api/pipeline/mint-alias` | WRITE | Access JWT | yes |  | mints a CF alias + Notion placeholder; operator-only (lead decision 5) |
| `POST` | `/api/pipeline/intake` | WRITE | Access JWT | yes |  | Notion + roster writes; operator-only (lead decision 5); the #177 tamper guard stays |
| `GET` | `/api/pipeline/stages` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `POST` | `/api/pipeline/{integration_id}/setup` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/pipeline/{integration_id}/transition` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/pipeline/{integration_id}/workspace` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `POST` | `/api/pipeline/{integration_id}/upload-presign` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/pipeline/{integration_id}/forward-to-topic` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/pipeline/{integration_id}/health` | PUBLIC | - |  |  | setup checks: booleans, R2 object count, cookie status, Telegram topic name; no credentials (triggers one R2 list) |
| `POST` | `/api/upload/submit` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/upload/jobs` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `GET` | `/api/upload/jobs/{job_id}` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `POST` | `/api/upload/jobs/{job_id}/cancel` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/upload/cookies` | PII-READ | Access JWT | yes |  | roster/poster/account data |
| `GET` | `/api/upload/cookies/{account_name}` | PII-READ | Access JWT |  |  | roster/poster/account data |
| `POST` | `/api/upload/login/{account_name}` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/upload/stats` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/stream` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/factory/state` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/factory/pause` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/factory/resume` | MONEY | Access JWT |  |  | resumes the autonomous factory: Codex image_gen services/abn_factory.py:112 |
| `POST` | `/api/agenticnews/tools/approve` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/tools/reject` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/agenticnews/videos` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/videos` | WRITE | Access JWT |  |  |  |
| `PATCH` | `/api/agenticnews/videos/{vid}` | WRITE | Access JWT |  |  |  |
| `DELETE` | `/api/agenticnews/videos/{vid}` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/videos/{vid}/move` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/agenticnews/jobs` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/jobs` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/jobs/{jid}/claim` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/jobs/{jid}/complete` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/jobs/{jid}/fail` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/tools/tts` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/tools/cards` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/tools/assemble` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/tools/scrape` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/agenticnews/episodes/{ep_id}/qa` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/patterns` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/episodes/{ep_id}/publish-package` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/episodes/{ep_id}/publish` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/gc` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/agenticnews/memory` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/workshop` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/scratch-metrics` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/stats` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/editor-timelines` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/editor-timelines/{project_id}/import-abn` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/agenticnews/editor-timelines/{project_id}` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/editor-timelines/{project_id}/asset-health` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/editor-timelines/{project_id}/commands` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/agenticnews/editor-timelines/{project_id}/commands/revert-last` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/agenticnews/editor-timelines/{project_id}/openshot` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/agenticnews/editor-render/capabilities` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/editor-render/{project_id}/render` | WRITE | Access JWT | yes |  |  |
| `POST` | `/api/agenticnews/editor-render/{project_id}/frame` | WRITE | Access JWT | yes |  |  |
| `GET` | `/api/agenticnews/editor/{ep_id}` | READ | Access JWT |  |  | operator read, no PII/spend (gated with its router) |
| `POST` | `/api/agenticnews/editor/{ep_id}/notes` | WRITE | Access JWT |  |  |  |
| `DELETE` | `/api/agenticnews/editor/{ep_id}/notes/{note_id}` | WRITE | Access JWT |  |  |  |
| `POST` | `/api/agenticnews/editor/{ep_id}/apply` | WRITE | Access JWT |  |  |  |
| `GET` | `/api/projects` | READ | Access JWT | yes |  | operator read, no PII/spend (gated with its router) |
| `GET` | `/api/health` | PUBLIC | - | yes | slideshow-agent | liveness; presence flags only (no values) |
| `MOUNT` | `/fonts` | PUBLIC | - |  | Worker, slideshow-agent | font files; read by the Worker font proxy and slideshow-agent |
| `MOUNT` | `/agenticnews-assets` | PUBLIC | - |  |  | rendered ABN media (public by design, WATCH) |
| `GET` | `/workspace` | PUBLIC | - |  |  | static HTML shell; its APIs are gated |
| `GET` | `/factory` | PUBLIC | - |  |  | static HTML shell; its APIs are gated |
| `GET` | `/abn-editor/{ep_id}` | PUBLIC | - |  |  | static HTML shell; its APIs are gated |
| `GET` | `/abn-editor` | PUBLIC | - |  |  | static HTML shell; its APIs are gated |
| `MOUNT` | `/projects` | PUBLIC | - |  | deliver_lab_clips_to_vault.sh, prompt/batch scripts (prompting-agent, ocean harness, content-distro, telegram-distro), slideshow-agent | #172 allowlist: <project>/<media kind>/.../<media file> only |
| `MOUNT` | `/output` | PUBLIC | - |  |  | generated media, public by design (WATCH) |
| `MOUNT` | `/caption-output` | PUBLIC | - |  |  | generated media, public by design (WATCH) |
| `MOUNT` | `/burn-output` | PUBLIC | - |  |  | generated media, public by design (WATCH) |
| `GET` | `/font-preview` | PUBLIC | - |  |  | static HTML page |
| `GET` | `/{full_path:path}` | PUBLIC | - |  |  | SPA fallback; only files inside frontend/dist (traversal-guarded) |
