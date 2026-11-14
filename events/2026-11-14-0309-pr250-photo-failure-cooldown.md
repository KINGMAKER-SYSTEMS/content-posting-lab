time: [03:09] [14-11-26]
agent: [claude] [opus 5.5]
worktree: [cx1022/t62b-photo-refills]
type: [merge]
area: [backend]

Brought PR #250 current with main by merging main into the branch. Main already
replenishes Pen Pal and Twin from distinct page-library photos, so the PR's
same-photo treatment planner was dropped in favour of main's planner and Worker
contract. Only the missing part was kept: two consecutive failed photo jobs
pause new photo jobs for that page until a later success or 30 minutes pass.
Capabilities now name the pause with the zero reason photo_jobs_cooldown and job
creation returns 409 photo_jobs_cooldown. No schema, deploy or live job was
touched.
