_________________________________________________________________________________

time:  [03:37pm] [10-19-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/burn-import-tos-auth-20261019]
type:  [bug report]: TOS import machine bearer blocked by browser middleware
area:  [backend]: exact-path authentication integration

The app's configured browser API key intercepted even a valid machine Bearer
before the TOS route guard. The exact POST import path now reaches its own
server-only guard; other API routes retain the browser-key gate. Added an
integration test for configured APP_API_KEY, browser-only refusal, valid
machine Bearer, and unrelated route behavior. No live POST was made.
_________________________________________________________________________________
