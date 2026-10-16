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
bare source frame when the style has no box).

- **Words.** Where the words would change the reference by more than 48 levels, the burned
  frame must have moved at least 40% of the way toward the words' colour, on average.
  Fewer than 64 such pixels means the words cannot be seen against what is directly behind
  them: `CAPTION_LOW_CONTRAST` (white text, no outline, on a white clip; text in its own
  box's colour). Enough contrast but no change: `CAPTION_NOT_DRAWN`.
- **Box.** A box or highlight must show its own colour in at least 60% of its pixels in the
  burned frame, or `CAPTION_NOT_DRAWN`.
- A box alone cannot pass the words test (the reference already has the box). White words on
  a dark box over a white clip pass, because they are measured against the box.

Measuring against the plain source frame, or counting colour matches, was tried and failed:
colour matches pass white-on-white with no overlay; plain change-against-source passes a
gray box over a black clip with no words (score 0.50), and refuses readable white-on-black-box
over a white clip. Thin fonts scored only 0.25 on a colour match even when drawn.

## Method

Real renders through `render_post` on seeno (real ffmpeg and the repo's real fonts), measured
with the refusal thresholds switched off so every score is recorded, then given the verdict
the real thresholds would give.

- 9 clips: the repo's real 7 s portrait video, white, near white (`0xf2f2f2`), bright yellow,
  black, gray, red, per-frame random noise, test pattern.
- 5 fonts: 12pt Light, 16pt Regular, 12pt Medium, 16pt Bold, 36pt Black.
- 14 looks: white with black outline, white, black, yellow and blue with no outline, red with
  white outline, white with a 1 px outline, and seven with a box or highlight (white on black
  box, black on white box, white on gray box, black on yellow highlight, white on black
  highlight, yellow on dark box, white on white box).
- 2 sizes: 12 pt and 24 pt.
- 3 modes: `drawn` (the real render); `bare` (the video was encoded with a transparent overlay,
  so nothing is burned in); `boxonly` (box looks only: the video was encoded with the box and
  without the words).

3,150 renders: 1,260 drawn, 1,260 bare, 630 box-only.

## Results

| | Result |
|---|---|
| Drawn, words visible against what is behind them (1,140 renders) | all 1,140 pass |
| Words share of those: lowest | 0.552 (12pt Light, blue, no outline, on noise) |
| Words share, box looks: lowest | 0.917 |
| Box match, drawn box looks: lowest | 0.947 |
| Drawn, words the colour of what is behind them (120 renders) | all 120 `CAPTION_LOW_CONTRAST` |
| Bare (nothing burned in) and box-only (1,890 renders) | 0 pass; 1,680 `CAPTION_NOT_DRAWN`, 210 `CAPTION_LOW_CONTRAST` |
| Bare, no-box looks, words share: highest on real or flat clips | 0.016 |
| Bare, no-box looks, words share: highest on the noise clip | 0.299 |
| Bare, box looks: words share of 0.4 or more | 384 of 540; every one has box match 0.451 or less |
| Box-only, words share | 0.000 in every render |

The words share gap is 0.40 against 0.552 on one side and 0.299 on the other (the noise clip
is the worst case: lossy encoding of random noise drifts toward the middle grey). The box gap
is 0.60 against 0.451 and 0.947.

By font, lowest drawn words share: 12pt Light 0.552, 16pt Regular 0.616, 12pt Medium 0.663,
16pt Bold 0.715, 36pt Black 0.761. By clip: noise 0.552, portrait 0.578, gray 0.579, red 0.606,
bright yellow 0.612, near white 0.658, test pattern 0.674, white 0.684, black 0.794.

## Limits

- It proves the words and box changed the picture, not that the caption is easy to read.
  Text close in colour to a busy picture can still be hard to read.
- One frame (the midpoint) is measured. A clip that is white exactly behind a no-outline white
  caption at that frame is refused even if other frames are fine.
- The captions are one fixed three-line text. Framed bands are covered by unit tests (16:9,
  4:3), not by this matrix.
- Cost of the reference: about 50 ms of ffmpeg, plus about 30 ms more for a box or highlight
  look (the box-only PNG and a second ffmpeg input), on a loaded host.
