export const meta = {
  name: 'abn-research-pipeline',
  description: 'Creator intel -> scored topics -> verified research -> outline ledger -> segment scripts -> editor compile, for the workflows-over-system-prompts episode',
  phases: [
    { title: 'CreatorIntel', detail: '5 agents study top niche creators' },
    { title: 'Topics', detail: '3 angle generators -> SEO + CTR scorers' },
    { title: 'Research', detail: '5 facet researchers + adversarial verify' },
    { title: 'Outline', detail: 'dossier -> episode outline ledger' },
    { title: 'Scripts', detail: 'per-segment writer -> editor pipeline' },
    { title: 'Compile', detail: 'editors-in-chief: retention + SEO package' },
  ],
}

const REPO = '/Users/risingtidesdev/dev/content-posting-lab'
const EP = REPO + '/yt-pipeline/drafts/ep-workflows-over-prompts'

const BRIEF = `EPISODE BRIEF — "Workflows over System Prompts": why programmatically LOCKED values (orchestration scripts, JSON schemas as contracts/ledgers, deterministic control flow) beat prompt-tuning for agent quality. Cover: how frontier coding agents (OpenAI Codex / GPT-5.x-codex line and Anthropic's Fable 5 / Claude Code) orchestrate deep, long autonomous runs; compiled techniques like /loop, /goal (stop-hook conditions), and ralph-loop (persistent re-prompt loops with state files); JSON schemas as ledgers and contracts between pipeline stages (structured outputs that downstream stages can trust); why "the prompt is not the program — the workflow is". The audience: builders who ship agentic systems (the ABN audience). Register: technical wire-service, proof-first, no hype.`

const CREATOR_SCHEMA = {
  type: 'object', required: ['creators'],
  properties: { creators: { type: 'array', items: { type: 'object',
    required: ['name', 'platformScale', 'formats', 'recurringSubjects', 'titlePatterns', 'whatWorks'],
    properties: { name: { type: 'string' }, platformScale: { type: 'string' }, formats: { type: 'array', items: { type: 'string' } },
      recurringSubjects: { type: 'array', items: { type: 'string' } }, titlePatterns: { type: 'array', items: { type: 'string' } },
      hookPatterns: { type: 'array', items: { type: 'string' } }, whatWorks: { type: 'string' } } } } },
}
const ANGLES_SCHEMA = {
  type: 'object', required: ['angles'],
  properties: { angles: { type: 'array', items: { type: 'object', required: ['title', 'angle', 'hook', 'whyNow'],
    properties: { title: { type: 'string' }, angle: { type: 'string' }, hook: { type: 'string' }, whyNow: { type: 'string' } } } } },
}
const SCORE_SCHEMA = {
  type: 'object', required: ['scores'],
  properties: { scores: { type: 'array', items: { type: 'object', required: ['title', 'score', 'reason'],
    properties: { title: { type: 'string' }, score: { type: 'number' }, reason: { type: 'string' } } } } },
}
const FACTS_SCHEMA = {
  type: 'object', required: ['facet', 'claims'],
  properties: { facet: { type: 'string' }, claims: { type: 'array', items: { type: 'object',
    required: ['claim', 'source', 'confidence'],
    properties: { claim: { type: 'string' }, source: { type: 'string' }, quote: { type: 'string' }, number: { type: 'string' }, confidence: { type: 'string' } } } } },
}
const VERIFY_SCHEMA = {
  type: 'object', required: ['kept', 'killed'],
  properties: { kept: { type: 'array', items: { type: 'object', required: ['claim', 'source'], properties: { claim: { type: 'string' }, source: { type: 'string' }, number: { type: 'string' } } } },
    killed: { type: 'array', items: { type: 'object', required: ['claim', 'why'], properties: { claim: { type: 'string' }, why: { type: 'string' } } } } },
}
const OUTLINE_SCHEMA = {
  type: 'object', required: ['title', 'segments'],
  properties: { title: { type: 'string' }, segments: { type: 'array', items: { type: 'object',
    required: ['index', 'title', 'hook', 'keyFacts', 'seoKeywords', 'targetWords'],
    properties: { index: { type: 'number' }, title: { type: 'string' }, hook: { type: 'string' },
      keyFacts: { type: 'array', items: { type: 'string' } }, sources: { type: 'array', items: { type: 'string' } },
      seoKeywords: { type: 'array', items: { type: 'string' } }, targetWords: { type: 'number' } } } } },
}
const SCRIPT_SCHEMA = {
  type: 'object', required: ['index', 'script', 'wordCount'],
  properties: { index: { type: 'number' }, script: { type: 'string' }, wordCount: { type: 'number' }, factsUsed: { type: 'array', items: { type: 'string' } } },
}
const EDIT_SCHEMA = {
  type: 'object', required: ['index', 'verdict', 'script', 'changes'],
  properties: { index: { type: 'number' }, verdict: { type: 'string', enum: ['pass', 'rewrote'] }, script: { type: 'string' }, changes: { type: 'array', items: { type: 'string' } } },
}

log('Research pipeline: every stage hands the next a schema-validated ledger')

phase('CreatorIntel')
const CLUSTERS = [
  'Fireship and Theo (t3.gg) — high-velocity dev-news/explainers',
  'ThePrimeagen and Matt Pocock — dev-culture takes + deep technical teaching',
  'Matt Berman, Wes Roth, AI Explained — AI news/analysis channels',
  'IndyDevDan, AI Jason, David Ondrej — agentic-coding / AI-builder practitioners',
  'smaller fast-rising agentic-AI channels from the last 6 months (find 3-4 yourself via search)',
]
const intel = await parallel(CLUSTERS.map((c, i) => () =>
  agent(`Research these YouTube creators in the AI/dev-builder niche: ${c}. Use web search. For EACH creator: scale (subs/views ballpark), content formats (length, structure, cadence), recurring subjects in the last ~3 months, title patterns (give 3-4 REAL recent titles verbatim), hook patterns (how their first 10 seconds work), and a one-line "what works" verdict. We make ABN (Agentic Builder News) — daily AI-builder news, 10-14min episodes — and are planning an episode on: ${BRIEF.slice(0, 400)}... Note anything these creators have ALREADY done on workflows/agent-orchestration topics (so we can differentiate). Write your full notes to ${EP}/intel/cluster${i + 1}.md (mkdir -p first).`,
    { label: `intel:${i + 1}`, phase: 'CreatorIntel', schema: CREATOR_SCHEMA })
))

const allCreators = intel.filter(Boolean).flatMap(r => r.creators)
log(`Creator intel: ${allCreators.length} creators profiled`)

phase('Topics')
const intelDigest = allCreators.map(c => `${c.name} [${c.platformScale}] subjects: ${c.recurringSubjects.join(', ')} | titles like: ${c.titlePatterns.slice(0, 2).join(' / ')} | works: ${c.whatWorks}`).join('\n')
const LENSES = [
  { key: 'practical', p: 'builder-practical lens: the viewer should walk away able to DO something (compile techniques, lock values, write schema contracts)' },
  { key: 'urgency', p: 'news-urgency lens: what changed RIGHT NOW in how Codex/Fable-class agents run long autonomous sessions that makes this episode timely' },
  { key: 'contrarian', p: 'contrarian lens: attack a sacred cow (prompt engineering is dead / your system prompt is a liability / stop tuning words, start locking values)' },
]
const angleBatches = await parallel(LENSES.map(l => () =>
  agent(`${BRIEF}\n\nCREATOR INTEL (what the niche already watches):\n${intelDigest}\n\nGenerate 5 distinct episode ANGLES through the ${l.p}. Each: a YouTube title (<=80 chars), the angle in 2 sentences, the cold-open hook line, and why-now. Differentiate from what the profiled creators already covered.`,
    { label: `angles:${l.key}`, phase: 'Topics', schema: ANGLES_SCHEMA })
))
const allAngles = angleBatches.filter(Boolean).flatMap(b => b.angles)
const angleList = allAngles.map((a, i) => `${i + 1}. "${a.title}" — ${a.angle} HOOK: ${a.hook}`).join('\n')

const scorers = await parallel([
  () => agent(`You are a YouTube SEO strategist. Score each angle 0-100 for SEARCH performance: keyword volume potential (agentic workflows, AI agents, Claude Code, Codex, prompt engineering...), search-intent match, long-tail capture, evergreen-vs-news decay. Use web search to sanity-check what people actually search.\n\nANGLES:\n${angleList}`,
    { label: 'score:seo', phase: 'Topics', schema: SCORE_SCHEMA }),
  () => agent(`You are a CTR/packaging editor (Fireship-school). Score each angle 0-100 for CLICK psychology: curiosity gap, stakes, specificity, contrarian energy, thumbnail-ability. Penalize generic "AI is changing everything" framing.\n\nANGLES:\n${angleList}`,
    { label: 'score:ctr', phase: 'Topics', schema: SCORE_SCHEMA }),
])
const tally = {}
for (const s of scorers.filter(Boolean)) for (const x of s.scores) tally[x.title] = (tally[x.title] || 0) + x.score
const ranked = Object.entries(tally).sort((a, b) => b[1] - a[1])
const winner = allAngles.find(a => a.title === ranked[0][0]) || allAngles[0]
log(`Winning angle: "${winner.title}" (${ranked[0][1]} pts)`)

phase('Research')
const FACETS = [
  'OpenAI Codex (GPT-5.x-codex line, including any 5.5-class release): long autonomous runs, orchestration features, harness/compile patterns, real numbers (run lengths, PR counts, benchmarks), official docs + engineering posts',
  'Anthropic Fable 5 and Claude Code: agentic orchestration (subagents, workflows, hooks, background tasks), long-run autonomy, structured outputs, official docs + announcements',
  'Compiled control-flow techniques in agent CLIs: loop commands, goal/stop-hook conditions, ralph-loop pattern (Geoffrey Huntley lineage), state files, why re-prompting with locked state beats one big prompt — find the actual origins and real usage reports',
  'JSON schemas as contracts/ledgers between agent pipeline stages: structured outputs enforcement, schema-validated handoffs, determinism in multi-agent systems — research literature, vendor docs, practitioner writeups',
  'Evidence that workflows/locked-values BEAT prompt-tuning: evals, case studies, postmortems, practitioner threads with numbers; also the strongest COUNTER-arguments (when prompts matter more) so the episode is honest',
]
const dossier = await pipeline(
  FACETS,
  (f, _f, i) => agent(`Research facet for the episode "${winner.title}":\n${f}\n\nUse web search + fetch primary sources. Return claims with: the claim, source URL, a short verbatim quote where possible, any NUMBER (numbers are gold for our visuals), and confidence (high/med/low). 8-15 claims. Today-current information only — verify recency. Also write full notes to ${EP}/research/facet${i + 1}.md (mkdir -p first).`,
    { label: `research:${i + 1}`, phase: 'Research', schema: FACTS_SCHEMA }),
  (facts, f, i) => {
    if (!facts) return null
    return agent(`Adversarial fact verification. Try to REFUTE or downgrade each claim below: re-search each, check the source actually says it, check the number is right, check it is current (not stale model versions or rumors). Kill anything you cannot confirm from a real source; keep what survives with its best source + number.\n\nCLAIMS:\n${facts.claims.map(c => `- ${c.claim} [${c.source}] ${c.number || ''}`).join('\n')}`,
      { label: `verify:${i + 1}`, phase: 'Research', schema: VERIFY_SCHEMA }).then(v => ({ facet: facts.facet, ...v }))
  }
)
const verified = dossier.filter(Boolean)
const keptCount = verified.reduce((s, d) => s + d.kept.length, 0)
log(`Dossier: ${keptCount} verified claims across ${verified.length} facets`)

phase('Outline')
const dossierText = verified.map(d => `## ${d.facet}\n${d.kept.map(k => `- ${k.claim} [${k.source}] ${k.number || ''}`).join('\n')}`).join('\n\n')
const outline = await agent(`${BRIEF}\n\nWINNING ANGLE: "${winner.title}" — ${winner.angle}\nHOOK: ${winner.hook}\n\nVERIFIED DOSSIER (use ONLY these facts; every keyFact must trace to one):\n${dossierText}\n\nCompose the episode outline: 8-9 segments, ~10-13 min total at ~145 spoken wpm (targetWords 130-170 per segment). Segment 0 = cold open (the hook + the single most striking number). Then: the thesis (workflows over prompts), the Codex story, the Fable/Claude Code story, the compiled-techniques segment (/loop, /goal, ralph-loop — explain mechanics concretely), the schema-as-ledger segment, the evidence segment (numbers, with the honest counter-case), the how-to-start segment, the takeaway. Each segment: title, hook line, 4-6 keyFacts (verbatim from dossier with their numbers), sources, seoKeywords, targetWords. ALSO write the outline as readable markdown to ${EP}/OUTLINE.md and the JSON to ${EP}/outline.json — this file is the LEDGER the script stage reads.`,
  { label: 'outline', phase: 'Outline', model: 'fable', schema: OUTLINE_SCHEMA })

if (!outline) throw new Error('outline failed')
log(`Outline: ${outline.segments.length} segments — "${outline.title}"`)

phase('Scripts')
const scripts = await pipeline(
  outline.segments,
  seg => agent(`You are the ABN scriptwriter. Write segment ${seg.index} of "${outline.title}".\nSEGMENT: ${seg.title}\nHOOK LINE: ${seg.hook}\nKEY FACTS (use these, keep every NUMBER verbatim):\n${seg.keyFacts.map(f => '- ' + f).join('\n')}\nSEO KEYWORDS to say naturally: ${seg.seoKeywords.join(', ')}\nTARGET: ${seg.targetWords} words (spoken VO, ~145wpm).\n\nABN voice: technical wire-service urgency, builder-to-builder, zero hype filler, concrete mechanisms and numbers over adjectives, short punchy sentences that TTS reads well (avoid tongue-twisters, spell tricky terms simply). Start on the hook. NO segues to other segments (no "next up..."), NO sign-offs — the segment ends on its own payoff line. Substance rule: name the actual model/feature/number/mechanism in the first two sentences.`,
    { label: `write:s${seg.index}`, phase: 'Scripts', schema: SCRIPT_SCHEMA }),
  (draft, seg) => {
    if (!draft) return null
    return agent(`You are the ABN segment editor. Edit this VO script for segment ${seg.index} ("${seg.title}").\n\nSCRIPT:\n${draft.script}\n\nGates: (1) FACT-CHECK — every claim/number must appear in these keyFacts, fix or cut anything that drifted: ${seg.keyFacts.join(' | ')}. (2) Hook lands in sentence one. (3) ${seg.targetWords}±20 words. (4) Reads aloud cleanly (TTS-safe: no parentheticals, no URLs spoken raw, numbers written as spoken). (5) SEO keywords present naturally: ${seg.seoKeywords.join(', ')}. (6) No segues/sign-offs; ends on a payoff. (7) Kill hype adjectives; keep mechanisms. Return the final script (rewrite as needed) + what you changed. Write the final to ${EP}/scripts/s${seg.index}.md with a header line (title + targetWords + keywords).`,
      { label: `edit:s${seg.index}`, phase: 'Scripts', schema: EDIT_SCHEMA })
  }
)
const finalScripts = scripts.filter(Boolean)
log(`Scripts: ${finalScripts.length}/${outline.segments.length} segments through write+edit`)

phase('Compile')
const fullScript = finalScripts.sort((a, b) => a.index - b.index).map(s => `## s${s.index}\n${s.script}`).join('\n\n')
const chiefs = await parallel([
  () => agent(`Editor-in-chief (retention) read of the FULL episode script below. Judge as a sequence: does energy build, do segments vary in rhythm, is there a mid-episode slump, does the cold open promise what the episode delivers, does the ending land? Check total runtime (count words / 145wpm — target 10-13min). Fix what needs fixing by EDITING the per-segment files in ${EP}/scripts/ directly (keep the fact discipline — facts live in ${EP}/outline.json), and write an EPISODE-NOTES.md with what you changed and the final estimated runtime.\n\n${fullScript}`,
    { label: 'chief:retention', phase: 'Compile', model: 'fable' }),
  () => agent(`Editor-in-chief (packaging) for the episode below. Produce ${EP}/PACKAGE.md: 3 title options (use the winning angle "${winner.title}" as a base, but improve), description (first line = strongest alt title, then 2-sentence summary, then chapter list from segment titles), 15-20 tags, thumbnail concept (claim words <=4 + proof object, per our forge-alert formula), and the 3 best shorts-cut candidates (which segments contain a self-contained 30-45s arc). Ground SEO choices in real search behavior (web search to check).\n\n${fullScript}`,
    { label: 'chief:packaging', phase: 'Compile', model: 'fable' }),
])

return {
  angle: winner,
  ranking: ranked.slice(0, 5),
  dossierClaims: keptCount,
  outline: { title: outline.title, segments: outline.segments.map(s => ({ i: s.index, title: s.title, words: s.targetWords })) },
  scripts: finalScripts.map(s => ({ i: s.index, verdict: s.verdict, changes: s.changes ? s.changes.length : 0 })),
  chiefs: chiefs.filter(Boolean).map(c => String(c).slice(0, 300)),
}