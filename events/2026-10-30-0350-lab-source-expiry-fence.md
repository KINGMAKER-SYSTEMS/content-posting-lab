# Keep completed source claims through late expiry

Foreign-runtime source expiry now checks and writes under the same job-store transaction. A late completion that wins first stays completed and reserved. A source setup failure also takes the runner claim before recording failure, so it cannot fail a different live runner.
