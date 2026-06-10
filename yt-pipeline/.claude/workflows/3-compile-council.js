export const meta = {
  name: 'abn-vo-assembly',
  description: '5 compilers glue aligned clips to VO audio per segment; 4-expert council reviews; fixes routed back',
  phases: [
    { title: 'Compile', detail: '5 agents, 2 segments each, clips + audio -> aligned segment cuts' },
    { title: 'Council', detail: '4 expert editors review all 10 cuts' },
    { title: 'Fix', detail: 'council notes routed to owning compiler' },
    { title: 'Signoff', detail: 'final QC re-audit of fixed cuts' },
  ],
}

const REPO = '/Users/risingtidesdev/dev/content-posting-lab'
const OUT = REPO + '/yt-pipeline/src/animations'
const ASM = OUT + '/assembly'
const AUDIO = '/Volumes/T9/agenticbuildernews/agenticnews_assets/ep_154d0920_origaudio.m4a'

const SEG_FOLDER = {
  0: 'RalphAgent1-sonnet', 1: 'RalphAgent2-sonnet', 2: 'RalphAgent3-sonnet',
  3: 'RalphAgent4-opus', 4: 'RalphAgent5-opus', 5: 'RalphAgent6-opus',
  6: 'RalphAgent7-fable', 7: 'RalphAgent8-fable', 8: 'RalphAgent9-haiku', 9: 'RalphAgent10-haiku',
}

const COMPILERS = [
  { n: 1, model: 'fable', segs: [0, 5] },
  { n: 2, model: 'fable', segs: [6, 7] },
  { n: 3, model: 'opus', segs: [1, 8] },
  { n: 4, model: 'opus', segs: [3, 4] },
  { n: 5, model: 'sonnet', segs: [2, 9] },
]

const COMPILE_SCHEMA = {
  type: 'object',
  required: ['compiler', 'segments'],
  properties: {
    compiler: { type: 'string' },
    segments: {
      type: 'array',
      items: {
        type: 'object',
        required: ['segment', 'mp4', 'durationSec', 'syncProof', 'iterations'],
        properties: {
          segment: { type: 'number' },
          mp4: { type: 'string' },
          durationSec: { type: 'number' },
          syncProof: { type: 'string' },
          iterations: { type: 'number' },
          notes: { type: 'string' },
        },
      },
    },
  },
}

const COUNCIL_SCHEMA = {
  type: 'object',
  required: ['reviewer', 'lens', 'perSegment', 'summary'],
  properties: {
    reviewer: { type: 'string' },
    lens: { type: 'string' },
    perSegment: {
      type: 'array',
      items: {
        type: 'object',
        required: ['segment', 'verdict', 'issues'],
        properties: {
          segment: { type: 'number' },
          verdict: { type: 'string', enum: ['ship', 'fix'] },
          issues: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    summary: { type: 'string' },
  },
}

function compilePrompt(c) {
  const segList = c.segs.join(', ')
  return `You are Compiler${c.n}-${c.model}, a broadcast finishing editor on the ABN assembly line. Max 10 build-verify iterations total. Your segments: ${segList}. For EACH segment N you own, produce ${ASM}/s<N>_aligned.mp4 — the segment's voiceover audio with the five retimed CSS animation clips playing at their exact aligned windows. mkdir -p ${ASM} first. Own ONLY your segments' output files.

INPUTS (read all before building):
- ${OUT}/segments.json — word grid per segment: words[{w,t}] are segment-grid seconds; durationSec.
- ${OUT}/AUDIO_OFFSETS.json — maps grid time -> time in the original episode audio (${AUDIO}). Normal segments: one audioStart (origaudio time of grid t=0). SEGMENT 7 IS SPECIAL: piecewise[] gives two ranges (gridFrom/gridTo -> audioOffset); read its note.
- ${OUT}/<agentFolder>/ALIGN.json — the five clip windows {video, voStart, voEnd} in grid seconds. Agent folders: ${c.segs.map(s => 's' + s + ' -> ' + OUT + '/' + SEG_FOLDER[s]).join(' ; ')}. Clips are <agentFolder>/VideoN/final.mp4 (1920x1080 yuv420p 24fps, duration == voEnd-voStart).

BUILD (per segment):
1. cutBase = grid time where your cut starts: 0.0 for normal segments; for segment 7 use 21.34 (its first clip's voStart — the piecewise HEAD audio offset applies from there, even though the piecewise range nominally starts at 21.84; extend the head offset back to 21.34). cutEnd = min(durationSec, last clip voEnd + 2.0). Video timeline position of clip k = voStart_k - cutBase.
2. VIDEO TRACK: clips at their positions, hard cuts. Gaps: lead gap (cutBase -> first voStart) = freeze first clip's frame 0; between clips = freeze previous clip's LAST frame; tail = freeze last clip's last frame. Build gap fills with ffmpeg (extract frame, loop it for the gap duration at 24fps yuv420p), then concat parts in order. Keep everything 1920x1080 yuv420p 24fps. Re-encode concat parts consistently (libx264, crf 18, preset medium) so concat is clean.
3. AUDIO TRACK: normal segments: ffmpeg -ss (audioStart + cutBase) -t (cutEnd - cutBase) -i ${AUDIO} -> aac 48k. Segment 7: two pulls per piecewise (head: offset+21.84 for grid 21.84-30.0; body: offset+30.0 for grid 30.0-cutEnd), concat with a 20ms acrossfade at the join so it does not pop.
4. MUX: -c:v from your video track, audio aac 192k. Video and audio length must match within 0.05s (pad/trim audio with apad/atrim if a frame boundary rounds).

VERIFY (every iteration; this is where your 10 iterations go):
- ffprobe: 1920x1080, yuv420p, 24/1, has aac audio, format duration == (cutEnd - cutBase) within 0.1s, v and a stream durations within 0.05s of each other.
- WORD-LANDING: pick 2 spoken on-screen words from DIFFERENT clips (find them in the clip's index.html const P + segments.json). Expected cut time tw = word.t - cutBase. Extract frame at tw+0.2: word must be visibly revealed.
- AUDIO-SIDE: for the same 2 words, ffmpeg -ss (tw-1) -t 2.5 -i s<N>_aligned.mp4 -vn -ac 1 -ar 16000 /tmp/asm_check.wav, then: whisper /tmp/asm_check.wav --model tiny.en --output_format txt --output_dir /tmp --language en — the transcript must contain the expected word(s). This proves picture AND sound agree.
- Listen for the segment-7 audio join: extract 4s around grid t=30 and whisper it — the sentence must read continuously.
If any check fails: diagnose, fix the build, re-run. Do not ship a cut whose proof failed.

Write ${ASM}/s<N>_NOTES.md per segment: cutBase, cutEnd, clip placements, gap fills, audio pulls, proof results. Return structured: compiler name, per segment {segment, mp4 abs path, durationSec (ffprobe), syncProof (words tested + result), iterations, notes}.`
}

const COUNCIL = [
  {
    key: 'retention', model: 'opus', title: 'Retention & Pacing Editor',
    brief: 'You are a senior YouTube retention editor (8 years cutting tech channels past 1M subs). Judge each cut as a viewer: does frame 1 hook? Does the energy carry? Find every dead stretch — any frozen/static span over 4 seconds is a retention leak: name its timestamp range. Judge cut rhythm against the narration beats, whether reveals create open loops, and whether the segment ends with momentum. Use ffmpeg frame sampling (1 fps contact strips or frames every 2-3s) to actually watch the flow.',
  },
  {
    key: 'syncqc', model: 'fable', title: 'Sync & Technical QC Editor',
    brief: 'You are a broadcast QC specialist with a ruthless ear. For EVERY cut: sample 3 spoken on-screen words spread across the duration (different ones than the compiler proved — read their NOTES.md to see what they tested, then pick others from segments.json). For each: extract the frame at expected time AND whisper the surrounding 2.5s audio; fail any reveal more than 0.4s off the spoken word. Check A/V stream duration match, listen at every gap boundary for audio pops/clicks (extract and inspect), verify segment 7 reads as one continuous sentence across its audio join, check loudness consistency across all 10 cuts (ffmpeg ebur128).',
  },
  {
    key: 'seo', model: 'sonnet', title: 'SEO & Packaging Strategist',
    brief: 'You are a YouTube SEO strategist for dev/AI channels. Watch each cut (frame sampling + the word grid in segments.json) and produce the packaging layer: a chapter map (segment -> chapter title with exact timestamp, keyword-front-loaded, search-intent phrasing), 3 title options for a compilation video of these cuts, the keyword moments inventory (where model names / numbers / claims appear on screen — these are rewind/share magnets), and per-segment thumbnail frame picks (give the exact ffmpeg -ss extraction command for each). Write your full report to ' + ASM + '/review/PACKAGING.md. A segment only fails your lens if its on-screen type misses an obvious searchable hook the script offers.',
  },
  {
    key: 'social', model: 'opus', title: 'Social Clips Editor',
    brief: 'You are a shorts/reels editor who clips long-form into viral verticals. For each cut: find the strongest self-contained 15-45s sub-clip (hook + payoff within the window), check whether key type survives a center 9:16 crop (extract frames, judge whether critical text falls outside the center 608px column — note which cuts are crop-safe and which need reframing), and rate mobile readability of the smallest on-screen text. Write your clip list with exact in/out timestamps to ' + ASM + '/review/SHORTS.md. Fail a segment only for unreadable-at-mobile type or a hook that takes longer than 3 seconds to land.',
  },
]

function councilPrompt(r) {
  return `${r.brief}

You sit on the 4-expert review council for the ABN aligned segment cuts. Max 10 review iterations. The 10 cuts: ${ASM}/s0_aligned.mp4 ... s9_aligned.mp4 (some indexes may map to different source folders — see ${ASM}/*_NOTES.md). Reference material: ${OUT}/segments.json (word grid + scripts), ${OUT}/MANIFEST.md (what each clip is), ${OUT}/AUDIO_OFFSETS.json, compiler notes in ${ASM}/. mkdir -p ${ASM}/review first.

Review ALL 10 cuts through YOUR lens only — the other three council seats cover the rest. Be a professional: concrete, timestamped, actionable. Every 'fix' verdict must carry issues a compiler can execute mechanically (timestamp + what is wrong + what right looks like). Do not fail a cut for matters outside your lens.

Return structured: reviewer (your title), lens key, perSegment [{segment, verdict ship|fix, issues[]}], summary (3-5 sentences, the state of the batch through your lens).`
}

function fixPrompt(c, segIssues) {
  const lines = c.segs.filter(s => segIssues[s] && segIssues[s].length)
    .map(s => `SEGMENT ${s}:\n` + segIssues[s].map(x => '  - ' + x).join('\n')).join('\n')
  return `You are Compiler${c.n}-${c.model} (fix pass), back on your cuts. Max 10 iterations. The review council flagged:\n${lines}\n\nFix these in ${ASM}/s<N>_aligned.mp4 (rebuild the affected parts — clip placement, gap fills, audio joins, or re-pull audio — using the same inputs and method as before: ${OUT}/segments.json, ${OUT}/AUDIO_OFFSETS.json, ${OUT}/<agentFolder>/ALIGN.json). Re-run your full verification (ffprobe, word-landing frames, whisper audio checks) after each fix. Update s<N>_NOTES.md with a FIXES section. Return: per segment, what changed and the re-verification results.`
}

log('Assembly line: 5 compilers -> 10 aligned segment cuts')

phase('Compile')
const compiled = await parallel(COMPILERS.map(c => () =>
  agent(compilePrompt(c), { label: `compile:${c.n}-${c.model}`, phase: 'Compile', model: c.model, schema: COMPILE_SCHEMA })
))

const okCompiled = compiled.filter(Boolean)
log(`Compile done: ${okCompiled.length}/5 compilers returned`)

phase('Council')
const reviews = await parallel(COUNCIL.map(r => () =>
  agent(councilPrompt(r), { label: `council:${r.key}`, phase: 'Council', model: r.model, schema: COUNCIL_SCHEMA })
))

const okReviews = reviews.filter(Boolean)
const segIssues = {}
for (const rv of okReviews) {
  for (const ps of rv.perSegment) {
    if (ps.verdict === 'fix' && ps.issues.length) {
      segIssues[ps.segment] = (segIssues[ps.segment] || []).concat(ps.issues.map(i => `[${rv.lens}] ${i}`))
    }
  }
}
const failingSegs = Object.keys(segIssues).map(Number)
log(`Council verdicts in: ${failingSegs.length} segments flagged (${failingSegs.join(', ') || 'none'})`)

phase('Fix')
let fixed = []
if (failingSegs.length) {
  const needFix = COMPILERS.filter(c => c.segs.some(s => failingSegs.includes(s)))
  fixed = await parallel(needFix.map(c => () =>
    agent(fixPrompt(c, segIssues), { label: `fix:${c.n}-${c.model}`, phase: 'Fix', model: c.model })
  ))
}

phase('Signoff')
let signoff = null
if (failingSegs.length) {
  signoff = await agent(
    `Final QC signoff on the fixed ABN segment cuts in ${ASM}: segments ${failingSegs.join(', ')} were rebuilt after council notes. For each: ffprobe sanity (1920x1080 yuv420p 24fps, aac, A/V durations within 0.05s), one fresh word-landing check (frame + whisper on a word NOT previously tested — pick from ${OUT}/segments.json), and confirm the specific council issues (listed in each s<N>_NOTES.md FIXES section) are actually resolved by inspecting the relevant timestamps. Return per segment: pass/fail + one line of evidence.`,
    { label: 'signoff', phase: 'Signoff', model: 'fable' }
  )
}

return {
  compiled: okCompiled.map(c => ({ compiler: c.compiler, segments: c.segments.map(s => ({ segment: s.segment, mp4: s.mp4, durationSec: s.durationSec, iterations: s.iterations, syncProof: s.syncProof })) })),
  council: okReviews.map(r => ({ reviewer: r.reviewer, lens: r.lens, summary: r.summary, flagged: r.perSegment.filter(p => p.verdict === 'fix').map(p => p.segment) })),
  fixedSegments: failingSegs,
  fixResults: fixed,
  signoff: signoff,
}
