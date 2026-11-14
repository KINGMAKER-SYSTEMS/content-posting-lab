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
- Refused with 409: an id held by a Notion, legacy or pipeline row, a handle
  any other roster row holds (case-insensitive), or a Syzygy slideshow library
  subject any other roster page already derives (the detail names the subject
  and the page; see below).

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

The row appears in operator roster listings. Generic name-based roster dedup
excludes externally owned rows: it cannot remove one or transfer its inventory
into a Notion/provider row. The existing operator project-assignment route can
still change a row's project, which changes its intent hash.

## Syzygy slideshow library subject

The canonical external subject is `external--<sha256(exact pageId UTF-8)>`.
It is independent of handle, and hashes the exact bounded page token rather
than a casefolded slug. Case/punctuation-distinct page ids remain separate.
The ordinary Notion/legacy/pipeline slugger is unchanged: it collapses each
punctuation run to one hyphen. It can never produce the external double-hyphen
prefix, even if a matching Notion handle appears after registration.

Both ingredients and execution use `library_subject` through
`slideshow_library_address`. The resulting address is
`syzygy:<lane>:<reserved subject>`, with objects at
`<lane>/<reserved subject>/photos/<file>` or `clips/<file>`.
`meme-slideshow` uses lane `meme`; `lyric-edits` uses
`slideshow-webweaving`. The writer must use the advertised ingredient binding
subject/libraryId; it must not reconstruct paths from handle or page-id slug.
The recipe's sourceLibraryId and catalogVersion must exactly select that
advertised option. Existing Rising Tides library addresses remain byte-identical.

This reserved address corrects the unlanded #264 proposal, which let a later
Notion handle collide with an external page-id slug. Matching Syzygy uploader
support and actual cross-service activation have not been verified; do not
activate North Star slideshow production until writer/reader address agreement
is qualified. No existing namespace is migrated or aliased by this code.

## Sourced video for an external page

Registering a page makes it resolvable. Cutting its footage also needs the
vault's `source-manifest.json` to bind to the page without a Notion page id.
`services/shipstream_source_manifest.py` accepts a null `notionPageId` only for
a registered external page, and only through that page's own Content Lab
import. For such a page ShipStream must write:

- `notion.pageId: null`, the key present and explicitly null, beside the same
  `contentNiche`, `contentEngine` and `serviceMode` the intent carries;
- `sourceAuthority.kind: "content_lab_page_source_import"`;
- `sourceAuthority.pageId` equal to the operational page id (the registered
  `pageId`);
- `sourceAuthority.pageHandle` equal to the page handle;
- `sourceAuthority.notionPageId: null`, the key present and explicitly null;
- `sourceAuthority.intentSource: "northstar"`, repeating the intent's `source`
  (Rising Tides manifests carry no `intentSource`; one that does is refused);
- `sourceAuthority.replacementEligible: true`;
- a `master` block, as for any Content Lab import.

Everything else is refused exactly as before: a blank string, an omitted key,
a missing or different `intentSource`, a Notion-source page missing its id, a
null-source row, another authority kind (historical-posted-cut recovery
included), or an authority naming another page id.

ShipStream's side is Risingtides-dev/shipstream PR 54, which writes exactly
these fields (plus `assignment.notionPageId: null`, which Content Lab does not
read) for a `source: "northstar"` registration.

Tests: `pytest -q tests/test_control_plane_external_pages.py
tests/test_shipstream_source_manifest.py`.
Tests: `pytest -q tests/test_control_plane_external_pages.py` and, for the
page-owned library subject, `pytest -q tests/test_northstar_slideshow_library.py`.
