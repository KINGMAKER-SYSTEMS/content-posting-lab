# Fence late source-job writes

Every source-runner state write now checks the job is still active inside the job-store lock. A thread that finishes after task cancellation cannot turn a failed job back to running or completed after its window was reassigned. Source setup failures use the same check, so they cannot erase a completed reservation.
