# Retain page claim through final cleanup

A controlled final-release barrier showed a second same-page executor could
enter while the first job's private ledger cleanup still drained. Keep the
cleanup context inside the acquired kernel page claim and asynchronous page
gate. The outer job claim continues to surround everything. A cancelled waiter
that never entered the page lane terminalizes/releases only its own queued
reservations under its job claim, without touching the current page owner.

The pre-repair control fails on early second-runner entry; the waiting-cancel
control already passes. Corrected ownership/setup/census checks pass 34 tests,
including cancellation while cleanup drains, refused state transitions, failed
persistence, and preservation of the first cancellation when a thread fails
later. The earlier 899-test integration receipt belongs to the parent tree;
repeat the broad phase-A and final composed suites on these corrected sources.
