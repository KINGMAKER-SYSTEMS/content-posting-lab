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
| `KINGMAKER-SYSTEMS/north-star-artists-dashboard` | `a8911516` | `hosted/src/http/media.rs` owns upload completion; `hosted/editorial-renderer/SERVICE.md` and `docs/SYSTEM-MAP.md` describe existing editorial preparation and its separate acceptance gates |

The public Syzygy `/readyz` returned `ok: true` and the full 40-character commit
above. This proves that health response only; no authenticated render or transfer
was executed. Its revision is a Git object ID, not a media SHA-256. Content Lab
must preserve full 40- or 64-character lowercase revisions while retaining exact
64-character media and recipe checksums.

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
   Worker verification is pending; the PR is draft, unmerged and undeployed.
   Finish that gate and release before claiming live end-to-end delivery.
2. **Northstar output intake.** No `syzygy` references were found in tracked
   Northstar main at the baseline. First prove importing a completed render
   through its existing tenant-owned upload flow; determine the smallest
   server-side adapter only after checking active Distro work. Never infer a
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
   server-to-server import identity and provenance retention still need an
   explicit integration design. A shared credential or upload URL alone is
   not that design.
3. **Acceptance.** Test owner isolation, interrupted transfer, exact-byte
   provenance, duplicate retries, deleted source and downstream refusal before
   a separately authorized real render/import. Record local, PR/CI, merged,
   deployed and live-verified results separately. No posting is needed to prove
   the initial creation-to-Library handoff.

Revisit this order against current evidence rather than expanding the feature
list. Prefer removing a redundant step or repairing a contract over adding UI.
