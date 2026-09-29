# DELETE /jobs/{job_id} traversal fix

Date: 2026-09-29

## Result

The lead's Step 0 probe on Seeno confirmed that `/api/clipper/jobs/%2E%2E` and
`%2e%2e` reached `safe_rmtree(<project dir>)` and returned 200. The route now
validates IDs against the format minted by `_make_clip_job_id` and resolves
only a direct, non-symlink child of the resolved clipper directory. Invalid
IDs and symlinked job directories return 400; a valid missing job still
returns 404.

The same path guard now protects the sibling metadata rename and download-all
handlers, which may write into the job directory or ZIP. Regression coverage
includes both encoded dot-dot spellings with a `safe_rmtree` recorder, a
symlinked job directory, and successful deletion of a real minted job.

## Validation

`python3 -m py_compile routers/clipper.py tests/test_clipper_job_path_safety.py`
completed successfully. Pytest was not run in this checkout per the lead's
instruction; the lead will run the tests on Seeno.

The reproduction probe `probe_delete.py` was removed before commit.
