# Fleet visual admission review receipt — 2026-09-12

## Verified scope

The Worker actively requests a decision from the authenticated Lab endpoint.
The Lab resolves only its durable page/job/index artifact, verifies SHA and byte
count, schedules a background whole-job sweep, and returns a current cached
verdict only after rehashing the artifact. The decision is bound to the scanner
algorithm, job, index, page and exact bytes. Restart markers, per-sweep identity
fences and unavailable retry cooldown preserve paid work without a long browser
request. A native decoder visits every video frame. At most two native-frame
Tesseract PSM12 processes run concurrently; GLM Flash sees up to 16 native frames.
The bounded decoder/OCR/model failures never become clean decisions.

## Reverified independent findings

- Opus B F1 (two GLM samples falsely prove all-frame OCR): dismissed. It combines
  the two independent detector roles and did not inspect Lab. `_scanned_frames`
  dispatches each decoded frame through `_ocr`; completed coverage must equal
  ffprobe's count. GLM indices describe only the additional vision samples.
  The native transient-text fixtures and real HTTP bridge reject text even
  when the fixture model returns clean. Authenticated server evidence is the
  intended trust boundary, not an arbitrary caller-supplied verdict.
- Opus B F7 (unsampled thumbnail frame): dismissed as stated. The thumbnail is
  extracted from the same pre-caption video at 0.25 seconds using only ffmpeg
  scaling and receives its own SHA. That video frame is included in OCR's full
  coverage. This is not a claim that arbitrary user-supplied thumbnails are safe.
- Worker peer review's unbounded model response: confirmed and fixed. The
  response is now read in 4096-byte chunks, with a 64KiB ceiling before accumulation.
- Worker peer review's sideways transient blind spot: confirmed and fixed for
  cardinal orientations with Tesseract PSM12 and native 0/90/180/270 fixtures.
- Worker peer review’s stale wrong-binding cache: confirmed and fixed. The
  background sweep now checks page, job and index as well as bytes and algorithm;
  a regression verifies an incorrect page binding is rescanned and repaired.
- Native throughput: original four-pass design failed closed at 240 seconds after
 52/210 frames. Two-worker orientation-aware replacement completed 210/210 frames
  at 1080x1920 in 228.38 seconds with a fixture GLM result. A real 9-second 270-frame
  run completed all OCR in 211.53 seconds total but the live model evidence was
  unavailable. The asynchronous scan budget is 420 seconds; no request waits for
  the sweep. Non-Latin, arbitrary-angle and adversarial recall remain bounded.
- Opus C shared-source planner gap: confirmed and repaired with the Worker's
  bounded global original-window exclusion feed before rendering. Lab also
  now skips the first 60 seconds of an original whole master. Existing pretrimmed
  masters retain their own offset. Worker atomic admission remains necessary
  for concurrent jobs; a selection snapshot alone cannot grant reservation.
- Permanent reservations before a rejected scan: Worker peer review confirmed
  and moved the reservation after clean evidence, before the first bucket put.

## Evidence and limits

`artifacts/visual-admission-evidence/worker-lab-http-e2e.json` records the real
Worker adapter → HTTP Lab → native OCR → Worker admission test. The clean 5s
fixture produced the sole mock bank write; a one-frame text fixture was refused.
GLM and the bucket are explicitly fixture doubles in that test.

The 9s native benchmark and separate live calls retain unavailable evidence;
Z.AI repeatedly returned HTTP 429/provider 1305 temporary overload. One earlier
single-image clean call succeeded. There is no proven live positive/negative
GLM pair, so paid generation must remain disabled pending a successful live
vendor pair and production operator verification.

The source-window feed and pre-R2 final guard belong to the Worker integration
commit. This Lab branch and the Ocean vision branch do not deploy themselves.
Production media, D1 data and the user's Ocean branch were not mutated here.
Project Clipper R2 mirrors (`clips/`) are scratch downloads, not the Worker's
`PAGE_BUCKETS` admission door; post-caption episode archives also remain outside
this pre-caption ready-clip gate. No claim is made that those unrelated files
cannot contain text.
