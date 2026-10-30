# Advertise the treatments that the source executor actually applied

On main 8aa46b0, the optional source.recipe projection used the recipe's baseline
speed and legacy Layer B flags even when a Scrambler draw supplied a different
render speed, reverse/mirror pass or split-swap. Layer B loop-swap was also omitted.
Actual HTTP enqueue, stored draws and completed execution reproduced three
misreports without encoding media. This affects the gated provenance projection;
no default-off posting, consumer activation or provider consequence is claimed.

Track the successful transform pass and split operation separately for each cut,
after their media call completes. Use the actual render speed and unchanged crop
for source.recipe. Scrambler cuts exclude the legacy lanes; do not invent stacked
or canceling transforms that the executor cannot run. Keep sourceTreatment,
Worker admission fields, identity hashes, schema keys and feature gates unchanged.

Eleven bounded controls cover Scrambler speed/split, actual supported reverse and
mirror with persisted credits, legacy combinations, mutual exclusion, flag-off,
identity/admission fields and a treated-to-untreated multi-cut sequence. Reverting
only the runtime produces six behavioral failures and five passes. Four compiling
mutants each fail their named speed, loop, applied-Scrambler or per-cut-reset
control; restored source passes. The relevant executor/registry/manifest/helper
subset passes 106 tests with six actual-media flag tests excluded. No encoding,
provider calls, full suite, production operation or activation was performed.

The current recovery composition uses drained _settle_source_call instead of
asyncio.to_thread around transforms. Its merge-forward must retain those wrappers;
the base-8aa patch is not an instruction to replace the composed executor.
