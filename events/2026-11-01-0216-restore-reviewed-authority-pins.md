# Restore reviewed recipe and serialization regression pins

On main `61e43cadab0dae9cb3df38f1e6c562c65a5a4165`, the Burnt recipe retirement removed the fixed legacy-map and prepared-renderer assertions and made the OFF golden comparison recursively omit five authority/hash fields. The retirement itself remains intact.

Restore the fixed current legacy-map digests and the unchanged prepared-renderer `017c619a...` digest. The exact current offline enqueue/executor/artifact capture differs from the retained historical golden only at the two stored `engineRegistryHash` paths, plus the already-checked private `sourceClaimVersion: 1`. Substitute the reviewed `4c8de21c...` to `640e8bac...` digest only at those two expected paths, then compare the complete document. Executor, contract, profile, request, and all nested fields remain compared.

Validation: both touched test files pass (30 tests, Python 3.11, scrubbed environment and inherited OS network/credential fence). Five in-memory corruptions of retained real-runner output survive the exact old assertion and fail the repaired assertion. Two map mutations and an actual renderer minimum-height predicate mutation fail the restored pins; corresponding baselines pass. No production, recipe, historical golden, public-wire, or deployment changes. No full-suite claim.
