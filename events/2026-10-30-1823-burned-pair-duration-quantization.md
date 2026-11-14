# Admit equal-frame clean/burned pairs across one millisecond of quantization

Two real 432x768 source fixtures produced valid 1080x1920 clean and burned
outputs with 26 decoded frames each. Their independently encoded containers
reported 867 ms and 866 ms. Durable admission rejected the burned copy because
it compared that duration exactly against the clean receipt.

Keep exact-duration behavior. Allow only an adjacent one-millisecond duration
when both durations are positive finite numbers and both decoded frame counts
are positive, finite and equal. Two milliseconds, unequal/unproved frames,
wrong types and nonfinite values refuse. SHA, QA, byte-count, geometry, codec,
source, authentication and immutable receipt checks remain unchanged.

No public receipt, stored authority schema, family duration or renderer output
changes. The retained real artifacts pass the same read-only verifier without
changing any file bytes; consumers receive the original receipts unchanged.
This fix belongs to the later API/final composition, not compatibility phase A.

Validation: both retained real failures reject on the parent and pass after
the fix without encoding. The focused job, deduplication and new duration
suite passes 142 tests, excluding seven actual-encode cases. Four private
in-memory mutants are caught: a two-ms allowance, unequal frames, nonfinite
frame admission and absent duration validation. Mutation runs never alter
source or shared build caches. No provider, phone or deployment action.

Evidence: `evidence/root-consolidation/lab-offline-broad/pair-duration-repair`
in the external control-plane-consolidation workspace.
