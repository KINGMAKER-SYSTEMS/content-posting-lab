time: [19:57] [04-11-26]
agent: [codex desktop]
worktree: [codex/lab-retention-job-cap-20261104]
type: [bug report]
area: [backend], [testing]

Preserved the cleanup job limit across the stray-file walk and the newest-first
confirmation pass. A pass can finish checking clips from a job it has already
selected. It keeps media from further jobs for a later pass.

The focused regression failed on Content Lab #352 at `bcfacdbc`. After the
fix, 84 retention, slideshow, Coffee, and packaged-import tests passed on the
combined tree with main `878c167c`.
