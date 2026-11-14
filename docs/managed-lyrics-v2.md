# Scoped managed Heavy Foot lyrics (draft integration)

This change adds `lyric-video`, a separate `lyrics_slideshows` / `lyric-edits`
format for `acct:burnt-mill-rd` only. It does not change the legacy lyric-edits,
meme, or UGC executor bytes or the static v1 prepared renderer.

The source program is the approved Tirana moving-v3 portrait: 20 original videos,
27 ordered moving windows, 20 seconds. Its executor SHA pins filenames, original
source SHA/bytes/duration, exact windows, order and focal crops. Source rendering
is silent and text-free. The ordinary managed treatment applies brightness0.85
once at speed1 and neutral additional crop. Final timed preparation verifies that
source treatment; it does not apply another grade, speed or crop.

An editor selects only the advertised scalar
`production.controls.lyricTimingSetId=burnt-heavy-foot-chorus-v1`. The immutable
server manifest/hash pins page, account, immutable Notion identity, Heavy Foot
7611482162773035025, source-program revision and seven full phrases over20s.
Arbitrary cue arrays, lyric text, project IDs or supplied timing hashes are not
Dossier controls. The catalog exposes the canonical text and immutable references
for a trusted coordinator; it is not permission to edit the manifest.

The existing authenticated durable `/api/control-plane/v1/post-renders` endpoint
accepts a discriminated `content-lab.post-render-request.v2` alongside unchanged
v1. V2 adds `lyric_timing_set_id`, `lyric_timing_sha256`, `sound_id`,
`source_program_id`, `source_program_sha256`. It requires canonical caption text
and exact immutable slot.sound.music_id. Each phrase uses the existing typed PNG
renderer and half-open intervals. Final output is silent1080x1920 H264/yuv420p,
600frames at30fps,20s, <=22MiB; official music is attached downstream. Actual decoded
first/last phrase frames must match typed glyph pixels. This is not proof of
phoneme alignment or official TikTok playback phase.

A real managed background execution emits `lyricTiming` only after media hashing.
The capsule binds the full page/account/Notion/sound/timing/source-program/source-
SHA/job/recipe/spec identities. Artifact reads require the stored producer capsule
to equal the current exact registered authority. Missing or stale evidence fails
closed; operator previews never receive fabricated managed receipts.

Remaining commissioning gates: Worker must retain and validate this real producer
capsule, choose the advertised canonical lyric text instead of a random static
caption, build renderer2 requests and admit v2 receipts. Physical phone identity,
normal publication, final full/image qualification and reviewed release are
separate gates. This branch does not commission a page, change flags, invoke a
paid render, deploy or assert posting readiness.

The previously executed EditorBay audio-bearing lyric QA remains a separate
page QA artifact; it is never used as a production source or official sound.
