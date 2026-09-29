# DELETE /jobs/{job_id} traversal reachability check

Date: 2026-09-29

## Result

The required live-route TestClient probe could not be executed in this checkout: `python3` does not have FastAPI installed (`ModuleNotFoundError: No module named 'fastapi'`). This report does not claim that the route is unreachable or that the required reachability gate has passed. No application code was changed.

## Evidence from route and middleware inspection

- `routers/clipper.py` registers `DELETE /jobs/{job_id}` and the handler joins the supplied `job_id` to `_get_clipper_dir(project)` before calling `safe_rmtree`.
- Clipper job IDs minted by `_make_clip_job_id` have the form `clip-{project}-{MMDDHHmm}-{4 lowercase hex characters}`.
- The route is mounted under `/api/clipper` in `app.py`. The API key middleware requires credentials only when `APP_API_KEY` is set; it has no route-specific authentication for this endpoint. There is no handler authentication dependency.
- Static path composition indicates traversal may be possible if Starlette delivers `..` as the path parameter, but the requested TestClient observations (including encoded dot/slash cases) are still needed to satisfy Step 0.

## Probe attempted

A Python script instantiated `fastapi.testclient.TestClient(app)` and sent DELETE requests for `/api/clipper/jobs/..`, `/api/clipper/jobs/%2E%2E`, `/api/clipper/jobs/..%2F..`, and `/api/clipper/jobs/%2Fetc`, with the clipper root and deletion function redirected to a temporary directory/recorder. It stopped before making requests because the FastAPI package is unavailable.

## Next action

Run the probe in the project test environment with FastAPI installed. Only proceed with the fix and regression tests if the live router delivers a job ID that escapes the resolved clipper directory.
