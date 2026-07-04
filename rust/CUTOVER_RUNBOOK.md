# Wave 4 — Cutover runbook (Rust rewrite replaces Python)

The Rust binary is at functional parity and structurally verified against live
Python. This runbook takes it live with a shadow run first and a clean rollback.
**The prod flip (Phase 4) is a go/no-go decision — that's yours, John.**

## Pre-flight — DONE (verified autonomously)

- Backend feature-complete against the frozen contract; **275 tests green, clippy clean**.
- `contracttest single` vs the assembled binary: 24 OK / 0 missing / 0 shape-mismatch.
- `contracttest diff` vs **live Python prod**: every frontend-consumed GET matches
  structurally. The 6 "mismatches" are all (a) empty-local-vs-populated-prod data
  noise (playlists, staging_group, cookies, CF domain) that vanish once both run
  the same migrated data, or (b) `/api/health` extra diagnostic fields the frontend
  never reads (it only uses `ffmpeg`/`ytdlp`, which Rust returns). **No real gaps.**
- `clab migrate` validated end-to-end (wave 0 on the real config: 7 posters / 66
  sounds; re-checked post-wave-3, import code unchanged). Idempotent, and
  **read-only on the JSON files** — it never modifies/deletes them.
- Deploy artifacts: `Dockerfile.rust`, `.dockerignore` (excludes rust/target),
  `railway.rust.toml`. Release binary compiles.

## Key safety properties (why rollback is clean)

- **ABN factory stays Python** the whole time — the Rust app reverse-proxies
  `/api/agenticnews/*` + `/api/pipeline/*` to it via `ABN_UPSTREAM_URL`. Never ported.
- **migrate only READS the JSON files.** Python's state (telegram_config.json etc.)
  is untouched, so Python can always resume from it.
- **Shadow uses a separate volume** until the flip → zero risk to prod data while testing.
- The one-way caveat: once Rust is live, NEW state accrues in **SQLite**, not the
  JSON. A rollback after that window loses changes made during it — so flip during
  low activity and watch the first hour (see Phase 5).

## Phase 1 — Shadow deploy (non-destructive)

1. New Railway service on the **`rust-rewrite`** branch, same project as prod.
2. Build → Dockerfile: `Dockerfile.rust`. Health check: `/api/health`.
3. Attach a **NEW, separate volume** at `/app/projects` (NOT the prod volume yet).
4. Env vars: copy ALL of prod's secrets, PLUS `ABN_UPSTREAM_URL` = the current
   Python service's base URL (so ABN keeps working). See `railway.rust.toml` for the list.
5. Deploy. Grab the shadow URL (e.g. `…-rust.up.railway.app`).
6. Smoke: `GET /api/health` → `{"status":"ok"|"degraded","ffmpeg":true,"ytdlp":true}`.

## Phase 2 — Seed the shadow with prod state, then diff

1. Copy prod's `telegram_config.json`, `page_roster.json`, `content_requests.json`
   from the prod volume onto the shadow volume (Railway shell / `railway run`, or
   scp via a one-off). Do NOT copy the Python process — just the JSON.
2. On the shadow: `content-lab migrate /app/projects` (idempotent). Check the report
   — poster/sound/page counts should match prod.
3. Re-run the diff now that both sides share data — should be clean apart from the
   known unconsumed `/api/health` fields:
   ```
   cargo run -p contracttest -- diff \
     --rust   https://<shadow>.up.railway.app \
     --python https://risingtides-content-lab-production.up.railway.app
   ```
4. Manually smoke the real flows on the shadow UI: dashboard loads, a generate job,
   a clip, a burn, Telegram status, Distribution tabs. (Mutations weren't diffed —
   they're covered by tests + shape checks; this is the confidence pass.)

## Phase 3 — Decide (go/no-go)

Green if: diff clean (data-adjusted), smoke flows work, no error-log surprises.
If anything's off, it's caught HERE, on the shadow, with prod untouched.

## Phase 4 — Flip (the cutover) — **YOUR call**

Pick one:
- **A · Repoint the existing service** (keeps the domain): set the prod service's
  build to `Dockerfile.rust` (copy `railway.rust.toml` → `railway.toml`), point it
  at the **prod volume**, run `content-lab migrate /app/projects` ONCE on it, deploy.
  Same URL now serves Rust.
- **B · Domain swap**: move the custom domain from the Python service to the shadow
  service (which now needs the prod volume + migrate run on it). Instant revert by
  swapping back.

Either way: **do NOT delete the Python service** — stop it (or leave it running
for ABN) so rollback is a config revert, not a redeploy.

## Phase 5 — Watch + rollback

- Watch the first hour: Railway logs (tracing), error rate, the real flows.
- **Rollback** (if needed): revert the build to the Python Dockerfile (A) or swap
  the domain back (B). Python resumes from the intact JSON files. Changes made in
  SQLite during the Rust window won't carry back — acceptable if the window is short
  and low-activity, which is why we flip during a quiet period.

## Deferred (post-cutover, non-blocking)

- Full **mutation-endpoint** diff needs a staging env with disposable data — not
  run here (side effects). Covered by wave-1/2/3 shape tests; monitor on the shadow.
- pyrogram topic-scanner + Playwright stealth scraper sidecars (optional Python
  helpers) — wire only if those specific features are needed.
- Cosmetic: `provider_schemas` lists 3 dropped models (dead JSON, never read).
