# Prepared rendering setup

The hosted `python main.py` service owns durable final-render jobs. It fetches
an already-approved source from the Worker's pending-artifact grant, renders the
selected caption once, and serves hash-bound final/QA/receipt files. It neither
claims a phone nor starts posting.

Configure these values only as part of an authorized service release:

| Setting | Purpose |
| --- | --- |
| `CONTENT_LAB_POST_RENDER_ROOT` | Absolute private directory on persistent storage; for the existing Railway `/app/projects` volume, use a dedicated directory such as `/app/projects/_post_render`. Blank leaves the preparation workers stopped. |
| `CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN` | `https://content-buckets.risingtidesviral.com`, the machine ingress for `/api/control-plane/v1/service/posting/v1/artifacts/.../source`. Required; no browser-origin fallback. |
| `CONTROL_PLANE_SERVICE_ID` | Existing Worker service identity, currently `gutenberg-shipstream`; it must retain the relevant scheduler lane and page grants. |
| `CONTROL_PLANE_SERVICE_SECRET` | The same machine credential held by the Worker. Provision it through the service secret store; never put its value in source, examples or logs. |
| `CONTROL_PLANE_TOKEN` | Existing inbound Lab bearer, matching Worker's `CONTENT_LAB_TOKEN`. Every render endpoint also requires the exact `X-RT-Page-Id`. |

Keep `CONTENT_LAB_CONTROL_PLANE_ORIGIN=https://control.risingtidesviral.com`
for existing `/api/control-plane/v1/pages/...` source-media reads. Those routes
use the browser ingress. Repointing this shared setting to the machine host
would break existing source generation. If the Lab's optional `APP_API_KEY`
middleware is enabled separately, the Worker also needs the matching
`CONTENT_LAB_API_KEY` for its `X-Api-Key` header.

The render root holds SQLite WAL state, locks, attempts and final artifacts.
Use one persistent volume with ordinary file-lock semantics; do not put the
root in the image's temporary filesystem or share it through storage lacking
those semantics. The service creates private subdirectories and starts two
bounded workers. Restart preserves the same remote job and recovers completed
outputs before rendering again.

The production Railway target is project `f512351a-26a8-4c33-98ef-eddd2f48c11e`,
environment `99ff0f13-254f-4bf2-960a-134f4f2e0e0f`, service
`a6dcd562-df4c-4ae1-a4e6-414f0d61b74c`. Use these explicit selectors when
inspecting release state; a checkout's default Railway link may name another
project. The application path is `/app`, its start command is `python main.py`,
and the persistent volume is mounted at `/app/projects`.

Before connecting a canary, integrate and validate current Lab and Worker
source, verify the actual running Lab revision and mounted root, and apply the
separately authorized Worker migration/configuration release. Worker preparation
remains off until explicitly enabled. A healthy `/api/health` response alone
does not establish that the render router, source grant or durable workers are
available. Verify authenticated page-bound status and an authorized preparation
job through actual final-byte admission before allowing phone execution.

Historical assets without genuine applied-video treatment evidence still need
regeneration. New generation emits that evidence; final rendering compares the
video treatment and adds only the caption and delivery encode. Neither setup
nor a successful render invents provenance for old bytes or authorizes a post.

Offline verification:

```sh
python3 -m pytest -q tests/test_post_render_jobs.py tests/test_post_render.py tests/test_source_treatment.py tests/test_control_plane_dossier_execution.py tests/test_control_plane_source_execution.py
```
