# External pages (non-Notion Master Pages rows)

Source imports, clip jobs and capabilities resolve a page through
`_current_master_pages_intent` in `routers/control_plane.py`. It needs exactly
one roster row whose `integration_id` is the page id. Rising Tides' own pages
get that row from the Notion Master Pages sync. A page whose intent is owned
elsewhere has no Notion row, so until it is registered here those routes
answer 409 (or an empty capability list) for it.

North Star workspaces are the first such owner. The control plane writes each
workspace page's canonical intent with `source: "northstar"` and
`notionPageId: null` (rotational-posting-agent PR 1165), then registers it here.

## Routes

Both routes use the control-plane machine credential: `Authorization: Bearer
$CONTROL_PLANE_TOKEN` plus `X-RT-Lane: content-bucket-control-plane`.

`PUT /api/control-plane/v1/external-pages/{pageId}`

- The body is the complete `master-pages.page-intent.v1` intent, validated by
  `services/master_pages_contract.py`. Its `pageId` must equal the path.
- `source` must be an external owner (`northstar` today), `notionPageId` must be
  null, and `contentNiche`, `contentEngine`, `automationMode` and `vaultUrl`
  must be set. The handle must be lowercase, because the roster reads handles
  back in TikTok's lowercase spelling.
- The intent is written through the roster store's `set_page`.
- The response gives `status` (`created`, `updated` or `unchanged`), plus the
  `masterPages` intent and `masterPagesHash` that jobs will now resolve. A repeat
  of the same intent writes nothing.
- Refused with 400: an `acct:` page id. Notion sync and pipeline intake mint only
  `acct:<handle>` ids, so keeping external ids out of that namespace means sync
  can never write over an external row.
- Refused with 409: an id held by a Notion, legacy or pipeline row, or a handle
  any other roster row holds (case-insensitive).

`DELETE /api/control-plane/v1/external-pages/{pageId}`

- Removes only a row whose source is an external owner. Notion, legacy and
  pipeline rows answer 409 and stay unchanged.
- Returns `status: "deleted"`, or `status: "absent"` when there was nothing to
  remove, so the caller can retry it safely. Jobs and artifacts already made for
  the page are kept.

## What an external row does and does not touch

- **Notion sync** writes only `acct:` ids and prunes only `source == "notion"`
  rows. It never overwrites or removes an external row.
- **The fleet roster projection** (`GET /v1/roster` and `POST /v1/roster/refresh`)
  publishes only `source == "notion"` rows. Its hash does not change when an
  external page is registered.
- **Source imports, clip jobs and capabilities** resolve the row by exact page id,
  the same way they resolve a Notion row.

The row does still appear in the operator roster listings (`/api/roster`,
`/api/pages`). The existing `PUT /api/roster/{id}` and `POST /api/roster/dedup`
can still change or remove it, just as they can a Notion row.

## Not covered by this route

Registering a page makes it resolvable. It does not make sourced-video
execution work for it:

- `services/shipstream_source_manifest.py` refuses a Master Pages intent with a
  blank `notionPageId`, and requires the vault's `source-manifest.json` to
  repeat it.
- ShipStream's source registration refuses one too.

Both need a non-Notion page-binding rule before a North Star page can cut
footage.

Tests: `pytest -q tests/test_control_plane_external_pages.py`.
