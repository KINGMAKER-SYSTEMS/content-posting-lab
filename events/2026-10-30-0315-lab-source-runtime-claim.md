# Keep source windows held during runtime overlap

A source runner now holds the existing per-job kernel claim while cutting. A different runtime may mark its job failed only if it holds that claim, and a queued runner checks the job is still active after acquiring it. This keeps in-flight windows reserved until the runner ends, while still allowing a dead runtime's job to be failed.
