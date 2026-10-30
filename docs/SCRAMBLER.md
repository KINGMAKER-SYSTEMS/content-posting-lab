# Scrambler — default OFF

The Scrambler varies source-video cuts before planning: output length, effective
speed, offset, treatment and mirror. It applies to resolved source-library
recipes, including Between the Lines under its tt-warner storage prefix.
It does not accept already-cut Warner client clips as source material.

## Policy authority

The [canonical iteration strategy](https://github.com/KINGMAKER-SYSTEMS/control-plane-pad/blob/main/zcode/MASTER-ITERATION-STRATEGY-1029.md)
owns source-use and variety policy (§1, §3, §7a, §9). This document covers the
Lab implementation, configuration and release handoff. Later direct user steering
caps the fast branch at 1.3× and favors slow cuts; the configurable implementation
default is 0% fast (slow-only, matching canonical §7a step 1), with the fast branch
opt-in via SCRAMBLER_FAST_SHARE.

Draws freeze before planning. The reserved span includes the skipped offset plus
output length times effective speed. Private canonical origin coordinates never
rewrite public source descriptors or identity hashes. Full-window fingerprint
ranking operates within fresh/used classes; missing coverage takes a named
spacing fallback.

## Source-use protection

Speed, loops and cut timing are creative changes; they do not reset source-use
history or guarantee a platform will never flag an upload.

The ON path requires usage.jsonl updated within 26 hours and valid origin
mapping and retirement data. Only usage has a file-age gate; map and retirement
currency must be verified against their authoritative producers before activation. Canonical origin IDs remain case-sensitive. Unknown mappings, impossible
windows, stale/corrupt feeds and retired sources refuse safely.

Every covered origin second retains the canonical policy in §1. Unassigned prior
windows on a known origin conservatively cover that origin. Feed reads and
indexed entries have explicit resource limits.

Private Scrambler queued and rendered-but-unposted commitments hold canonical footage
across pages, aliases and different cut lengths. Overlapping origin seconds
cannot be reserved twice. Only exact asset-bound posting evidence with covering
origin seconds releases a rendered hold; copied producer timestamps persist so
feed rotation cannot resurrect old holds. Render timestamps never become
posting timestamps.

## Configuration

    SCRAMBLER_ENABLED=1
    SCRAMBLER_PAGES=acct:page-id,acct:another-page
    SCRAMBLER_DENY_PAGES=acct:blocked-page
    SCRAMBLER_FAST_SHARE=0
    SCRAMBLER_PAGE_FAST_SHARE=acct:page-id:0.05
    SCRAMBLER_HISTORY_PATH=/durable/path/scrambler-history.json
    SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT=1

Deny wins. Page overrides split at the last colon. The master switch defaults
off. The OFF path does not load originality feeds or draw history.

### Page ids: use the roster integration_id exactly

SCRAMBLER_PAGES, SCRAMBLER_DENY_PAGES and SCRAMBLER_PAGE_FAST_SHARE compare the
page id the Worker sends (cp_pages.id, the roster ``integration_id``) EXACTLY;
there is no normalization. For a Notion-sourced page that id is ``acct:`` plus
the handle with every run of characters outside ``a-z0-9`` turned into ``-``
(services/notion_pages.py ``mint_integration_id``). Example: the handle
``freddy.2am`` has page id ``acct:freddy-2am``. So ``acct:freddy-2am`` turns
that page on, while ``freddy.2am``, ``acct:freddy.2am`` or ``Freddy.2am`` match
nothing and silently leave it OFF. Copy each id from the roster; preflight
(below) flags any entry that is not in the ``acct:<slug>`` / ``acct:rail:<hex>``
form.

### Pilot environment (exact)

    SCRAMBLER_ENABLED=1
    SCRAMBLER_PAGES=acct:trailheadtravis,acct:backpack-bohemian,acct:freddy-2am
    SCRAMBLER_FAST_SHARE=0
    SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT=1
    CONTENT_LAB_ORIGINALITY_USAGE_PATH=/app/projects/originality/usage.jsonl
    SCRAMBLER_FEED_R2_BUCKET=rt-content-buckets
    SCRAMBLER_FEED_R2_PREFIX=originality/feed/
    SCRAMBLER_HISTORY_PATH=/app/projects/scrambler-history.json

origins.json must sit beside usage.jsonl (/app/projects/originality/origins.json).
Preflight reports NOT READY when either file is over 26 hours old. At run time
the ON path checks the age of usage.jsonl only (see "Source-use protection"),
so a stale origins.json alone fails preflight but does not stop ON. The
scheduled feed pull keeps both exactly as fresh as the producer's data: their
mtime is the manifest's ``generated_at`` (see "Feed provisioning: one path").
SCRAMBLER_HISTORY_PATH may be left unset when RAILWAY_VOLUME_MOUNT_PATH is
/app/projects (the default history file is then the same path). Each pilot page's
recipe must resolve as source_library / source_dna_recut and carry
``masterPages.handle``; otherwise its enqueue refuses with
``scrambler_page_handle_missing``.

Check the configuration before and after the flip:

    python tools/scrambler_report.py --preflight

It prints one JSON line and exits 0 when every ON-path requirement is met, or
1 with every missing or unusable setting named (switches, page-id form, Worker
compatibility acknowledgment, usage ledger and origins map presence and age, a
real parse of the feed, the durable history path, and the two feed-pull
settings). It never writes. The app also logs one ``scrambler preflight:`` line
at startup (OFF, ready, or NOT READY with the same names; the startup line skips
the full feed parse). Neither adds anything to any HTTP response. After the
flip, still confirm one enqueue per page returns 200.

Plain ``--preflight`` names ``SCRAMBLER_ENABLED must be 1`` while the switch is
off, so it cannot be the gate BEFORE the flip. Use ``--preflight --as-enabled``
for that (the "Pilot flip" steps below).

### Pilot flip (exact steps)

Who runs it: Releases, from the DEPLOYED image (a shell in the running Lab
service, working directory ``/app``, the service's own env and R2 keys).
Only these reviewed commands; no ad-hoc code.

1. Deploy #281 with ``SCRAMBLER_ENABLED`` unset (or ``0``). Expect the startup
   line ``scrambler preflight: OFF``.
2. Set every pilot setting EXCEPT the switch (the "Pilot environment (exact)"
   list above without ``SCRAMBLER_ENABLED=1``). The service restarts. With
   ``SCRAMBLER_PAGES`` set and the switch off, the startup line reads
   ``scrambler preflight: NOT READY (...): SCRAMBLER_ENABLED must be 1; ...``.
   That is expected at this step; the gate is step 4, not this line. The pages
   stay OFF (the gate's reason is ``scrambler_disabled``).
3. Pull the feed once. This proves the Lab's R2 key can read the feed and
   puts the first feed on the volume:

       python tools/scrambler_report.py --pull-feed-once

   Exit 0 prints ``{"result": "ok", "generated_at": ..., "rows": ...,
   "sha_ok": true, "warnings": [...]}``. If ``warnings`` holds
   ``manifest_stale``, the producer's feed is over 20 h old: stop, and call the
   originality lead (step 4 will refuse it anyway). Any other exit: see the
   table below, fix, and run step 3 again.
4. The gate:

       python tools/scrambler_report.py --preflight --as-enabled

   It checks every ON-path requirement as if the switch were 1, plus proof of
   the pull (manifest.json beside the usage ledger, matching sha256s and rows,
   ``generated_at`` at most 20 h old). Exit 0 with ``"ready": true`` is the
   only go. Exit 1 lists every problem by name; fix them and repeat (from
   step 3 if a feed file is named).
5. Set ``SCRAMBLER_ENABLED=1``. The service restarts.
6. Watch the startup line: it must read ``scrambler preflight: ready
   pages=acct:...``, followed shortly by
   ``content_lab.scrambler-feed-pull.ok generated_at=... rows=...`` (the
   startup pull). ``NOT READY`` or a ``content_lab.scrambler-feed-pull.<reason>``
   error line: set ``SCRAMBLER_ENABLED=0`` (rollback) and read the names.
   Then run plain ``--preflight`` (exit 0) and confirm one enqueue per page
   returns 200.

``--pull-feed-once`` exit codes (it prints only the error class, the reason
name, the feed file and the R2 error Code; never keys, endpoints, paths or
other env values):

| exit | ``error_class`` | meaning | who fixes it |
|---|---|---|---|
| 0 | (``result: ok``) | pulled, sha256s and rows verified, written | nobody |
| 3 | ``r2_access_denied`` | R2 refused the read (``AccessDenied``, ``InvalidAccessKeyId``, ``SignatureDoesNotMatch``, ...): the Lab's R2 key lacks read on ``rt-content-buckets`` | Eric/John: the credentials step (grant the Lab key read on the bucket); then rerun step 3 |
| 4 | ``manifest_missing`` / ``feed_file_missing`` | ``NoSuchKey``: the producer has not put the feed (or one file of it) at ``originality/feed/`` | originality lead: run the producer (r4, ``sha256s`` map) |
| 5 | ``feed_invalid`` | manifest not valid JSON or missing a field, ``generated_at`` not ISO 8601, a ``sha256s`` entry missing, a sha256 or row-count mismatch. Old files kept | originality lead: fix the producer output |
| 6 | ``other`` | anything else, e.g. ``NoSuchBucket`` (wrong ``SCRAMBLER_FEED_R2_BUCKET``), ``feed_unconfigured`` / ``dest_unconfigured`` (env unset), ``write_failed`` (volume), network (``exception`` names the class) | Releases: check the env names and the volume; R2 network errors: retry |

Exit 1 and 2 are not used by the pull (Python crash and argparse usage error).
``--preflight --as-enabled``: 0 ready, 1 not ready (problems named).

ON feed paths (usage is required; origins defaults beside usage, and retirement
defaults to the versioned repository list):

    CONTENT_LAB_ORIGINALITY_USAGE_PATH=/path/usage.jsonl
    CONTENT_LAB_ORIGINALITY_ORIGINS_PATH=/path/origins.json
    CONTENT_LAB_ORIGINALITY_RETIRED_PATH=/path/retired-origins.json

An explicit history path must be absolute. Otherwise an existing writable
absolute RAILWAY_VOLUME_MOUNT_PATH is required. No ephemeral roster fallback
is allowed. Preserve the ledger, its creation marker and job-store history.
Existing Scrambler jobs with missing ledger identities refuse; deleting history
is not a reset procedure.

### Feed provisioning: one path

There is ONE scheduled path for the originality feed: the in-app pull,
services/scrambler_feed_pull.py.

- Producer (control-plane, Mac launchd, originality lead): once per night in
  the 00:00-06:14 ET quiet window, tools/launchd/bin/originality-feed.sh puts
  three objects in the PRIVATE R2 bucket:
  ``rt-content-buckets/originality/feed/{manifest.json,usage.jsonl,origins.json}``.
  The manifest contract (services/scrambler_feed_pull.py ``parse_manifest``) is
  ``{"generated_at": "<ISO 8601 UTC>", "rows": <usage.jsonl line count>,
  "sha256s": {"usage.jsonl": "<hex64>", "origins.json": "<hex64>"}}``. All
  three fields are required; extra fields are ignored.
- What runs in the Lab: a daemon thread started in the app lifespan on EVERY
  boot, beside the compaction scheduler. While ``SCRAMBLER_ENABLED=1`` it pulls
  once at startup and then every 60 minutes. While OFF the thread still exists
  but only sleeps and re-reads the switch once an hour: no R2 call, no write.
  ``tools/scrambler_report.py --pull-feed-once`` runs one pull by hand,
  regardless of the switch (the pilot-flip bootstrap, step 3 above).
- R2 access: it uses the Lab's existing R2 client (services/r2.py: the
  ``R2_ENDPOINT`` / ``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY`` already set
  for Clipper). There are no feed credentials of its own. That key must be able
  to read ``rt-content-buckets``.
- Object keys: ``SCRAMBLER_FEED_R2_BUCKET`` names the bucket and
  ``SCRAMBLER_FEED_R2_PREFIX`` IS the feed directory. With the pilot values the
  keys are ``originality/feed/manifest.json``, ``originality/feed/usage.jsonl``
  and ``originality/feed/origins.json``. Nothing is appended to the prefix.
- Checks: it downloads all three files and checks each file's sha256 and the
  usage row count against the manifest before writing anything. Any failure
  keeps the old files and logs ``content_lab.scrambler-feed-pull.<reason>``
  (for example ``sha256_mismatch:usage.jsonl`` or ``fetch_failed:manifest.json``).
  A manifest whose ``generated_at`` is not an ISO 8601 time is refused
  (``manifest_invalid``). A manifest more than 20 h old logs ``manifest_stale``,
  but the files are still written, with that old time as their mtime (below).
  A good pull logs ``content_lab.scrambler-feed-pull.ok``.
- Where the files land: the directory of
  ``CONTENT_LAB_ORIGINALITY_USAGE_PATH``, so in the pilot
  ``/app/projects/originality/usage.jsonl``, ``/app/projects/originality/origins.json``
  and ``/app/projects/originality/manifest.json``. (If the usage path is unset,
  the files go to ``$RAILWAY_VOLUME_MOUNT_PATH/originality/``, but ON needs the
  usage path anyway.) Each file is written to a temp file and then renamed into
  place. origins.json is replaced first, then usage.jsonl, then manifest.json.
  This order narrows, but does not close, the window in which a reader pairs
  two generations: a reader that read the old origins.json just before both
  renames and reads usage.jsonl after them sees old origins with new usage.
  The impact is small (each producer usage row carries its own origin
  identity). If a rename fails part-way, the replaced files are put back in
  reverse order.
- mtime = the data's age, not the pull time: usage.jsonl and origins.json get
  the manifest's ``generated_at`` as their mtime (capped at now). The 26 h gate
  reads that mtime, so if the producer stops refreshing the feed, ON refuses 26
  hours after the last ``generated_at``, however often the Lab pulls. The
  producer must therefore run every night: after a missed night the feed passes
  26 h and ON refuses (fails closed) until the next good producer run and pull.
  manifest.json keeps the pull time as its mtime (when the last good pull ran).
- Preflight names a setting the pull cannot serve: the usage path must end in
  ``/usage.jsonl``, and ``CONTENT_LAB_ORIGINALITY_ORIGINS_PATH`` must be unset
  or point at origins.json beside it.

Manual fallback only (no schedule): tools/scrambler_feed_sync.py copies
usage.jsonl + origins.json from a local directory into the same place, in the
same order. Use it to seed the volume before the first pull, or to recover
while R2 is unreachable:

    python tools/scrambler_feed_sync.py --source /feed/dir --dest /app/projects/originality

It refuses (exit 1) rather than copy a missing, stale (>26 h) or oversized
usage ledger or an invalid origins map. The next good scheduled pull simply
overwrites what it copied. Do not schedule it next to the pull.

The loader remains the authority. Neither the pull nor the sync tool changes
what ON accepts; they only keep a fresh copy where ON reads it.

The active 64-draw rotation is distinct from durable capacity: 4,000 rows per
page, 20,000 total rows, 256 pages and a 32-MiB file limit. All operation
identities, including released tombstones, remain durable. Capacity exhaustion
refuses by name and requires an operator migration. It never silently prunes
history. Fully released operation IDs cannot be redrawn.

## Activation dependencies

What the live Worker admits (read from Worker main 91f4e1a, 2026-10-29):

* the ORIGIN window of a sourced clip (cutWindow.originalEndMs -
  originalStartMs) must be 5–10 s (sourceVideoBounds.js, reserveSourceWindow,
  D1 migration 0091);
* the receipt's visualTreatment must equal the page recipe's look exactly,
  with clipSpeed 0.5–2 (sourceTreatment.js sourcePreparationBlocker,
  renderTreatment.js). A receipt with any other speed is "previous look" stock
  and is never scheduled; below 0.5 it is rejected as malformed.

So the Lab never draws a cut whose origin window is outside 5–10 s
(``worker_admits_window``): a 0.3×–0.55× cut shows at most 4.95 s of origin and
would be encoded, marked rendered and then refused. On the slow branch this
leaves 0.6×–0.8× (0.6× at 8.5–9 s up to 0.8× at 6.5–9 s); the fast branch keeps
1.1× (up to 9 s), 1.2× (up to 8 s) and 1.3× (up to 7.5 s). The receipt keeps the
recipe's clipSpeed (the Worker requires it); the drawn speed is recorded in the
manifest-only ``scrambler`` block. Restoring 0.3×–0.55× and putting the drawn
speed on the receipt both need a Worker release first. The compatibility
environment variable stays an explicit operator acknowledgment. Never falsify
cut bounds to satisfy admission.

Lab ordering and render-day limits do not control actual posting order or day.
Worker inventory selection can reorder assets. Any guarantee about consecutive
POSTED treatments or reverse counts per POSTED day requires posting-side
scheduling support and verification.

Before a pilot, verify current page-bound source libraries, fresh feeds,
retirement data, deployed Worker compatibility and first rendered/delivered
artifacts. Pilot candidates: acct:between-the-lines66,
acct:healing-in-the-hills and acct:velvet-hours. Cached libraries are not current
production authority.

Rollback: disable the master switch or deny the page. Queued Scrambler jobs
fail safely and release unrendered commitments. Legacy jobs retain their
original execution path. Rendered records remain durable.

## Daily fleet-report handoff

Run from the image (tools/ ships in the production Dockerfile) or the Lab
checkout with its existing Python environment:

    python tools/scrambler_report.py --history /durable/path/scrambler-history.json

The command selects currently enabled pages from SCRAMBLER_PAGES. Repeat
--page acct:page-id for explicit historical pages even when OFF. Without
--history it uses the core durable environment path. Optional --now accepts
an ISO timestamp for replay.

JSON stdout is scoped lab_rendered_last_24h: accepted rendered cuts, including
retained completed prefixes of failed jobs, not posted or delivered counts.
It includes per-page statistics and repeat/zero-cut alerts. Exit 0 means no
alerts, 1 means repeat/zero alerts, and 2 means unavailable history,
configuration or clock. Implicit disabled/denied pages skip history reads.
Missing history or its creation marker refuses without bootstrapping/resetting.
The ordinary store-lock sidecar may be created when absent.

Connect this command's JSON and exit status to the fleet report before a pilot.
This PR supplies the producer; it does not install a fleet scheduler or consumer.

## Failure and verification

Enqueue releases reservations on plan, directory, enrichment and durable-save
failures. Runtime expiry, cancellation, setup/render failure and skipped
backups release unrendered rows. Rendered rows and their asset identity remain
immutable; retrying a rendered commit cannot move its timestamp. ON execution
serializes per page across tasks/processes. A frozen suffix that conflicts after
a skipped or released predecessor refuses before encoding; it is never redrawn.
A cut refused at execution (originality recheck, commitment mismatch, a
render-order/reverse-day conflict, or an output length outside 5–9 s) is
skipped and only its own reservation is released; the job's vetted cuts still
complete and deliver. A job fails only when every cut is refused (named with
the first refusal) or on a hard error (encode failure, history corruption).
Runbook: when a middle cut is refused, the next cut can be refused too. Its
treatment and length were drawn to differ from the refused cut, so once that
cut is gone it may repeat the treatment (or length) of the cut rendered before
it, and the render-order check refuses it. This is fail-safe by design and
costs one more cut; the cut is never redrawn.
Partial ON failures retain completed assets and private recovery checkpoints;
failed jobs expose no partial public artifacts. A rendered cut whose job failed
can never deliver: 24 hours after it rendered, the next enqueue releases its
origin-window hold (``holdRelease`` on the ledger row). A cut whose job is
unknown to the job store keeps its hold: job-store compaction archives
completed (delivered) jobs after 30 days, so an unknown job may be a delivered
cut that has not posted yet. The row stays rendered, so its combo and reverse credit are unchanged. Recovery must match each
checkpoint against a committed asset SHA in the durable ledger. Uncommitted
RESERVED operations absent from the durable job store are reconciled under the
enqueue lock. The originality feed is parsed off the event loop before the jobs
lock is taken, and one parse is reused while the feed files are unchanged (at
most 10 minutes), so an enqueue and every cut of its job share it.

Behavioral tests cover actual enqueue/execution, canonical alias holds, exact
posting observations, failure cleanup, state corruption, long rotations and
reverse caps. The OFF golden compares the original base checkout's serialized
surface with the candidate under fixed randomness. Portable edited-source
mutation runners are tools/verify_scrambler_core_mutants.py and
tools/verify_scrambler_reader_mutants.py; both require a passing baseline and
preserve source hashes, patches and logs. Run remotely through suite-slot.
