# Managed photo reservations

time: [04:54] [04-11-26] America/New_York
agent: [codex desktop] [gpt-6.1-sol]
worktree: [codex/managed-photo-reservation-handoff-20261104] content-lab-photo-reservations-20261104
type: [feature-request]
area: [backend] [testing]

Add a reserve-only route for the exact PenPal and Twin recipes. It uses the existing slideshow planner under the shared job-store lock and records the ordered plan before acknowledging the request. Live and archived Lab jobs constrain direct reservations, and direct holds constrain ordinary refills and their capability counts. A replay can recover the exact plan from disk without reading the library. The endpoint launches no generation and retains holds across ambiguous remote outcomes.

The old route returned 404 in the new mounted regression. Both page fixtures now pass restart replay, simultaneous direct and legacy reservations, archive compaction, exhaustion, corruption, and app authentication checks. The broader Control Plane suite passed 698 tests with one skip. Source base d014261d. Direct execution still needs the Posting checkpoint and Syzygy receipt consumer.
