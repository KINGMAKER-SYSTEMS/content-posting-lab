# Keep the source claim through task-wide shutdown

The source write drain now tracks the executor future directly, rather than a child Task that a shutdown cancel-all sweep can cancel before its thread stops. It retains the caller's context and keeps the runner claim until the actual persistence work ends.
