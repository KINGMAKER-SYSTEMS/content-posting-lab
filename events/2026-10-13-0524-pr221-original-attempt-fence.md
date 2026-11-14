_________________________________________________________________________________
time: [05:24am] [10-13-26] EDT
agent: [Codex desktop with Mac mini execution] [gpt-6.1-sol] [focused PR221 repair]
worktree: [codex/pr221-original-attempt-fence] /private/tmp/content-lab-pr221-original-attempt-fence
type: [bug report]: prevent a second TikTok upload submission during unresolved original attempt
area: [backend], [testing], [review]

Integrated PR221 with actual main 89b5831, preserving the newly merged Control Plane capability Bearer gate and append-only root events. The prior upload store validation rejected duplicate job IDs but still allowed a new queued upload for the same account/video while an original persisted job was uploading or had a failed browser outcome. Submission now holds queued, uploading, and failed same-target attempts for original-outcome reconciliation with HTTP 409 and no new queue write or provider launch. The local queue read/modify/write and queued-to-uploading/cancel transitions share a stable cross-process file lock so cancellation cannot mislabel an active Post as safe to resubmit. Focused upload/cookie and merged capability-auth tests pass 40, including path-normalized duplicate admission, byte-for-byte unchanged storage and an in-flight fake browser call. This is an isolated unpushed candidate; independent final-head review, hosted checks, local single-writer limits, and any live outcome remain release holds. No browser Post or production publication was attempted.
_________________________________________________________________________________
