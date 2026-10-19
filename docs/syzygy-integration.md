# Syzygy integration

## Ownership and reuse

Syzygy is the creation studio and rendering service. Content Lab already has
the page-bound slideshow adapter in `services/control_plane_slideshows.py`,
the executor contract in `recipes/executors/syzygy-slideshow.v1.json`, and the
runner in `routers/control_plane.py`. Extend those seams before adding another
client, renderer, job store, or creation screen.

Control Plane owns page intent, admission and replenishment. ShipStream owns
delivery. Northstar owns its authenticated tenant Library, curation, approvals
and distribution. A render receipt must not itself grant admission, content
approval, scheduling or publishing authority in either consumer.

Keep the integration narrow: create in Syzygy, transfer verified output through
the consumer's existing media boundary, then use that consumer's review and
distribution flow. Reuse available controls; do not add a parallel dashboard,
provider account manager, scheduler or caption-style vocabulary.

## Verified source baseline

Inspected 2026-10-19; refresh these refs and open PR ownership before editing.
These are source observations, not deployment acceptance.

| Repository | Commit | Existing seam |
| --- | --- | --- |
| `KINGMAKER-SYSTEMS/syzygy` | `c7dd0fea35665706396e6ca18aa6430a72591d27` | `docs/AGENTIC-USE.md`, `/api/v1/render`, owner-scoped job reads and render downloads |
| `KINGMAKER-SYSTEMS/content-posting-lab` | `50a4fe4` | Stable Syzygy library selection, deterministic plans, idempotent render submit, bounded polling, source observations and slideshow receipts |
| `KINGMAKER-SYSTEMS/rotational-posting-agent` | `b0cc66cc9d15aa4ffc91c0ebd97a75015f221735` | `control-plane-worker/src/adapters/contentLabClient.js` validates incoming artifacts |
| `KINGMAKER-SYSTEMS/north-star-artists-dashboard` | `1a5d069a` | `hosted/src/http/media.rs` owns upload completion; `hosted/src/http/distro.rs` admits Ready Library clips into existing review; editorial rendering is a separate source/occurrence-bound flow |

The public Syzygy `/readyz` returned `ok: true` and the full 40-character commit
above. This proves that health response only; no authenticated render or transfer
was executed. Its revision is a Git object ID, not a media SHA-256. Content Lab
must preserve full 40- or 64-character lowercase revisions while retaining exact
64-character media and recipe checksums.

`test_runner_uses_real_syzygy_http_adapter` exercises the existing Content Lab
runner through its actual revision, render-submit, polling, download and
source-observation adapters. Its offline HTTP responses follow the Syzygy
`render.rs` public job/result shapes at the baseline above. It checks one render
submission, source hashes before/after, retained job/revision provenance and
that the Syzygy credential is not sent to source storage. Synthetic output and
stubbed local encoding keep this a contract test, not live render acceptance.

## Next integration work

1. **Control Plane artifact contract.** At the main baseline, `validateArtifactSource`
   dispatches any `sourceLibraryId` to source-DNA validation, whose exact field
   set is incompatible with Content Lab's
   `content-lab.syzygy-slideshow-source.v1`. There is no Syzygy-specific branch.
   [PR #1113](https://github.com/KINGMAKER-SYSTEMS/rotational-posting-agent/pull/1113)
   at `7152217cde55c2e9751a3d92ffb9cebea6dedfdd` adds that recognition. Its
   producer fixture was captured from the actual offline Content Lab runner
   and HTTP serializer with synthetic media and complete caption context.
   142 adapter tests and eight replenishment cases passed: both engines reach
   inventory and retain the existing page, recipe and byte checks. Full local
   `TZ=UTC npm test` exited 0: 10,948 passed, 9 skipped and one existing TODO.
   The PR merged as `3669bd27a71eb7d1311a9e8f27c1fe8f00ed42b9`; deployment and
   live render-to-inventory acceptance remain unverified. Release both the
   producer revision fix and consumer recognition before live acceptance.
2. **Northstar output intake.** No `syzygy` references were found in tracked
   Northstar main at the baseline. First prove importing a completed render
   through its existing tenant-owned upload flow. Normal uploaded finished clips
   already enter rotation through `POST /t/{tenant}/distro/rotation` with
   `action=add`, a media UUID, expected revision and tenant/session-bound CSRF.
   `distro_library_intake` verifies owned Ready media, immutable R2 version and
   SHA-256, then creates the content version for existing per-destination review.
   It neither requires nor creates an original-source registration. Intake does
   not approve publication or change the rotation's pause state. Never infer a
   Northstar tenant from a Syzygy artist label, page name or shared key. Keep
   private bytes, checksum verification, retry identity and review authority
   in the existing media contract. Northstar's editorial renderer already
   performs distribution preparation; do not replace or duplicate it merely
   to introduce Syzygy. `docs/R2-UPLOAD-SOURCE-PROVENANCE.md` explicitly excludes
   generated identities and generated-byte reuploads from original-source
   registration. A Syzygy render is a completed creation, not original footage:
   preserve its origin and render identity rather than using
   `library_register_r2_upload` to relabel it. The existing browser media routes
   derive authority from the authenticated tenant/session and CSRF flow;
   a direct Studio handoff still needs an explicit account-linking flow.
   A shared credential or upload URL alone is not tenant authority. Automatic
   receipt association is a separate enhancement: the ordinary media schema has
   no generic creation receipt field. Retain Syzygy job/revision/output evidence
   separately rather than encoding provenance in filenames or pretending it is
   original footage.
   `hosted/src/operator_media_client.rs::OperatorMediaUploader::upload_session`
   already implements hashed multipart transfer into private R2. Reuse it if
   operator intake is chosen, but do not claim it is an available importer:
   `PROGRESS-OPERATOR-MEDIA.md` and `scripts/reachability-allow.txt` record its
   absent execute caller, and the required `distro_import_media_edge` SQL
   authority is absent from current migrations. Existing editorial generated
   media is reserved against a source, recipe, schedule and occurrence;
   `editorial_finish_render` requires that reservation and a live lease, and
   `editorial_guard_content` prevents using it as a rotation original.
   Neither unfinished operator import nor editorial reservation is a prerequisite
   for the existing user-driven upload/review journey. Do not manufacture an
   editorial occurrence or add a new admission system to fit an external render.
   `hosted/tests/ui-product-walk.cjs` already uploads a finished synthetic MP4,
   verifies private playback, adds it to rotation and saves review content without
   source registration. Reuse this proof before adding another fixture. Check
   overlapping original/style PR #471 and large-media transport PR #459 before editing.
3. **Acceptance.** Test owner isolation, interrupted transfer, exact-byte
   provenance, duplicate retries, deleted source and downstream refusal before
   a separately authorized real render/import. Record local, PR/CI, merged,
   deployed and live-verified results separately. No posting is needed to prove
   the initial creation-to-Library handoff.

Revisit this order against current evidence rather than expanding the feature
list. Prefer removing a redundant step or repairing a contract over adding UI.
