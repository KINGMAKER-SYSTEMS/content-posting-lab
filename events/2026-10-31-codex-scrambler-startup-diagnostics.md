Move read-only Scrambler startup diagnostics off the lifespan readiness path.
The existing owned-process supervisor bounds master resolution to ten seconds
including child startup and joins cleanup on timeout or app shutdown. Existing
enqueue refusals, stopped-runtime reconciliation and pilot activation checks
remain unchanged. A diagnostic timeout reports unknown readiness, never ready.

The real 61e lifespan fails the controlled blocked-reader startup assertion.
Focused tests cover responsive actual ASGI startup, shutdown joining, real owned
reader deadline cleanup, OFF reporting, NOT READY severity and child failure.
