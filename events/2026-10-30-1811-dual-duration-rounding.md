# Admit the same decoded dual-final frames across one-millisecond probe rounding

The Lab98 offline run retained two synthetic 768p clean/burned pairs with 26
fully decoded frames, valid hashes and delivery geometry, but independent stream
probes reported 867 and 866 ms. The durable service rejected both at its exact
cross-variant duration check. This condition also exists on main 04c7747.

Keep the old exact-duration acceptance path unchanged. Add only a one-millisecond
exception requiring equal positive integer frame counts, finite positive durations,
and both durations within one millisecond of that count at the renderer's closed
30 fps delivery preset. Keep the existing source, artifact, QA, receipt/HMAC,
codec and geometry bindings. There are no new wire fields or renderer changes.

Replay the retained probe metadata through synthetic output files and the actual
service run/status/reload path. The old runtime fails both positive cases; the
candidate passes 23 bounded controls. The affected service/source-treatment and
legacy-upgrade subset passes 173 tests, with 14 actual-media render cases excluded.
Three isolated compiling mutants removing the equal-frame, cross-duration or
cadence check each fail its named negative control; restore the frozen source.
Huge integer/finite-float combinations retain a typed refusal instead of leaking
arithmetic overflow. No new media encoding, provider calls, production operation,
full suite, publication or deployment was performed for this repair.
