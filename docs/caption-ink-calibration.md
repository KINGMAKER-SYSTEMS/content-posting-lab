# Caption ink check: calibration

This is the evidence behind the numbers in `services/post_render.py`
(`CAPTION_INK_MIN_SHARE`, `CAPTION_INK_CONTRAST`, `CAPTION_INK_MIN_PIXELS`,
`CAPTION_BOX_MIN_MATCH`). The raw measurements are in
`tests/fixtures/caption_ink_calibration/real-render-shares.csv`, and
`tests/test_post_render.py::test_the_calibration_evidence_in_the_repo_supports_the_thresholds`
fails if the thresholds stop agreeing with them.

## What the check does

After the final video is encoded, `render_post` cuts the QA frame at the middle of the
video. It also cuts a reference frame: the same source frame at the same time, through the
same delivery graph, with the caption's box or highlight drawn but the words left out (the
bare source frame when the style has no box). For box looks it cuts the bare source frame
too.

- **What is behind the words.** The box colour where the box really differs from the source
  (more than 24 levels), the bare source frame everywhere else. A box that hardly differs from
  the picture is no evidence of anything, so an unchanged source pixel can never count as ink.
  Where the words are judged against the bare source because of a near box, a pixel scores the
  lower of two judgements: how far it moved from the source toward the words, and how far it
  moved from the box toward the words (unchanged footage scores nothing on the first, a box
  with no words nothing on the second). A pixel also needs 48 levels of contrast against both.
- **Words.** Where the words would change what is behind them by more than 48 levels, the
  burned frame must have moved at least 40% of the way toward the words' colour, on average.
  Fewer than 64 such pixels means the words cannot be told apart from what is directly behind
  them: `CAPTION_LOW_CONTRAST` (white text, no outline, on a white clip; text in its own box's
  colour). Enough contrast but no change: `CAPTION_NOT_DRAWN`.
- **Box.** A box or highlight pixel counts as shown only if the burned colour is nearer the
  box colour than the bare source frame's colour at that pixel. Pixels within 24 levels of the
  source are left out of the tally (a black box over a black clip burns the source colour and
  is a real caption). At least 60% of the counted box pixels must be shown, or
  `CAPTION_NOT_DRAWN`. A box that is not in the burned frame is judged first, before the words.
- A box alone cannot pass the words test (what is behind the words already has the box). White
  words on a dark box over a white clip pass, because they are measured against the box.

Earlier versions failed in turn, and each failure is a regression test: colour matches pass
white-on-white with no overlay; plain change-against-source passes a gray box over a black
clip with no words; a plain colour match calls light-grey footage a `#c0c0c0` box; and a near
box let unchanged footage score as word ink (flat `#e0e0e0` source, `#c8c8c8` box, white
words, no overlay scored 0.436). Thin fonts scored only 0.25 on a colour match even when drawn.

## Method

Real renders through `render_post` on seeno (real ffmpeg and the repo's real fonts), measured
with the refusal decision switched off so every score is recorded, then given the verdict the
real decision gives.

- 12 clips: the repo's real 7 s portrait video, the same video at four times the speed (a
  fast-motion real clip), white, near white (`0xf2f2f2`), light gray (`0xf0f0f0`), mid gray
  (`0xa0a0a0`), bright yellow, black, gray, red, per-frame random noise, test pattern.
- 3 fonts: 12pt Light, 16pt Bold, 36pt Black.
- 18 looks: white with black outline, white, black, yellow and blue with no outline, red with
  white outline, white with a 1 px outline, and eleven with a box or highlight (white on black
  box, black on white box, white on gray box, black on yellow highlight, white on black
  highlight, yellow on dark box, white on white box, and four mid-grey ones).
- 2 sizes: 12 pt and 24 pt.
- 3 modes: `drawn` (the real render); `bare` (the video was encoded with a transparent overlay,
  so nothing is burned in); `boxonly` (box looks only: the video was encoded with the box and
  without the words).

3,384 renders: 1,296 drawn, 1,296 bare, 792 box-only. The CSV also records, for every bare
render, the codec drift: the largest difference between the burned QA frame and the bare source
frame over the caption area (99th percentile and maximum).

Synthetic frames cover the rest. `test_class_guard_nothing_missing_from_the_video_passes_and_a_real_render_is_never_not_drawn`
runs every combination of source level, box level (both sides of the 24-level boundary, light on
light and dark on dark), words colour and outline, and asserts both directions per cell: nothing
burned in is refused, and a real render is shown or low contrast, never not-drawn.

## Results

| | Result |
|---|---|
| Drawn, words visible against what is behind them (1,200 renders) | all 1,200 pass; none low contrast, none not drawn |
| Words share of those, no box: lowest | 0.559 (12pt Light, no outline, on noise) |
| Words share, box looks: lowest | 0.914 |
| Box match, drawn box looks: lowest | 0.986 |
| Drawn, words the colour of what is behind them (96 renders) | all 96 `CAPTION_LOW_CONTRAST` |
| Bare (nothing burned in) and box-only (2,088 renders) | 0 pass; 1,974 `CAPTION_NOT_DRAWN`, 114 `CAPTION_LOW_CONTRAST` |
| Bare, no-box looks, words share: highest on real or flat clips | 0.016 |
| Bare, no-box looks, words share: highest on the noise clip | 0.286 |
| Broken box looks whose words share is 0.4 or more | 400 renders; every one has box match 0.305 or less |

Moving the words to be judged against the bare source where a box is near the picture changed
no verdict on a good render: 0 of the 1,200 visible drawn renders became low contrast.

Codec drift (burned frame against bare source, caption area; largest over the matrix): real
portrait 99th percentile 4, maximum 13; the same video at four times the speed
5 and 16; flat clips 0 to 2; the synthetic test pattern 13 and 32; random noise
193 and 255. An unchanged pixel drifting by d scores d / contrast against the gate, and the
least contrast measured is 48, so drift under 48 x 0.40 = 19 levels can never pass as ink. Real
footage (including fast motion) stays under it; random noise does not and is held by its words
share instead.

By font, lowest drawn words share: 12pt-Light 0.559, 16pt-Bold 0.715, 36pt-Black 0.783. By clip: black 0.794, bright-f2 0.658, bright-yellow 0.612, gray 0.579, light-gray-f0 0.652, mid-gray-a0 0.587, noise 0.559, portrait 0.578, portrait-fast 0.578, red 0.606, testsrc2 0.674, white 0.684.

## Limits

- It proves the words and box changed the picture, not that the caption is easy to read.
  Text close in colour to a busy picture can still be hard to read.
- One frame (the midpoint) is measured. A clip that is white exactly behind a no-outline white
  caption at that frame is refused even if other frames are fine.
- The captions are one fixed three-line text. Framed bands are covered by unit tests (16:9,
  4:3), not by this matrix.
- The fast-motion clip is the real portrait sped up, not footage shot fast; the test pattern
  is a harsher moving case.
- Cost of the reference: about 50 ms of ffmpeg, plus about 30 ms more for a box or highlight
  look (the box-only PNG and a second ffmpeg input), plus one more bare-frame cut (about 50 ms)
  for box looks, on a loaded host.
