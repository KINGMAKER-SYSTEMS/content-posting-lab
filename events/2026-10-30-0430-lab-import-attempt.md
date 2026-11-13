# Fence source-import retries

A source import now holds a per-job claim and records a private attempt identity. Its writes settle before cancellation releases the claim, and an older attempt cannot overwrite a retry or a completed result. A legacy foreign import whose runner cannot be proven stopped stays held for reconciliation.
