# Ready-clip visual admission

The Worker calls `POST /api/control-plane/v1/jobs/{job_id}/visual-admission/{index}`
with the existing control-plane bearer and exact `X-RT-Page-Id`. Its body is
exactly `{ "sha256": "<downloaded-byte-sha256>", "bytes": 123 }`. No URL, path,
model name or caller verdict is accepted. Content Lab resolves the durable job's
pre-caption artifact and rehashes its bytes before and after complete decode.
The response is `content-lab.visual-admission.v1`, bound to job, output index,
page, byte count and SHA. It is persisted in the job's `visualAdmission` map.

The first call immediately returns `verdict: unavailable, reason: scan_pending`
and schedules a bounded background sweep of every output in that paid job.
Poll the same endpoint: clean/text decisions are reused only for the exact
page/job/index/SHA/size and current scanner algorithm, after rehashing the
current file. No browser request waits for all OCR. A process-runtime marker
and heartbeat prevent duplicate sweeps and permit recovery after restart;
transient unavailable decisions have a 30-second retry cooldown. The Worker
keeps its paid remote job queued while scans are pending. Originals in the
source library are not scanned by this ready-clip admission workflow.

`CONTENT_LAB_VISION_API_KEY` supplies a Z.AI key with access to the standard API
at `https://api.z.ai/api/paas/v4/chat/completions`. Coding-plan text-only transport
is not a vision substitute. The fixed model is `glm-4.6v-flash`. Install ffmpeg,
ffprobe and Tesseract English data; the Dockerfile includes these tools.

One scan per host temp directory is permitted by an OS lock, with a per-process
thread semaphore. The hard limits are 128 MiB input, 600 frames, 4096×2160 pixels
per frame, 32 MiB total encoded vision input and 420 seconds. Frame decode streams one native RGB frame at a time;
no decoded-frame disk cache is retained. Each frame receives orientation-aware sparse-text OCR (Tesseract PSM12), with
at most two native-frame OCR subprocesses in flight.
Up to 16 evenly spaced native frames also reach the vision model. OCR detects
transient text even when it falls between model samples. A detected glyph rejects
the clip immediately. Clean requires complete frame coverage, clean OCR, and a
structured clean model decision. Unknown/failed/missing evidence never grants
admission. `scanner_busy_retry` is actionable temporary unavailability.

This is a bounded detector, not a mathematical proof of absence: arbitrarily rotated,
low-contrast, stylized, non-English, tiny or adversarial text can evade OCR, and
sampled vision has recall limits. Do not describe a clean decision as perfect
semantic detection. Full original source masters belong to the source library;
this gate is for ready clips, before our caption renderer. Applying it to a long
master returns `frame_budget_exceeded` rather than silently declaring it clean.

Validation: `pytest -q tests/test_visual_admission.py tests/test_production_image_imports.py`.
Native fixtures require ffmpeg/ffprobe/tesseract and are explicit skips if absent.
Live model evidence belongs separately from the hermetic fixture model responses.

## Source timeline planning

New whole-master cuts skip the first 60 seconds in the immutable source timeline.
A pretrimmed master retains its declared source offset; the 9-second disjoint
slot grid stays anchored to library time. Generation jobs accept up to 2000
`constraints.sourceWindowExclusions` records with canonical source identity and
half-open original start/end milliseconds, populated by the Worker from global
other-page reservations and historical evidence. Sourced capability entries include
`sourceIdentities` from the parsed current recipe, so the Worker restricts queries
to that recipe before applying the 2000-window limit. Unknown original identities
withdraw the capability. Overlapping slots are skipped
before rendering, including different encodings of the same canonical source.
Unknown historical timing can exclude the entire source. The Worker retains
its atomic final reservation guard for concurrent plans. Capability counts
without a specific job remain local estimates; job creation is exact and
returns `insufficient_inventory` if the global exclusions exhaust supply.

The generation request ceiling is 512000 bytes to carry this bounded feed; source
import requests retain their 16384-byte ceiling. Thumbnails are generated from
the same pre-caption video at 0.25 seconds with a scale-only ffmpeg transform,
and carry their own independently verified SHA. They do not introduce text.

`scripts/verify_visual_admission_e2e.py <worker-root>` exercises the actual
Worker adapter over HTTP, the Lab's asynchronous whole-job scan, native OCR,
and the Worker admission function. Its GLM reply and bucket are explicitly
fixture doubles; it proves clean/text routing, not live vendor availability.

## Commissioned crop groups

The producer checks the final recipe `production.controls.crop_mode` (with its
registered family default) before processing generated artifacts. Dual, triptych
and both require exactly 2, 3 and 5 candidates with matching modes and counts
and every unique index from zero. Missing or malformed groups fail the job.
Artifact serialization preserves a whole boundary group when requested quantity
is not a multiple of its size. Worker independently binds the group mode to the
immutable publication before any bucket write; visual rejection can still yield
an honestly partial admitted group.
