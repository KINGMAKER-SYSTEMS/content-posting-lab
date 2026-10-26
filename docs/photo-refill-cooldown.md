# Managed photo refill cooldown

Pen Pal and Twin plan new render treatments of their pinned photos. The total
video duration changes in 1/30 second steps; each photo receives an equal share.
A Pen Pal treatment can equal the legacy plan, so the production planner skips
repeated signatures. Final clip hashes still reject repeated delivered bytes.

New photo-job admission and capabilities inspect the page's existing terminal
photo jobs. Two consecutive failures (ordered by terminal time, ignoring active,
cancelled, foreign-page and nonphoto jobs) advertise zero capacity and reject new
creates with HTTP 409 `photo_jobs_cooldown`. Idempotency replays still return the
original job. A later completed photo job or thirty minutes after the most recent
failure restores admission automatically. No store field or public schema is
added. The existing 30-day job retention exceeds this thirty-minute window.
The warning `photo_jobs_paused` identifies the page and cooldown. Existing jobs
may finish during a pause; this guard does not cancel or resubmit paid work.

If a renderer keeps failing, at most two serial failed photo jobs are admitted
per cooldown before the next pause. Existing ready stock and other pages remain
available. Investigate the saved job error when the warning repeats; the guard
is a bounded retry fallback, not evidence of successful rendering or posting.

The executor JSON's `deterministic_single_plan_without_replacement` value is a
legacy serialization tag retained to preserve queued jobs' executor hashes. The
runtime adapter advertises `deterministic_variety_without_replacement`. The
unused single-plan planner and its exhaustion tests have been removed; refill
coverage exercises the actual production planner and admission path.
