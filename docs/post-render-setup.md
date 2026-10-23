# Prepared rendering setup

The hosted `python main.py` service owns durable final-render jobs. It fetches
an already-approved source from the Worker's pending-artifact grant, renders the
selected caption once, and serves hash-bound final/QA/receipt files. It neither
claims a phone nor starts posting.

Configure these values only as part of an authorized service release:

| Setting | Purpose |
| --- | --- |
| `CONTENT_LAB_POST_RENDER_ROOT` | Absolute private directory on persistent storage; for the existing Railway `/app/projects` volume, use a dedicated directory such as `/app/projects/_post_render`. Blank leaves the preparation workers stopped. |
| `CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN` | `https://content-buckets.risingtidesviral.com`, the machine ingress for `/api/control-plane/v1/service/posting/v1/artifacts/.../source`. Required; no browser-origin fallback. |
| `CONTENT_LAB_POST_RENDER_WORKERS` | Concurrent render workers (threads and lock permits). Optional; default `2`, clamped to `1..4`. An unset, blank, non-integer, or out-of-range value falls back to `2` with a logged warning. Each worker can run a concurrent `ffmpeg` process, so raise this only after confirming the target Railway instance has the CPU headroom for that many concurrent encodes. |
| `CONTENT_LAB_POST_RENDER_RETIRE_AFTER_MS` | Safety window before acknowledged successful media is retired. Optional; default 24 hours. The verified receipt/final/QA hash authority remains in SQLite. |
| `CONTENT_LAB_POST_RENDER_MIN_FREE_BYTES` | Free bytes that must remain after reserved render workspace. Optional; default 1 GiB. Admission additionally reserves 1,165,317,468 bytes per configured worker; unreadable filesystem capacity fails closed with `post_render_free_space_unavailable`. |
| `CONTROL_PLANE_SERVICE_ID` | Existing Worker service identity, currently `gutenberg-shipstream`; it must retain the relevant scheduler lane and page grants. |
| `CONTROL_PLANE_SERVICE_SECRET` | The same machine credential held by the Worker. Provision it through the service secret store; never put its value in source, examples or logs. |
| `CONTROL_PLANE_TOKEN` | Existing inbound Lab bearer, matching Worker's `CONTENT_LAB_TOKEN`. Every render endpoint also requires the exact `X-RT-Page-Id`. |

Keep `CONTENT_LAB_CONTROL_PLANE_ORIGIN=https://control.risingtidesviral.com`
for existing `/api/control-plane/v1/pages/...` source-media reads. Those routes
use the browser ingress. Repointing this shared setting to the machine host
would break existing source generation. If the Lab's optional `APP_API_KEY`
middleware is enabled separately, the Worker also needs the matching
`CONTENT_LAB_API_KEY` for its `X-Api-Key` header.

The render root holds SQLite WAL state, locks, attempts and final artifacts.
Use one persistent volume with ordinary file-lock semantics; do not put the
root in the image's temporary filesystem or share it through storage lacking
those semantics. The service creates private subdirectories and starts two
bounded workers. Restart preserves the same remote job and recovers completed
outputs before rendering again.

Retention never deletes no-repeat authority: every `jobs` row (request/slot
binding, counters, and receipt/final/QA hashes) and every `idempotency` row is
permanent. Failed or interrupted attempt media is diagnostic and is removed
after 24 hours. Non-authoritative `attempts` and `provenance_updates` history is
removed after 30 days, after which the service checkpoints the SQLite WAL.

The per-worker 1,165,317,468-byte peak reserve is derived from two simultaneous
512 MiB source files (the authenticated download and renderer verification
copy), a bounded 16 MiB overlay, its base64-bearing caption JSON, two 22 MiB
finals, two 2 MiB QA frames (the worst-case in-app attempt), and bounded request/receipt/decode metadata. A retry
removes its first final before writing another. The claim gate reserves that
peak for every configured worker in addition to the retained free-space floor;
it does not start work when filesystem free space cannot be measured.

The production Railway target is project `f512351a-26a8-4c33-98ef-eddd2f48c11e`,
environment `99ff0f13-254f-4bf2-960a-134f4f2e0e0f`, service
`a6dcd562-df4c-4ae1-a4e6-414f0d61b74c`. Use these explicit selectors when
inspecting release state; a checkout's default Railway link may name another
project. The application path is `/app`, its start command is `python main.py`,
and the persistent volume is mounted at `/app/projects`.

Before connecting a canary, integrate and validate current Lab and Worker
source, verify the actual running Lab revision and mounted root, and apply the
separately authorized Worker migration/configuration release. Worker preparation
remains off until explicitly enabled. A healthy `/api/health` response alone
does not establish that the render router, source grant or durable workers are
available. Verify authenticated page-bound status and an authorized preparation
job through actual final-byte admission before allowing phone execution.

Historical assets without genuine applied-video treatment evidence still need
regeneration. New generation emits that evidence; final rendering compares the
video treatment and adds only the caption and delivery encode. Neither setup
nor a successful render invents provenance for old bytes or authorizes a post.

Offline verification:

```sh
python3 -m pytest -q tests/test_post_render_jobs.py tests/test_post_render.py tests/test_source_treatment.py tests/test_control_plane_dossier_execution.py tests/test_control_plane_source_execution.py
```

## In-app dual assets (candidate; activation remains gated)

A prepared v1 request with `captionDelivery: "in_app"` produces a clean primary
`final`/`qa` and a captioned `burned_final`/`burned_qa` before success. Both use
the identical source, caption and requested treatment binding; the fallback
does not repeat source grade, speed or crop. The existing receipt adds
`caption_delivery: "in_app"` and `burned_fallback`, containing `final_sha256`,
`final_byte_length`, `final_object_key` and `qa_frame_sha256`. The fallback uses
the separate `posting/dual-final/<sha>.mp4` content-addressed object key;
Worker fallback QA storage uses `posting/dual-qa/<sha>`. These namespaces
avoid existing primary-only retention deleting a live fallback when its bytes
match a historical primary. Fallback objects accumulate until a separate
migration-backed retention lifecycle is implemented; storage estimates below
are per slot, not a bounded steady-state total. Artifact
downloads retain the existing bearer and exact page boundary.

Previously succeeded clean-only in-app jobs keep their original contract on
upgrade. Compatibility requires an already-succeeded row, all four persisted
receipt/final/QA/length pins, and exact agreement with its retained clean-only
authority. Status advertises only final, QA and receipt; download and ACK still
verify their immutable bindings. Ordinary exact-final admission confirmation
allows age-out, and retained authority remains usable after retirement. No
burned fallback is invented or promised for these historical bytes. A new or
unfinished job cannot use this exception; any fallback key, including a null
or malformed promise, requires the complete verified pair. Unverifiable jobs
keep their media and fail closed through the existing verification errors.

Succeeded status and idempotent replay refuse missing retained authority,
invalid JSON/non-object roots, and wrong numeric/object types at the retained
completion-time, duration, decoded-frame-count and clean/burned decode-probe
checks through the existing HTTP 409 error contract. These refusals preserve
rows and media and never schedule a replacement render. This is bounded type
validation of retained authority, not comprehensive storage-corruption recovery. This does not authenticate retained receipt contents against the exact
receipt-byte pin. Conditional private-storage edits that remove both fallback
members after retirement can still misclassify a pair as legacy and lose its
burned dedup identity. See [the bounded repair disposition](post-render-authority-repair.md).
This case remains open; the four pins must not be described as detecting it.

The whole attempt fails if either render fails. Existing bounded job retries
handle transient process failures before readiness. Durable verification checks
both variants against their media hashes and decode evidence; partial or
changed fallback bytes cannot be downloaded as a ready artifact. Timed lyrics
continue to refuse in-app delivery. Downstream admission, per-page breaker and
rail selection are separate consumers; this candidate does not authorize an
activation or a database-authority bypass.

Measured on maj/Python 3.11 with one seven-second synthetic 1080x1920/30fps
source: normal burned render 3.634 seconds and 8,504,503 retained bytes; dual
render 7.195 seconds and 17,297,216 bytes. Receipt size grows from 1,000 to
1,352 bytes. Thus M opted-in slots add about M × 3.561 elapsed render seconds and
M × 8.39 MiB retained storage in this synthetic sample; actual content, host
load and bitrate change these measurements. Both final variants retain their
individual 22 MiB caps and both QA images their 2 MiB caps. No paid provider
call is added. Claims carry asset metadata rather than media bytes.


Dual jobs deduplicate the captioned fallback hash, so the same clean source can
produce distinct captions without `duplicate_output`. Ordinary burned jobs and
identical burned fallbacks still share one no-repeat identity. The reservation
check and result write run under one SQLite `BEGIN IMMEDIATE` writer transaction.
No schema change is needed: the first owner retains the UNIQUE primary hash
column; later verified pairs sharing that clean hash use the existing nullable
column convention and retain their actual clean hash in full output authority.
Status reads that verified authority for shared jobs, including after restart
and media retirement; no column is repurposed to hold a different file's hash.

Shared clean bytes also change automatic retention proof. A dual job is
automatically retired only when its own `/acknowledge` records that both variants were admitted and the applicable
existing age/planned-slot window has passed. That window need not start after
acknowledgement. A primary-only presence check cannot retire another caption's
unacknowledged fallback. The Worker must acknowledge each admitted
pair; otherwise its Lab media remains retained. Ordinary rendered jobs keep the
existing primary-final confirmation sweep.

The no-schema burned-identity lookup scans retained local job authority. On maj,
with 2,547-byte authority fixtures, ten samples at 10,000 historical jobs measured
26.55 ms median / 29.24 ms maximum per writer reservation; 20,000 jobs measured
54.17 ms median / 56.04 ms maximum. This cost grows with retained history; it is
not a constant-time index claim. The lookup runs when output authority is saved in either mode, because ordinary
outputs must also avoid an already-bound burned fallback. It adds no phone-drive
work or paid call.


Output identity lookup uses the existing `idempotency` primary-key index. Internal
keys start with `/output-identity/v1/`, outside the public key alphabet, and hold
one permanent captioned-output reservation per distinct hash. This does not
change the meaning of `jobs.final_sha256`, the request keys, or the schema.
Finish, ACK and retire use indexed probes under the same writer transaction as
the authority write. They never parse the whole job history.

Before serving work, each service start streams the retained authority history
once under a writer lock to recover old ordinary and dual reservations, including
retired shared-clean rows. Invalid JSON or an unavailable identity is logged with
the job ID and skipped; known column hashes and prior reservations remain in
force. This isolates corruption; it does not recover an unknown lost hash.
Status omits unavailable legacy/shared hashes. Artifact reads still validate
output authority and do not serve corrupt bytes.

Stop the old runtime before opening this version on the same SQLite database.
Concurrent instances of this version are safe; mixing 248 writers with these
writers is unsupported because 248 does not maintain the private reservations.
A rollback reads the unchanged columns and authority. Starting this version again
always repeats the history pass to pick up writes made during the rollback.
If backfill fails for a database/I/O reason, startup refuses work; restore the
prior release and retain the database for repair. It must not silently run with
an incomplete identity index. Private reservations are retained with request
keys and tombstones, including when media or auxiliary attempts are pruned.

The shared disk admission floor is 1,165,317,468 bytes per worker for all jobs;
that includes the dual workspace reserve even for ordinary jobs. This follow-up
changes status recovery and identity lookup, and the renderer docstring, while
leaving rendering instructions and artifact contents unchanged.
