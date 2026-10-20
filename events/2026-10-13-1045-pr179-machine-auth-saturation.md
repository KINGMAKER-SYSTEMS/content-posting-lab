_________________________________________________________________________________
time: [10:45am EDT] [13-10-26]
agent: [codex desktop] [gpt-6.1-sol] [contentlab_pr179_jwks_builder]
worktree: [codex/pr179-jwks-async-local] [/private/tmp/codex-lab179-jwks-20261013]
type: [bug report]: Preserve machine auth during Access cert saturation
area: [backend] [testing]

Independent review found that eight stalled Access cert lookups could make a mixed
Access-plus-Worker request return 503 before checking its valid Worker token.
Machine credentials now take precedence in authorization and bypass the Access
lookup cap when valid; an Access-only request still fails closed at saturation.
Added the mixed-credential saturation regression. FastAPI runs the synchronous
route dependency in its thread pool, so the event-loop block was at pre-body
middleware. Local candidate only; no push, merge, deployment, or live proof.
_________________________________________________________________________________
