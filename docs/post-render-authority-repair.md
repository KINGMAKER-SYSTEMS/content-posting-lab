# Retained receipt authority

The in-app candidate authenticates retained receipt contents against the exact
original receipt SHA-256 already pinned in the job row. New in-app authority keeps
the original receipt bytes as base64 in the existing output_authority_json.
No table, column, public request or renderer receipt changes.

For old authority without those bytes, the checker reconstructs the production
renderer’s canonical sorted compact UTF-8 encoding and checks its hash. A match
authenticates those exact bytes. If that fails, only the still-present original
receipt file can establish the encoding. Decoded contents must also equal the
retained receipt. A noncanonical retired receipt without recoverable bytes is
unverifiable and refuses. This deliberately changes the earlier compatibility
policy; it does not pretend to recover lost whitespace or key order.

A pair with both burned_fallback members removed cannot become clean-only
legacy: its changed receipt cannot match the original hash. Nested malformed
completion/duration/frame-count and decode/probe types retain typed refusals.

## Recovery and fallback

On startup, one transaction validates completed/pinned in-app history and
backfills permanent burned identities into private /output-identity/v1/ keys in
the existing idempotency table. Missing, corrupt or unverifiable in-app authority
records a private /output-uncertain/v1/ job marker. Known identity reservations
survive corruption. New in-app reservations refuse while any uncertainty marker
exists; ordinary jobs stay available. Status and artifact verification refuse
invalid in-app evidence without enqueueing paid work.

Restoring independently verified original authority and restarting automatically
clears the corresponding uncertainty marker and rebuilds its identity. No code
guesses evidence, refetches media, rerenders, deletes paid output, or rewrites the
immutable job pins. Live legacy noncanonical receipts can acquire exact-byte
retention on normal ACK/retirement before media is removed. Preexisting
unverifiable tombstones require operator evidence recovery, not another render.

The startup uncertainty policy can prevent ACK/retirement that needs a new
in-app reservation, even for another healthy job. Already acknowledged status
replays remain observational. This availability cost is intentional containment.

Stop the old runtime before the new one uses the same database. Mixed old/new
writers are unsupported. Restart always backfills, including after rollback.
A running runtime does not continuously police arbitrary private database edits:
existing permanent identities remain authoritative, and verification detects
receipt tampering on access. Compromise of all pins/request/indexes is outside
the retained-authority corruption model.

## Ordinary traffic and PR 254

Ordinary valid render outputs, persisted authority bytes, final-hash dedup,
tables and write transactions stay as in ee33b9051. Ordinary history never gets
new reservation rows. Its historical duplicate query still scans history; it
now guards malformed JSON before extraction. Invalid old ordinary status omits
unavailable hashes as in the PR 254 LOW repair.

The candidate includes the LOW docs/test naming and malformed-history isolation,
and scopes the permanent index to in-app jobs. It does not fully contain PR 254’s
all-mode indexed reservations, so PR 254 is not superseded. In particular do not
copy its all-mode lookup or storage cost claims onto this candidate.

## Cost and verification

New in-app status verifies a bounded receipt of at most 64 KiB; base64 storage is
at most 87,384 bytes per receipt. No additional provider calls or media renders.
Startup remains linear in history, and permanent reservation storage grows with
history. In-app indexed probes execute inside the output writer transaction.
Ordinary-only startup adds read-only scans but no write transaction.

Regression tests cover primary/shared pairs, double stripping with/without
restart, raw/projection tampering, Unicode canonical reconstruction, null/malformed
historical authority, fail-closed new in-app admission, ordinary availability,
restoration/restart recovery, and direct reservation. The noncanonical baseline
fixture now explicitly expects refusal only after original bytes have already
been lost; its live lifecycle remains covered.
