time: [06:52] [14-11-26]
agent: [claude] [opus 5.5]
worktree: [closer/swarm-ledger-1114]
type: [merge]
area: [review]

Closer ledger for the Nov 13-14 open-PR swarm. Landed on main with merge
commits, each confirmed MERGED on GitHub: #221 (hold ambiguous TikTok uploads),
#226 (refuse undrawable captions), #379 (unused master moments), #250 (photo-job
pause only; its same-photo planner was already superseded on main), #307
(private originality summary pull, off by default), #311 (advertise the applied
source-cut recipe). #382 (Miles backup master) also merged from outside the
swarm. Closed as superseded: #238 (by #379, its scenarios ported as
tests/test_source_freshness_priority.py) and #253 (photo stale-batch repair
landed in control-plane-pad#101; its paired in-app render work is on main via
#248). #381 was closed by its author and reopened as #382. Still held: #179
(route auth; release hold until Cloudflare Access same-origin ingress and live
callers are verified, plus reconciling the import-tos machine-only contract),
#251 (beat slideshows; waits on syzygy#662, still open), #376 (slideshow source
pins; Syzygy production listings must actually serve indexed_sha256 and active
generic jobs need a fresh pin check before rollout). Main health at 88a37bb:
full backend suite 7,902 passed, 24 skipped, 5 failed: the two Christmas-POV
moderation tests and the registry golden hash reproduce the pre-swarm baseline,
and the two capability p95/p99 perf tests were load flakes (load ~35) that pass
6/6 alone. Frontend vitest 54/54 under Node 22 (Node 26 lacks jsdom
localStorage, an environment issue) and the production build passes. Railway
status on main is success. No deploy, provider, job or live data was touched.
