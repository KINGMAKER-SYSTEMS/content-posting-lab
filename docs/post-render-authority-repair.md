# Retained authority repair: bounded offline disposition

F2 is repaired. F1 remains open; this package must not ship as a complete
integrity repair. Source baseline: 73520496993b7cf3f5115fe03b5fef1efd6a2d44.

## F2: controlled refusal

Succeeded status now decodes retained output authority through the same typed
loader as tombstone verification, then validates the authority for both unique
primary owners and shared-clean jobs. Missing authority raises
`output_authority_missing`; invalid JSON or a non-object raises
`output_authority_invalid`. Existing authenticated status and idempotent submit
routes translate these to HTTP 409. Queued/failed status is unchanged.

Detection automatically replaces the unhandled parser exception with the
existing refusal envelope. The safe fallback is to retain the original row,
request, pins, idempotency binding and media for inspection. It neither restores
invented authority nor rerenders, refetches or overwrites paid output. This is
error containment, not automatic repair of lost evidence. Tests cover SQL NULL,
malformed JSON, JSON null/list/number/string for both primary-owner and shared
jobs, direct status/replay, authenticated HTTP status/replay, row preservation,
media retention and a healthy second job.

## F1: conditional storage-corruption case remains open

After successful pair ACK and retirement, editing private SQLite to remove both
`burned_fallback` members (authority and nested receipt) leaves the four persisted
pins unchanged. Existing verification accepts the altered object as legacy;
identity reservation can then admit a repeat burned creative. No public request
producer, actual post or downstream admission bypass is demonstrated.

The receipt pin hashes exact bytes. `_authority_values` recursively sorts keys
when retaining the decoded receipt, losing its original order and whitespace.
Both the actual baseline and current production renderer serialize canonically
(sorted keys, compact separators, UTF-8 without ASCII escaping). For those
receipts recomputing that serialization and comparing its digest to the retained
immutable pin is possible and detects nested receipt alteration. The accepted
baseline fixture, however, came from actual baseline job persistence with a
mock renderer using noncanonical JSON. Its original 1,049-byte receipt hashes to
b846dfcd1ac6b7f633ef450940d2cd996faf137c5bdec68b526dcb016d3756a2;
the canonical 1,006-byte projection hashes to
431df45201bd9104188c3946163d0e14fc8e7c17b35ec13c22329e26e5e35986.
Its retired clean-only authority remains valid under the existing contract.

Thus a canonical-only check would reject a required valid baseline lifecycle.
Treating a mismatch as legacy would retain F1. Guessing a finite list of historic
serializations cannot establish all previously accepted receipt bytes, and a
fixture-specific key-order exception would be a test-specific production rule.
No such exception or silent trust-contract change is included. This is a bound
on this migration-free repair, not a claim that every possible design is
impossible. The code still trusts this retained JSON; its pins do not currently
authenticate the retained receipt contents after retirement.

## Bounded design requiring a separate approved contract

1. Specify whether historical compatibility includes every accepted receipt
   encoding or only proven production canonical producers. Preserve uncertain
   existing rows; do not relabel or synthesize authority from primary pins.
2. For receipts with available original bytes, compare their exact hash to the
   existing receipt pin, decode them, and require exact equality with retained
   receipt facts before using legacy classification or burned identity. Do not
   infer identity from clean bytes alone.
3. Retain original authenticated receipt bytes before deleting media in a future
   approved persistence revision. Define versioning and a bounded maximum using
   the existing 64 KiB receipt limit before implementation. No schema or field
   layout is proposed as already authorized. Hash verification supplies
   integrity against unchanged pins, not authenticity against a writer able to
   rewrite the pins too.
4. For already-retired canonical receipts, authenticate the canonical projection
   against the existing pin. For noncanonical tombstones with no original bytes,
   use an explicitly approved quarantine/refusal policy, or obtain independent
   immutable original receipt evidence. Automatic reconstruction is not safe.
5. Apply the check at status/verification AND inside the same writer transaction
   that reads historical identities for reservation. Fixing only verification
   leaves the dedup path vulnerable. Never skip an uncertain history entry and
   admit a new creative; refuse the reservation with a named integrity error.
   This conservative fallback can block new admissions across jobs until evidence
   is restored; that availability tradeoff needs explicit acceptance.
6. Red tests must include double removal with unchanged pins for primary and
   shared owners, normal and canonical encodings, after restart/retirement,
   status/replay/ACK, duplicate same and different clean bytes, an unaffected
   distinct-caption pair, and simultaneous admission. Prove every bypass mutant
   fails behaviorally. Benchmark parsing/hashing and writer-lock duration at
   1k/10k/20k retained rows before approval; no indexed constant-cost claim.

No migration, repair of existing storage, invented credential, schema change or
production action is included. Current F1 has no detection or automatic remedy;
the red correctness probes are retained as delivery evidence, separate from the
normal regression suite. They must stay visibly red until the contract is
resolved. Request genuine independent-family review of the immutable final head
and this bounded disposition. Codex's checks are supplementary only.
