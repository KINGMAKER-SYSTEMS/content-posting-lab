# Flagged retired-source capability reason

A retired source library still refuses resolution. Capabilities now carry its
`source_master_retired` diagnosis through the existing zero-plan advertisement
flag; flag-off responses retain their earlier shape. Execution callers retain
the recipe-or-None contract and existing job-creation refusal. The resolver runs
once per binding and keeps its earlier retirement-check precedence.

The historical-library listing test and cross-repository origguard comment now
distinguish retained history and an external sync check from execution authority.

Validation: normal Maj light gate ran 14 selected-function stdlib tests against
actual resolver/capabilities/projection bodies; all passed. The same fixture on
base 036bb75 failed three named-wire assertions, with no setup errors. Full
FastAPI route tests were added but not executed: the approved interpreter's
Uvicorn/Starlette dependency gate remains open. Selected-function fixtures do
not establish HTTP/auth/startup or deployment readiness. No flag was enabled,
Worker allow-list changed, service installed, or paid work started.
