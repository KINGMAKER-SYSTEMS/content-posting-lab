# Engine registry runbook: add an engine, a page, or a look without new code

Written 2026-11-22 (B9 of the build audit: "a new content engine still needs
code in two repos"). After the PRs named at the end, the three things below
are config steps. What is still code, and why, is stated under each.

Vocabulary: an **engine** is how clips are made (`ai_video`, `sourced_video`,
`sourced_slideshow`, ...). A **format** is one niche's recipe under an engine
(`truck-scenic` is TRUCK on `ai_video`). A **look** is one page's font, size,
colour, background, filters and frame (a Dossier recipe publication). A
**page** is one TikTok account the fleet posts.

## 1. Add a new engine (Lab config, 3 files, 1 Lab deploy)

An engine binds a name to a material source and an executor kind the Lab has
code for. Today's executor kinds: `prompt_family` (generated video),
`source_dna_recut` (recut from a source library), `slideshow_renderer`
(image or lyric-image library). Those three are the closed, code-backed set
(`services/content_engine_registry.py` `KNOWN_EXECUTOR_KINDS`).

1. `recipes/content-engines.v1.json`: add the engine.
   ```json
   "test_engine_x": { "label": "Test engine X", "materialSource": "image_library", "executorKind": "slideshow_renderer" }
   ```
   Ids are `^[a-z][a-z0-9_]{0,39}$`. `materialSource` must be one the executor
   kind reads (`MATERIAL_SOURCES_BY_KIND`).
2. `recipes/content-format-contracts.v1.json`: add one format per niche the
   engine serves (copy a sibling on the same executor kind, change
   `contentNiche`, `contentEngine`, `materialSource`; keep the
   `creativeAuthority` of the executor you reuse).
3. `recipes/content-engine-registry.v1.json`: add the matching profile
   (`contentEngine`, `materialSource`, `executorKind`, `executorId`,
   `executorVersion` as the sibling, `formatContractVersion` =
   `sha256:` of the contract entry in canonical JSON: `json.dumps(entry,
   sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).
4. `pytest tests/test_content_engines.py tests/test_content_format_contracts.py -q`
   fails closed on any mismatch. Open the Lab PR; the Lab train deploys it.

Then, with no Worker change: `GET /v1/format-contracts` serves the engine in
`engines` with `contentEnginesVersion`; the Worker's format authority verifies
the hash, Page Settings labels the engine, North Star lists its formats, and
the planner projects a page whose Notion `ContentEngine` is the new engine and
whose `Content Niche` is the new format's niche (registry first,
`notionAccountProjection.js` `resolveNicheProjection`).

Still code (by nature): a new **executor kind** (a renderer that does not
exist yet) and any engine-specific rule such as sourced-video clip speed.
Still a reviewed edit by SOP: a new **niche**. The niche vocabulary is a
controlled taxonomy vendored from shipstream `data/niche_map.json` into the
Worker (`src/domain/nicheVocabulary.js`, "unknown aliases create blockers,
never taxonomy"), so a format for a niche no page has yet needs that
vocabulary entry; a new engine for an existing niche does not.

## 2. Add a page (Notion + one row, 0 deploys)

1. Notion Master Pages: create the row (Account Username, Account Group, Page
   Type, Content Niche, ContentEngine, Automation vs Operator, Vault Link).
   The Lab's `/v1/roster` snapshot carries it; the Worker Cron reconciles it
   into `cp_pages` (INSERT) and the Dossier bootstrap locks its first recipe.
   Unknown Account Group labels are refused by name (`rosterReconcile.js`
   `GROUP_TO_OWNER`), never guessed.
2. Lists that used to be committed Worker files are now `cp_pages` columns
   (Worker migration `0127_page_curation.sql`). Set what applies, through the
   D1 gate (reviewed operator SQL, never by hand on prod):
   ```sql
   UPDATE cp_pages SET caption_delivery = 'in_app' WHERE id = 'acct:<handle>' AND owner_id = 'automation';
   UPDATE cp_pages SET warner_operated = 1        WHERE id = 'acct:<handle>' AND owner_id = 'automation';
   UPDATE cp_pages SET lyric_pool = 'lyric'       WHERE id = 'acct:<handle>' AND owner_id = 'automation';
   ```
   NULL = "the committed file decides" (no behaviour change). A row's value
   wins over the file for `caption_delivery` (a row saying `'burned'` beats
   the file's `in_app`). For `warner_operated` and `lyric_pool` the file is
   a floor: a row adds a page to the list; it cannot remove a page the file
   names (that stays a file edit until the files are retired).
3. Phone side (poster phone, keyboard, atlas) stays the rail's chain
   (`zcode/PAGE-ONBOARDING-BIBLE.md`).

Where each column is read (Worker, after PR B):
- `caption_delivery`: planner compile (`materializer.js` every
  `policyCaptionConfig` call), claim door (`postExecution.js`
  `claimBurnedOnly`), calendar edit door (`slots.js` `replacePlannedSlot`),
  look-apply rebuilds. Not yet: `swarm/nextBestPlay.js`,
  `standingRouting.js`, allocator shadow reports (read the committed map;
  they report, they do not plan).
- `warner_operated`: in-app breaker exemption (`loadInAppTrippedPages`),
  retry-defer (`inAppRetryDeferApplies`). Not: allocator live-apply guard
  (`allocator/warnerPages.json`, allocator lane).
- `lyric_pool = 'lyric'`: compile SQL arms, `eligibleAsset`,
  `materializerRefreshReason`, claim fences, pool refill rule.
  `'blank_only'`: compile SQL arms, `eligibleAsset`, edit door. Not:
  inventory counts (`automationInventory.js`, `dashboard.js`) and
  `staleSlotReplan.js`, which keep the committed list.

A page-policy record (`PAGE_POLICY_MODE=live`) still wins over both.

## 3. Add a look (Dossier UI, 0 deploys)

Open the page's Dossier from Control Plane (Page Settings -> Choose format ->
Dossier), set font, size, colour, background, filters, frame, caption
discipline, then Save & apply. The Lab registers the publication
(`cp_recipe_publications`), edited slots are rebuilt through the same door
with the person's pins kept (`materializer.js` `rebuildEditedSlotsOnLookApply`).
"Use this page's style" copies a look between pages.

Still code: description/hashtag text per page (page-policy `literal` mode is
behind `PAGE_POLICY_MODE`), caption delivery per *slot*, and the caption
length rule.

## Human edits are never overwritten

- 0127 seeds write only `WHERE <column> IS NULL`; re-running is a no-op; a
  value a person set is kept.
- Roster sync writes only the ontology columns (`rosterSync.js`
  `ONTOLOGY_COLUMNS`); it never touches the three curation columns.
- The engine list is read-only data; the Worker refuses a list whose hash
  does not match rather than guessing.

## PRs
- Lab: "One engine list both repos read" (this repo): `recipes/content-engines.v1.json`,
  `services/content_engine_registry.py`, `routers/control_plane.py`
  (`/v1/format-contracts` `engines` + `contentEnginesVersion`; family
  dispatch), `services/{control_plane_generation,dossier_ingredients,control_plane_sources}.py`.
- Worker A: format authority verifies the engine list; labels and North Star
  read it; `contentLabClient` accepts the two fields.
- Worker B: migration 0127 + `src/domain/pageCuration.js` + threading.
