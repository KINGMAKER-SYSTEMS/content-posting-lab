export const meta = {
  name: 'ralph-css-fanout',
  description: '10 agents x 5 script-rooted seekable CSS animation videos for ABN',
  phases: [
    { title: 'Build', detail: 'one agent per segment, 5 unique videos each' },
    { title: 'Verify', detail: 'adversarial audit per agent folder' },
    { title: 'Fix', detail: 'repair anything the audit rejected' },
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

const BUILD_SCHEMA = {
  type: 'object',
  required: ['agent', 'model', 'segment', 'videos', 'iterations'],
  properties: {
    agent: { type: 'string' },
    model: { type: 'string' },
    segment: { type: 'number' },
    iterations: { type: 'number' },
    videos: {
      type: 'array',
      items: {
        type: 'object',
        required: ['name', 'title', 'concept', 'mp4', 'scriptLines', 'durationSec', 'ffprobe'],
        properties: {
          name: { type: 'string' },
          title: { type: 'string' },
          concept: { type: 'string' },
          mp4: { type: 'string' },
          contact: { type: 'string' },
          scriptLines: { type: 'string' },
          durationSec: { type: 'number' },
          ffprobe: { type: 'string' },
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

function buildPrompt(a) {
  const dir = `${OUT}/RalphAgent${a.n}-${a.model}`
  return `You are RalphAgent${a.n} (model: ${a.model}) on the ABN visual-storytelling fleet. Credit yourself as "RalphAgent${a.n}-${a.model}" in every deliverable title. Your job: create FIVE unique, beautiful, production-grade seekable CSS animation videos for the Agentic Builder News YouTube channel, all rooted in YOUR assigned script segment.

YOUR SEGMENT: index ${a.seg} in ${OUT}/segments.json — read it FIRST. It has: title, sourceUrl, the full spoken script text, and word-level timestamps ({w, t} = word + the second it is spoken). This is REAL voiceover from a produced episode.

THE NON-NEGOTIABLE RULE — NO LOREM IPSUM: every word, number, name, and quote that appears on screen must be VERBATIM from your segment's script (or its title/sourceUrl domain for attribution chips). The animation choreography must follow the script's order — if the script says "five steps", the five steps appear in script order; if it says "1 million lines", that number counts up; if it's a quote, the words land as they are spoken (use the word timestamps to pace reveals). You are the visual storyteller: let the script drive how the animation unfolds. Inventing copy = instant failure.

READ BEFORE BUILDING (in this order):
1. ${OUT}/segments.json — your segment (index ${a.seg}).
2. ${REPO}/yt-pipeline/remotion/DESIGN.md — the Forge Signal design contract. Atoms are sacred: palette tokens, TikTok Sans 800/900 display + mono (JetBrains Mono with Menlo/SF Mono fallback is fine), extreme weight contrast, one accent voice per element (forgeRed #FF2B2F = editorial, signalCyan #7FD2FF = data/source — never both on one element), the banned-moves list (no gradient text, no left-edge accent stripes, no flat centered slides, no pure #000/#fff, no Inter).
3. ${REPO}/tools/seekable-html-video/templates/zine-slam/index.html — the authoring idiom (seek contract, const P, layering, paper texture tricks). Skim stat-shrine and press-tear in the same templates/ dir for variety. Your pieces must NOT clone these — they are idiom references, not skins.

SEEKABLE CONTRACT (the renderer samples frames at exact timestamps — break this and the render is garbage):
- window.duration = <seconds> and window.seek(t) defined; call seek(0) on load; EVERY visual state derived from t. No wall-clock time reads, no setInterval, no rAF-driven state, no runtime randomness affecting rendered output (precompute constants or use a seeded table built once at load), no hover/scroll triggers.
- CSS keyframe technique (preferred, this is the css-animations adapter pattern): declare finite animations, set animation-play-state: paused, and in seek(t) drive them with negative delay: el.style.animationDelay = (-t + startSec) + 's'. FINITE animation-duration and animation-iteration-count ONLY — never 'infinite' (looping motifs: compute iterations to cover window.duration). animation-fill-mode: both so held states are correct before/after.
- Exactly ONE block matching: const P = {...};  — all your text/numbers parameterized there so the factory can later refill it (the regex used is /const P = \\{.*?\\};/s, count 1 — so no OTHER 'const P = {' anywhere).
- Fonts: @font-face with url("../../fonts/TikTokSans36pt-Black.ttf") and url("../../fonts/TikTokSans16pt-Bold.ttf") (already at ${OUT}/fonts/). Never assume a font exists; never use Inter/Montserrat/Roboto.
- Frame 0 must already show composed content (back-date your first entrance) — a blank first frame fails QA.
- Duration per video: 8–14s. 1920x1080.

FIVE UNIQUE CONCEPTS: each video a DISTINCT visual concept — different layout, energy, and technique. Pull different beats from your script (the hook, the key number, the mechanism, the payoff, the verdict). Concept menu to riff on (invent beyond it; do not do five variations of one idea): kinetic type slam, count-up stat monument, spoken-word quote reveal paced by timestamps, terminal/code-stream motif, split-screen clash, news-ticker/wire crawl, step-mechanism diagram with drawn connectors, press-clipping collage, glitch-redacted security motif, data-readout HUD. Layered theatrics: 2-4 ambient decoratives (accent glows, hairline rules, grain, slow drift) — static backdrops read dead. Asymmetric layouts by default. This is loose iterative exploration, NOT a design system — take risks, keep the atoms.

OUTPUT LAYOUT (own ONLY this folder; never touch other agents' folders or any file outside it):
${dir}/Video1 ... ${dir}/Video5, each containing index.html, final.mp4, contact.jpg, NOTES.md.
NOTES.md per video: title line "RalphAgent${a.n}-${a.model} — <concept name>", the exact script lines used, duration, technique notes, and what you changed across iterations.

RENDER + QA LOOP (max 10 build-render-review iterations total across your 5 videos; budget at least one refinement pass for each):
cd ${REPO} && NODE_PATH=frontend/node_modules node tools/seekable-html-video/render_seekable.cjs --input <abs index.html> --output <abs final.mp4> --fps 24 --contact-sheet <abs contact.jpg> --cleanup-frames
Then: ffprobe -v error -select_streams v:0 -show_entries stream=width,height,pix_fmt,r_frame_rate -show_entries format=duration -of default=noprint_wrappers=1 <final.mp4>  (expect 1920/1080/yuv420p/24/1) AND Read the contact.jpg with your Read tool and JUDGE it: does it read as "a terminal that learned broadcast design" — newsroom urgency, extreme type contrast, layered depth? If it reads flat, centered-everything, slideshow-y, or AI-generic, it is NOT done — fix and re-render. Render videos one at a time (never parallel renders).

Return (as your structured output): agent name, model, segment index, iteration count, and per video: name (VideoN), title (must include RalphAgent${a.n}-${a.model}), concept, abs mp4 path, contact path, the script lines used, measured durationSec, and a one-line ffprobe summary. Plus brief notes on your strongest piece.`
}

function verifyPrompt(a, built) {
  const dir = `${OUT}/RalphAgent${a.n}-${a.model}`
  const claimed = (built && built.videos ? built.videos.map(v => v.name + ': ' + v.concept).join('; ') : 'unknown')
  return `Adversarial QA audit of ${dir} (5 videos claimed: ${claimed}). You are trying to REJECT these. For EACH VideoN folder:
1. Mechanical: final.mp4 exists and is >50KB; ffprobe shows 1920x1080, yuv420p, 24/1 fps, duration 6-16s.
2. Contract greps on index.html: exactly ONE match for 'const P = {'; zero matches for 'infinite' as an animation-iteration-count value; zero setInterval calls; zero wall-clock time reads; zero runtime randomness affecting rendered state (a seeded/precomputed-at-load constant table is acceptable ONLY if seek(t) output stays deterministic per t); window.duration and window.seek both defined; @font-face uses ../../fonts/ TikTok Sans (no Inter/Montserrat/Roboto, no Google Fonts link tag).
3. Script-rooted: open ${OUT}/segments.json segment ${a.seg}; verify the on-screen copy in index.html's const P comes VERBATIM from that segment's script/title (attribution chips from sourceUrl domain are fine). Any invented marketing copy or placeholder text = fail.
4. Visual: Read contact.jpg. Judge against ${REPO}/yt-pipeline/remotion/DESIGN.md banned moves: gradient text, left-edge accent stripes, everything-centered-equal-weight, flat gradient slides, two sans-serifs, red+cyan co-branding a single element, pure black/white, web-size tiny type. Also fail anything that reads as a flat slideshow frame rather than layered broadcast design (needs ambient depth: glows/rules/grain/texture).
5. Frame 0: leftmost contact-sheet panel must show composed content, not a blank/near-blank frame.
Be strict but concrete: every issue must name the video and the exact problem. verdict='ship' only if ALL five videos pass.`
}

function fixPrompt(a, verify) {
  const dir = `${OUT}/RalphAgent${a.n}-${a.model}`
  const issues = verify.perVideo.filter(v => !v.pass).map(v => v.name + ': ' + v.issues.join(' | ')).join('\n')
  return `You are RalphAgent${a.n}-${a.model} (fix pass) for ${dir}. The QA audit rejected these videos:\n${issues}\n\nFix ONLY the failing videos in place (index.html in each VideoN folder), keeping the seekable contract (window.duration, window.seek(t), one const P block, finite CSS animation durations/iteration-counts, fill-mode both, fonts from ../../fonts/, all on-screen copy verbatim from segment ${a.seg} in ${OUT}/segments.json, choreography in script order). Re-render each fixed video: cd ${REPO} && NODE_PATH=frontend/node_modules node tools/seekable-html-video/render_seekable.cjs --input <abs index.html> --output <abs final.mp4> --fps 24 --contact-sheet <abs contact.jpg> --cleanup-frames — then ffprobe (1920x1080 yuv420p 24/1) and Read the new contact.jpg to confirm the issue is actually gone. Update the video's NOTES.md with what you changed. Render one at a time. Return a summary of fixes and the final state of each video.`
}

log('Launching 10-agent fleet: 50 script-rooted CSS animation videos')

const results = await pipeline(
  AGENTS,
  a => agent(buildPrompt(a), {
    label: `RalphAgent${a.n}-${a.model}:s${a.seg}`,
    phase: 'Build',
    model: a.model,
    schema: BUILD_SCHEMA,
  }),
  (built, a) => {
    if (!built) return null
    return agent(verifyPrompt(a, built), {
      label: `audit:RalphAgent${a.n}`,
      phase: 'Verify',
      schema: VERIFY_SCHEMA,
    }).then(v => ({ built, verify: v }))
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
const totalVideos = ok.reduce((s, r) => s + ((r.built && r.built.videos) ? r.built.videos.length : 0), 0)
const shipped = ok.filter(r => r.verify && r.verify.verdict === 'ship').length
log(`Fleet done: ${ok.length}/10 agents returned, ${totalVideos} videos, ${shipped} folders passed audit clean`)

return {
  agents: ok.map(r => ({
    agent: r.built.agent,
    model: r.built.model,
    segment: r.built.segment,
    iterations: r.built.iterations,
    auditVerdict: r.verify ? r.verify.verdict : 'no-audit',
    auditSummary: r.verify ? r.verify.summary : '',
    fixed: !!r.fixed,
    videos: r.built.videos.map(v => ({ name: v.name, title: v.title, concept: v.concept, mp4: v.mp4, durationSec: v.durationSec, scriptLines: v.scriptLines })),
  })),
  totalVideos,
}