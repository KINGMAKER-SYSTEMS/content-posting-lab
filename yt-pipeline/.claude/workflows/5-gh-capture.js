export const meta = {
  name: 'gh-capture-fleet',
  description: 'Capture real page-nav recordings of GitHub repos (list-episode footage), verified per tool',
  phases: [
    { title: 'Capture', detail: 'scripted playwright nav per repo, recorded + re-encoded' },
    { title: 'Verify', detail: 'duration, content frames, repo identity visible' },
  ],
}

// args: { tools: [{rank, name, url, extraUrl?}], outDir }
// extraUrl: optional second page worth recording (docs page, examples dir, demo)
const REPO = '/Users/risingtidesdev/dev/content-posting-lab'
const CAPTURE = REPO + '/tools/gh-capture/capture_nav.cjs'
const A = (typeof args === 'string') ? JSON.parse(args) : (args || {})
const tools = A.tools || []
const OUT = A.outDir || (REPO + '/yt-pipeline/src/animations/ep3/footage')
if (!tools.length) throw new Error('args.tools required: [{rank, name, url}]')

const CAP_SCHEMA = {
  type: 'object', required: ['rank', 'files', 'verified'],
  properties: { rank: { type: 'number' }, files: { type: 'array', items: { type: 'string' } }, verified: { type: 'string' }, notes: { type: 'string' } },
}

log(`Capture fleet: ${tools.length} repos -> ${OUT}`)

const results = await pipeline(
  tools,
  t => agent(`Capture page-nav footage for "${t.name}" (rank ${t.rank} in the episode list). mkdir -p ${OUT} first.

1. PRIMARY: cd ${REPO} && NODE_PATH=frontend/node_modules node ${CAPTURE} --url ${t.url} --out ${OUT}/t${t.rank}_main.mp4 --seconds 32
2. ${t.extraUrl ? `SECONDARY: same command with --url ${t.extraUrl} --out ${OUT}/t${t.rank}_extra.mp4 --seconds 20` : `SECONDARY (your judgment): if the README scroll alone is visually thin, capture ONE more page that shows the tool's substance (docs page, examples/ dir on github, the project's site if it has a live demo) to ${OUT}/t${t.rank}_extra.mp4 --seconds 20. Find the URL yourself (web search ok). Skip if the main capture is rich.`}
3. VERIFY each capture: ffprobe (1920x1080 yuv420p 24/1, duration 15-45s); extract 3 spread frames and Read them — the repo/tool identity must be VISIBLE (name in header or README title), pages must be real content (fail on error pages, captcha, rate-limit walls, mostly-blank frames). If a capture fails verification: retry once with --seconds adjusted; if GitHub rate-limits, wait 30s (sleep via a bash loop is unavailable — re-run and rely on elapsed time) and retry. Max 5 attempts total.
4. If the page renders in light mode despite the dark colorScheme hint, note it (GitHub respects the hint when logged out — verify the frames are dark; light-mode footage is a verification FAIL since it will strobe inside our dark episode — retry once, then report honestly).

Return: rank, files (abs paths that passed), verified (one line: what is visible in the frames), notes.`,
    { label: `cap:t${t.rank}-${t.name.slice(0, 14)}`, phase: 'Capture', schema: CAP_SCHEMA })
)

const ok = results.filter(Boolean)
const total = ok.reduce((s, r) => s + r.files.length, 0)
log(`Capture done: ${ok.length}/${tools.length} tools, ${total} files`)
return { captures: ok }
