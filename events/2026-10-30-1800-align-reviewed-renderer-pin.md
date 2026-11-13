# Align the static-renderer guard with reviewed 768p support

The full offline gate found the lyric-format identity test still pins the
renderer from #248. Main #282 (1e57fc2) intentionally lowered the accepted source
height from1080 to768 for upright Hailuo crops and updated the refusal message.
The entire renderer AST is identical to the pinned source after restoring only
those two literals. Legacy >=1080 behavior and both frozen registry hashes stay
unchanged. Update only the expected full-file SHA and document that provenance;
keep the strict assertion and both registry guards. No runtime file changes.

The pin plus existing low-height/landscape/SAR/rotation refusals and legacy
no-frame delivery graph controls pass15 checks. An isolated compiling mutation
of the near-vertical width calculation still fails the exact repaired pin; the
same unmodified copy passes. Existing actual432x768 specified/unspecified-SAR
render fixtures will run in the corrected broader offline gate. The interrupted
first broad run is invalid as full qualification because its external launcher
re-entered pytest in macOS spawn workers; the evidence-only launcher is now
__main__ guarded and its bounded one-worker spawn control passes.
