time: [15:48] [05-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/joy-paired-render-timing]
type: [bug report]
area: [backend]

Joy's slowed 24 fps sources reached the delivery encoder on a half-frame
boundary. The production FFmpeg 5.1 build emitted 282 clean frames and 283
captioned frames, so the prepared pair could not be admitted. Resample both
paths to the existing 30 fps delivery clock before adding the caption overlay.
The source treatment, receipts, duration checks, and retry budgets stay intact.

Reproduced the 282/283 mismatch on FFmpeg 5.1.9; the common clock produces
283/283. Added a real-media regression for a 226-frame 24 fps source.
The focused prepared render, job, provenance and image import suites passed
334 tests.
