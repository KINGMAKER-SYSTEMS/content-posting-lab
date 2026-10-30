# Keep legacy source claims safe

Jobs written before source-runner claims have unknown liveness, so a newer runtime leaves their windows held instead of inferring that a free lock proves the old runner stopped. Only new jobs with the exact claim version can be failed after another runtime obtains the claim. An old runtime does not honor the new claim, so a mixed-version rollout needs separate release review.
