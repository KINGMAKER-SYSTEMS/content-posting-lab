# Managed photo refill cooldown

Pen Pal and Twin replenish from distinct page-library photos
(`image_video_variant_plans`, `plan_slideshows`). This guard stops a broken
renderer or library from burning through those plans one failure at a time.

New photo-job admission and capabilities inspect the page's existing terminal
photo jobs. Two consecutive failures (ordered by terminal time, ignoring active,
cancelled, foreign-page, nonphoto jobs and legacy rows without a photo plan
list) advertise zero capacity with zero reason `photo_jobs_cooldown` and reject
new creates with HTTP 409 `photo_jobs_cooldown`. Idempotency replays still
return the original job. A later completed photo job or thirty minutes after
the most recent failure restores admission automatically. No store field or
public schema is added. The existing 30-day job retention exceeds this
thirty-minute window. The warning `photo_jobs_paused` identifies the page and
cooldown. Existing jobs may finish during a pause; this guard does not cancel
or resubmit paid work.

If a renderer keeps failing, at most two serial failed photo jobs are admitted
per cooldown before the next pause. Existing ready stock and other pages remain
available. Investigate the saved job error when the warning repeats; the guard
is a bounded retry fallback, not evidence of successful rendering or posting.
