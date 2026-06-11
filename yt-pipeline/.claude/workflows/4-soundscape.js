export const meta = {
  name: 'abn-soundscape',
  description: '5 sound designers add beat-synced SFX + ducked music bed to the 10 aligned cuts; 2-ear review',
  phases: [
    { title: 'Sound', detail: 'SFX on beats + bed ducked under VO, factory recipe' },
    { title: 'Listen', detail: 'mix QC + taste review across all 10' },
    { title: 'Fix', detail: 'remix flagged segments' },
  ],
}

const REPO = '/Users/risingtidesdev/dev/content-posting-lab'
const OUT = REPO + '/yt-pipeline/src/animations'
const ASM = OUT + '/assembly'
const T9 = '/Volumes/T9/agenticbuildernews/agenticnews_assets'

const SEG_FOLDER = {
  0: 'RalphAgent1-sonnet', 1: 'RalphAgent2-sonnet', 2: 'RalphAgent3-sonnet',
  3: 'RalphAgent4-opus', 4: 'RalphAgent5-opus', 5: 'RalphAgent6-opus',
  6: 'RalphAgent7-fable', 7: 'RalphAgent8-fable', 8: 'RalphAgent9-haiku', 9: 'RalphAgent10-haiku',
}
const DESIGNERS = [
  { n: 1, model: 'fable', segs: [0, 5] },
  { n: 2, model: 'fable', segs: [6, 7] },
  { n: 3, model: 'opus', segs: [1, 8] },
  { n: 4, model: 'opus', segs: [3, 4] },
  { n: 5, model: 'sonnet', segs: [2, 9] },
]

const SOUND_SCHEMA = {
  type: 'object',
  required: ['designer', 'segments'],
  properties: {
    designer: { type: 'string' },
    segments: {
      type: 'array',
      items: {
        type: 'object',
        required: ['segment', 'mp4', 'sfxCount', 'lufs', 'proof'],
        properties: {
          segment: { type: 'number' },
          mp4: { type: 'string' },
          sfxCount: { type: 'number' },
          lufs: { type: 'number' },
          proof: { type: 'string' },
          notes: { type: 'string' },
        },
      },
    },
  },
}

const REVIEW_SCHEMA = {
  type: 'object',
  required: ['reviewer', 'perSegment', 'summary'],
  properties: {
    reviewer: { type: 'string' },
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

function soundPrompt(d) {
  return `You are SoundDesigner${d.n}-${d.model}, scoring the ABN aligned cuts. Max 8 build-listen iterations. Your segments: ${d.segs.join(', ')}. For each segment N: input ${ASM}/s<N>_aligned.mp4 (VO-only), output ${ASM}/s<N>_mix.mp4 (same video, full sound design). Own only your segments' files.

PALETTE (all in ${T9}/): sfx_whoosh.mp3, whoosh.mp3, whoosh_v1.mp3 (slams/entrances), sfx_pop.mp3 (badges, small reveals, stamps), sfx_riser.mp3 (build-up INTO a payoff — time it to RESOLVE exactly on the payoff beat), bed.mp3 + bed_v1.mp3 (music beds — ffprobe both, pick per segment energy; bed.mp3 is the factory default). Multiply the palette with ffmpeg: pitch/speed variants (asetrate*0.85..1.2 + aresample, atempo), lowpassed soft versions for minor beats, volume tiers. ffprobe each sample's duration first.

BEAT MAP (this is what makes it sound designed, not decorated): for each segment read ${OUT}/<agentFolder>/ALIGN.json (clip windows; agent folders: ${d.segs.map(s => 's' + s + ' -> ' + SEG_FOLDER[s]).join(' ; ')}) and each VideoN/index.html const P (beat times are parameterized there; NOTES.md explains each video's choreography). Convert to cut time: tCut = gridTime - cutBase (cutBase: 0.0 for all segments except segment 7 = 21.34 — check ${ASM}/s<N>_NOTES.md to confirm what the compiler used).

TASTE RULES (violating these = slot-machine noise, instant fail):
- 3-6 SFX per clip window, NEVER one per beat. Hit: each clip's ENTRANCE (whoosh, vary the variant), the single biggest payoff/verdict beat (riser resolving on it, or pop for stamps/badges), and at most 2 mid-beats that visually slam.
- SFX volume sits UNDER the VO: peak around -18 to -14 dB relative; a whoosh must never mask a spoken word — if a beat coincides exactly with a word onset, pull the SFX 0.1s earlier and 3dB down.
- Count-up monuments: low soft pop at start + riser resolving at the final value. Terminal/typing motifs: NO per-keystroke sounds.
- Nothing in the last 1.5s except the bed (let the VO close clean).

BUILD (ffmpeg): 1) SFX track: each event = adelay=<ms>|<ms> on its (possibly pitched/filtered) sample, amix all events over silence of exact cut duration. 2) MIX, the factory recipe (proven in production — keep these numbers): VO+SFX premix (VO weight 1.0, SFX bus as placed), then [bed]volume=0.22, sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300 keyed by the VO+SFX bus, amix weights 1 0.9, then loudnorm=I=-14:TP=-1.5:LRA=11. Loop/trim the bed to cut length (aloop or -stream_loop), fade bed in 1s / out 2s. 3) Remux with -c:v copy from s<N>_aligned.mp4 — DO NOT re-encode video. A/V duration delta < 0.05s.

VERIFY each iteration: ffprobe (video stream untouched: same duration/codec; audio aac 48k). ebur128 → integrated LUFS within -14.5..-13.5. Whisper-check VO intelligibility OVER the mix at 2 word-dense moments (extract 2.5s, whisper tiny.en, words must still transcribe). SFX-on-beat proof: extract 1.2s audio around 2 known slam beats, measure RMS (astats) vs the 1.2s before — the hit must show; state both beats + values in proof. LISTEN judgment: extract a 10s stretch you are least sure about, play it through astats/spectral judgment + your read of the waveform; if the bed fights the VO or SFX feel random, fix.

Write ${ASM}/s<N>_SOUND.md (bed choice, every SFX event: time/sample/variant/level/why, mix chain). Return: designer, per segment {segment, mp4, sfxCount, lufs, proof, notes}.`
}

const EARS = [
  {
    key: 'mixqc', model: 'fable', title: 'Broadcast Mix QC',
    brief: 'You are a broadcast audio QC engineer. For EVERY s<N>_mix.mp4 in ' + ASM + ': ebur128 integrated/-range/true-peak (gate: -14.5..-13.5 LUFS, TP <= -1.0dBTP, batch spread <= 1.0 LU); astats for clipping; whisper 3 word-dense windows per cut — every word must transcribe as cleanly as in the dry s<N>_aligned.mp4 (run both, compare); verify video stream is bit-identical-length to the aligned cut (no re-encode, A/V delta < 0.05s); check the bed does not pump audibly (sidechain release artifacts) by inspecting RMS envelope continuity in 2 VO gaps; verify nothing but bed sounds in the final 1.5s.',
  },
  {
    key: 'taste', model: 'opus', title: 'Sound Design Taste Editor',
    brief: 'You are a senior trailer/promo sound designer reviewing taste, not levels. For each cut: read its s<N>_SOUND.md event list against the clip choreography (ALIGN.json + the videos\' NOTES.md), then audit by ear-proxy: extract audio at 4-5 SFX moments and 2 non-SFX stretches; judge: do hits land ON visual slams (pull the matching frame to confirm the visual beat), is density 3-6 per clip (count events/clip from SOUND.md and flag any clip over), does a riser resolve ON its payoff, do any SFX collide with word onsets (whisper the moment), does the segment have a dynamic arc (quiet valleys between hits) or is it wall-to-wall? Flag samey-ness: if the same whoosh variant fires more than 4x in one segment, fail it.',
  },
]

function earPrompt(r) {
  return `${r.brief}

Review ALL 10 cuts: ${ASM}/s0_mix.mp4 .. s9_mix.mp4. Max 10 iterations. Reference: ${ASM}/s<N>_SOUND.md, ${ASM}/s<N>_NOTES.md, ${OUT}/segments.json. Every 'fix' verdict needs mechanically executable issues (timestamp + wrong + right). Do not flag outside your lens. Return: reviewer title, perSegment [{segment, verdict, issues}], summary.`
}

function fixPrompt(d, segIssues) {
  const lines = d.segs.filter(s => segIssues[s] && segIssues[s].length)
    .map(s => `SEGMENT ${s}:\n` + segIssues[s].map(x => '  - ' + x).join('\n')).join('\n')
  return `You are SoundDesigner${d.n}-${d.model} (remix pass). Max 8 iterations. Review flagged:\n${lines}\n\nRemix the flagged segments in place (${ASM}/s<N>_mix.mp4): adjust the SFX events / bed level / duck settings per the notes, keep the factory mix recipe and -14 LUFS gate, keep -c:v copy (no video re-encode). Re-verify (ebur128, whisper intelligibility, RMS proof at the corrected beats) and update s<N>_SOUND.md with a REMIX section. Return what changed per segment + final LUFS.`
}

log('Sound fleet: beat-synced SFX + ducked bed over 10 cuts')

const results = await pipeline(
  DESIGNERS,
  d => agent(soundPrompt(d), { label: `sound:${d.n}-${d.model}`, phase: 'Sound', model: d.model, schema: SOUND_SCHEMA })
)

const okSound = results.filter(Boolean)
log(`Sound pass done: ${okSound.length}/5 designers returned`)

phase('Listen')
const reviews = await parallel(EARS.map(r => () =>
  agent(earPrompt(r), { label: `ear:${r.key}`, phase: 'Listen', model: r.model, schema: REVIEW_SCHEMA })
))

const okReviews = reviews.filter(Boolean)
const segIssues = {}
for (const rv of okReviews) {
  for (const ps of rv.perSegment) {
    if (ps.verdict === 'fix' && ps.issues.length) {
      segIssues[ps.segment] = (segIssues[ps.segment] || []).concat(ps.issues.map(i => `[${rv.reviewer}] ${i}`))
    }
  }
}
const failing = Object.keys(segIssues).map(Number)
log(`Ears in: ${failing.length} segments flagged (${failing.join(', ') || 'none'})`)

phase('Fix')
let fixes = []
if (failing.length) {
  const need = DESIGNERS.filter(d => d.segs.some(s => failing.includes(s)))
  fixes = await parallel(need.map(d => () =>
    agent(fixPrompt(d, segIssues), { label: `remix:${d.n}-${d.model}`, phase: 'Fix', model: d.model })
  ))
}

return {
  designers: okSound.map(s => ({ designer: s.designer, segments: s.segments })),
  reviews: okReviews.map(r => ({ reviewer: r.reviewer, summary: r.summary, flagged: r.perSegment.filter(p => p.verdict === 'fix').map(p => p.segment) })),
  remixed: failing,
  fixResults: fixes,
}