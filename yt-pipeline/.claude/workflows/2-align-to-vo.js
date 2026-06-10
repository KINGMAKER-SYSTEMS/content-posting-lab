export const meta = {
  name: 'ralph-vo-align',
  description: 'Retime all 50 CSS animation videos to their exact VO windows with word-level beat alignment',
  phases: [
    { title: 'Retime', detail: 'duration = VO window, reveals land on spoken words' },
    { title: 'Verify', detail: 'frame-extraction proof that words land on time' },
    { title: 'Fix', detail: 'repair rejected alignments' },
  ],
}

const REPO = '/Users/risingtidesdev/dev/content-posting-lab'
const OUT = REPO + '/yt-pipeline/src/animations'
const AGENTS = [
  { n: 1, model: 'sonnet', seg: 0 },
  { n: 2, model: 'sonnet', seg: 1 },
  { n: 3, model: 'sonnet', seg: 2 },
  { n: 4, model: 'opus', seg: 3 },
  { n: 5, model: 'opus', seg: 4 },
  { n: 6, model: 'opus', seg: 5 },
  { n: 7, model: 'fable', seg: 6 },
  { n: 8, model: 'fable', seg: 7 },
  { n: 9, model: 'haiku', seg: 8 },
  { n: 10, model: 'haiku', seg: 9 },
]

const RETIME_SCHEMA = {
  type: 'object',
  required: ['agent', 'segment', 'videos'],
  properties: {
    agent: { type: 'string' },
    segment: { type: 'number' },
    videos: {
      type: 'array',
      items: {
        type: 'object',
        required: ['name', 'voStart', 'voEnd', 'renderedDur', 'beatProof'],
        properties: {
          name: { type: 'string' },
          voStart: { type: 'number' },
          voEnd: { type: 'number' },
          renderedDur: { type: 'number' },
          beatProof: { type: 'string' },
        },
      },
    },
    notes: { type: 'string' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['verdict', 'perVideo', 'summary'],
  properties: {
    verdict: { type: 'string', enum: ['ship', 'fix'] },
    perVideo: {
      type: 'array',
      items: {
        type: 'object',
        required: ['name', 'pass', 'issues'],
        properties: {
          name: { type: 'string' },
          pass: { type: 'boolean' },
          issues: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    summary: { type: 'string' },
  },
}

function retimePrompt(a) {
  const dir = `${OUT}/RalphAgent${a.n}-${a.model === 'haiku' && a.n === 9 ? 'haiku' : a.model}`
  return `You are RalphAgent${a.n}-${a.model}, returning to your own library folder for the VOICEOVER ALIGNMENT pass. Your folder: ${OUT}/RalphAgent${a.n}-${a.model} (if that exact name does not exist, find your folder with ls ${OUT} — it starts with RalphAgent${a.n}). You built 5 seekable CSS animation videos rooted in segment ${a.seg} of ${OUT}/segments.json. They currently run a fixed ~12s. Now they must lock to the real voiceover.

THE GOAL: each video becomes a drop-in clip for an exact window of the segment's VO. When the narrator speaks a word that is on screen, that word's reveal must land AT that moment.

FOR EACH of your 5 videos (Video1..Video5):
1. Decide its VO WINDOW [voStart, voEnd] in segment-relative seconds, using the word timestamps in ${OUT}/segments.json segment ${a.seg} (each word has s=start, e=end). The window must tightly span the script lines your video visualizes (check your NOTES.md + the on-screen copy in your index.html). Pad at most 0.5s before the first visualized word and 1.0s after the last. Windows of your 5 videos MUST NOT OVERLAP each other and must be in chronological VO order (Video order on disk does not need reordering — just their windows must be disjoint).
2. Set window.duration = (voEnd - voStart) exactly. Re-choreograph the internal timeline: every text element that quotes a VO word must reveal within ±0.15s of (that word's timestamp - voStart). Elements that are NOT spoken (chrome, ambient, attribution) keep free timing but must respect frame-0 composition (frame 0 fully composed — these clips hard-cut in). Keep ALL existing contract rules: window.seek(t) pure f(t), finite CSS animation durations and iteration counts (no 'infinite'), animation-fill-mode both, exactly ONE 'const P = {' block, fonts ../../fonts/, no wall-clock, no runtime randomness. Put your beat times in P (e.g. P.beats or per-element start values) so the timing stays parameterizable.
3. IMPORTANT pacing judgment: a 25-40s window is fine — let the piece breathe with the narration: stagger sub-reveals on their own word timestamps, keep ambient drift alive between beats (slow glows, grain, ticker), never leave a static dead frame longer than ~4s. If your original 12s choreography is too dense for the window, spread it; if too sparse, add held states with subtle motion. Do NOT linearly time-stretch everything — re-choreograph on the word grid.
4. Re-render: cd ${REPO} && NODE_PATH=frontend/node_modules node tools/seekable-html-video/render_seekable.cjs --input <abs index.html> --output <abs final.mp4> --fps 24 --contact-sheet <abs contact.jpg> --cleanup-frames  (one render at a time). Then ffprobe: duration must equal (voEnd - voStart) within 0.06s, still 1920x1080 yuv420p 24fps.
5. SELF-PROOF per video: pick 2 spoken on-screen words (one early, one late), compute their expected clip time tw = word.s - voStart, extract a frame at tw + 0.2 with: ffmpeg -y -ss <tw+0.2> -i final.mp4 -frames:v 1 /tmp/proof_a${a.n}_<video>_<word>.png — Read the PNG and confirm the word is visibly revealed (and was NOT already fully revealed in a frame extracted at tw - 0.8 unless it entered earlier by design). Record this as beatProof text (word, expected time, observed).
6. Update your NOTES.md (add an ALIGNMENT section) and write ${OUT}/RalphAgent${a.n}-*/ALIGN.json: {"segment": ${a.seg}, "videos": [{"video":"VideoN","voStart":x,"voEnd":y}...]}.

Return structured: agent, segment, per video {name, voStart, voEnd, renderedDur (ffprobe), beatProof}, notes.`
}

function verifyPrompt(a, ret) {
  const claimed = (ret && ret.videos ? ret.videos.map(v => `${v.name}[${v.voStart}-${v.voEnd}]`).join(' ') : '?')
  return `Adversarial alignment audit for the folder starting with RalphAgent${a.n} under ${OUT} (find exact name with ls). Claimed windows: ${claimed}. Segment ${a.seg} word timestamps: ${OUT}/segments.json. Try to REJECT:
1. ALIGN.json exists; its 5 windows are disjoint, chronological, inside [0, segment durationSec], and match the claimed windows.
2. Per video: ffprobe format duration == (voEnd - voStart) within 0.06s; 1920x1080 yuv420p 24/1.
3. WORD-LANDING PROOF (the core check — do it yourself, do not trust their beatProof): for each video pick 2 DIFFERENT spoken on-screen words than the builder likely used (read index.html const P + NOTES.md to see what's on screen; find those words in segments.json). For each: tw = word.s - voStart. Extract frames at max(0, tw - 0.8) and tw + 0.25 (ffmpeg -y -ss T -i final.mp4 -frames:v 1 /tmp/audit_a${a.n}_<video>_<i>.png), Read both: the word must be absent-or-entering in the early frame and clearly revealed in the late frame (exception: elements composed at frame 0 by design, e.g. headline plates — then check a mid-clip word instead). A reveal more than ~0.4s off the word timestamp = fail.
4. Frame 0 composed (extract -ss 0 frame) — no blank/near-blank open.
5. Contract still intact: exactly one 'const P = {'; no 'infinite' iteration counts; window.duration matches the window length in the source too.
verdict='ship' only if ALL five videos pass. Issues must be specific (video, word, expected vs observed).`
}

function fixPrompt(a, verify) {
  const issues = verify.perVideo.filter(v => !v.pass).map(v => v.name + ': ' + v.issues.join(' | ')).join('\n')
  return `You are RalphAgent${a.n}-${a.model} (alignment fix pass). Your folder starts with RalphAgent${a.n} under ${OUT}. The alignment audit rejected:\n${issues}\n\nFix the failing videos: adjust beat times in index.html (keep window.seek(t) pure, one const P, finite animations, fonts ../../fonts/) so every spoken on-screen word reveals within ±0.15s of (word.s - voStart) per ${OUT}/segments.json segment ${a.seg}; keep window.duration == voEnd - voStart; update ALIGN.json if windows changed (must stay disjoint + chronological). Re-render each fixed video (cd ${REPO} && NODE_PATH=frontend/node_modules node tools/seekable-html-video/render_seekable.cjs --input <abs index.html> --output <abs final.mp4> --fps 24 --contact-sheet <abs contact.jpg> --cleanup-frames), re-prove with frame extraction at the audited words, update NOTES.md. Return what changed and final ffprobe durations.`
}

log('Retiming 50 videos onto the VO word grid')

const results = await pipeline(
  AGENTS,
  a => agent(retimePrompt(a), {
    label: `retime:RalphAgent${a.n}:s${a.seg}`,
    phase: 'Retime',
    model: a.model,
    schema: RETIME_SCHEMA,
  }),
  (ret, a) => {
    if (!ret) return null
    return agent(verifyPrompt(a, ret), {
      label: `audit:RalphAgent${a.n}`,
      phase: 'Verify',
      schema: VERIFY_SCHEMA,
    }).then(v => ({ ret, verify: v }))
  },
  (r, a) => {
    if (!r || !r.verify || r.verify.verdict === 'ship') return r
    return agent(fixPrompt(a, r.verify), {
      label: `fix:RalphAgent${a.n}`,
      phase: 'Fix',
      model: a.model,
    }).then(f => ({ ...r, fixed: f }))
  }
)

const ok = results.filter(Boolean)
const shipped = ok.filter(r => r.verify && r.verify.verdict === 'ship').length
log(`Alignment done: ${ok.length}/10 agents, ${shipped} clean on first audit`)

return {
  agents: ok.map(r => ({
    agent: r.ret.agent,
    segment: r.ret.segment,
    auditVerdict: r.verify ? r.verify.verdict : 'no-audit',
    auditSummary: r.verify ? r.verify.summary : '',
    fixed: !!r.fixed,
    windows: r.ret.videos.map(v => ({ name: v.name, voStart: v.voStart, voEnd: v.voEnd, renderedDur: v.renderedDur })),
  })),
}