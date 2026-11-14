import { sha256Hex, stableStringify } from './policy.js';

export const LYRIC_FORMAT = 'lyric-video';
export const LYRIC_PAGE = 'acct:burnt-mill-rd';
export const LYRIC_ACCOUNT = 'burnt.mill.rd';
export const LYRIC_NOTION = '3451465b-b829-80a8-b9d1-fb7e34b98ff5';
export const LYRIC_SOUND = '7611482162773035025';
const HASH = /^[0-9a-f]{64}$/;
const MANIFEST_FIELDS = ['schema','id','pageId','account','soundId','durationMs','phrases','canonicalCaption',
  'referenceAudioSha256','timingProvenance','intervalSemantics','sourceProgramId','sourceProgramSha256','notionPageId'];
const BINDING_FIELDS = ['schema','pageId','account','notionPageId','soundId','timingSetId','timingSha256',
  'sourceProgramId','sourceProgramSha256','durationMs','sourceSha256','generationJobId','recipeId','recipeVersion','recipeSpecHash','manifestJson'];
const CAPTION_FIELDS = ['identity','text','source','sound_id','sentiments','corpus_id','corpus_revision'];
const catalogFields = ['id','sha256','soundId','durationMs','canonicalCaption','sourceProgramId','sourceProgramSha256','notionPageId'];
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => plain(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value,key));
const bounded = (value,max=512) => typeof value === 'string' && value.length > 0 && value.length <= max && value.trim() === value && !/[\x00-\x1f]/.test(value);
const fail = code => { throw Object.assign(new Error(code), { code }); };

export function isLyricFormat(format) { return format === LYRIC_FORMAT; }

export function validateLyricCatalogBinding(value) {
  if (!plain(value) || !catalogFields.every(key => Object.hasOwn(value,key))
      || !bounded(value.id) || !HASH.test(value.sha256) || value.soundId !== LYRIC_SOUND
      || value.durationMs !== 20000 || value.notionPageId !== LYRIC_NOTION
      || !bounded(value.sourceProgramId) || !HASH.test(value.sourceProgramSha256)
      || typeof value.canonicalCaption !== 'string' || !value.canonicalCaption.trim()
      || value.canonicalCaption.length > 2200) fail('LYRIC_CATALOG_BINDING_INVALID');
  return Object.freeze(Object.fromEntries(catalogFields.map(key => [key,value[key]])));
}

export function selectLyricCatalogBinding(catalog, { masterPages, masterPagesHash, production }) {
  if (!plain(masterPages) || masterPages.pageId !== LYRIC_PAGE || masterPages.handle !== LYRIC_ACCOUNT
      || masterPages.notionPageId !== LYRIC_NOTION || masterPages.automationMode !== 'Automation'
      || masterPages.archived !== false || catalog?.schema !== 'content-lab.dossier-ingredient-catalog.v1'
      || catalog.pageId !== LYRIC_PAGE || catalog.masterPagesHash !== masterPagesHash
      || stableStringify(catalog.masterPages) !== stableStringify(masterPages)
      || !Array.isArray(catalog.currentFormatCandidates) || !catalog.currentFormatCandidates.includes(LYRIC_FORMAT)) {
    fail('LYRIC_CATALOG_SCOPE_MISMATCH');
  }
  const entry = catalog.formats?.find(item => item.formatId === LYRIC_FORMAT);
  const ingredient = entry?.ingredients?.find(item => item.ingredientId === 'lyric-timing');
  if (entry?.recipeId !== 'lyric-video:master' || entry.contentEngine !== 'lyrics_slideshows'
      || ingredient?.status !== 'bound' || ingredient.selectionControl !== 'lyricTimingSetId') fail('LYRIC_CATALOG_BINDING_INVALID');
  const binding = validateLyricCatalogBinding(ingredient.binding);
  if (!exact(production?.controls,['lyricTimingSetId']) || production.controls.lyricTimingSetId !== binding.id
      || !entry.productionSelections?.some(selection => selection.catalogVersion === production.catalogVersion
        && ['providerId','modelId','promptModuleId','referenceSetId','sourceLibraryId'].every(key => (selection[key] ?? null) === (production[key] ?? null))
        && exact(selection.controls,['lyricTimingSetId']) && selection.controls.lyricTimingSetId === binding.id)) {
    fail('LYRIC_CATALOG_SELECTION_MISMATCH');
  }
  return binding;
}

export async function parseLyricManifest(manifestJson, advertised) {
  const authority = validateLyricCatalogBinding(advertised);
  if (typeof manifestJson !== 'string') fail('LYRIC_MANIFEST_INVALID');
  const bytes = new TextEncoder().encode(manifestJson);
  if (bytes.length > 64*1024 || new TextDecoder('utf-8',{fatal:true}).decode(bytes) !== manifestJson
      || await sha256Hex(manifestJson) !== authority.sha256) fail('LYRIC_MANIFEST_HASH_MISMATCH');
  let manifest;
  try { manifest=JSON.parse(manifestJson); } catch { fail('LYRIC_MANIFEST_INVALID'); }
  if (!exact(manifest,MANIFEST_FIELDS) || manifest.schema !== 'content-lab.lyric-timing-set.v1'
      || manifest.pageId !== LYRIC_PAGE || manifest.account !== LYRIC_ACCOUNT
      || manifest.notionPageId !== LYRIC_NOTION || manifest.soundId !== LYRIC_SOUND
      || manifest.durationMs !== 20000 || !HASH.test(manifest.referenceAudioSha256)
      || !bounded(manifest.timingProvenance,2000) || !bounded(manifest.intervalSemantics,1000)
      || !Array.isArray(manifest.phrases) || manifest.phrases.length !== 7
      || manifest.id !== authority.id || manifest.canonicalCaption !== authority.canonicalCaption
      || manifest.sourceProgramId !== authority.sourceProgramId
      || manifest.sourceProgramSha256 !== authority.sourceProgramSha256) fail('LYRIC_MANIFEST_SCOPE_MISMATCH');
  let previousEnd=null;
  for (const phrase of manifest.phrases) {
    if (!exact(phrase,['startMs','endMs','text']) || !Number.isSafeInteger(phrase.startMs)
        || !Number.isSafeInteger(phrase.endMs) || phrase.startMs < 0 || phrase.startMs >= phrase.endMs
        || phrase.endMs > manifest.durationMs || (previousEnd !== null && phrase.startMs !== previousEnd)
        || !bounded(phrase.text,1000)) fail('LYRIC_MANIFEST_CUES_INVALID');
    previousEnd=phrase.endMs;
  }
  if (previousEnd !== manifest.durationMs || manifest.canonicalCaption !== manifest.phrases.map(phrase=>phrase.text).join('\n')) {
    fail('LYRIC_MANIFEST_CUES_INVALID');
  }
  return Object.freeze({...manifest,phrases:Object.freeze(manifest.phrases.map(phrase=>Object.freeze({...phrase})))});
}

export function validateLyricCapsuleShape(value) {
  if (!exact(value,BINDING_FIELDS) || value.schema !== 'content-lab.lyric-timing-binding.v1'
      || value.pageId !== LYRIC_PAGE || value.account !== LYRIC_ACCOUNT || value.notionPageId !== LYRIC_NOTION
      || value.soundId !== LYRIC_SOUND || value.durationMs !== 20000 || value.recipeId !== 'lyric-video:master'
      || !HASH.test(value.timingSha256) || !HASH.test(value.sourceProgramSha256) || !HASH.test(value.sourceSha256)
      || !/^sha256:[0-9a-f]{64}$/.test(value.recipeSpecHash)
      || !['timingSetId','sourceProgramId','generationJobId','recipeVersion'].every(key=>bounded(value[key]))
      || typeof value.manifestJson !== 'string' || new TextEncoder().encode(value.manifestJson).length > 64*1024) {
    fail('LYRIC_TIMING_BINDING_MISMATCH');
  }
  return value;
}

export async function validateLyricTimingBinding(value, { advertised, ...expected }) {
  const authority=validateLyricCatalogBinding(advertised);
  validateLyricCapsuleShape(value);
  if (!exact(value,BINDING_FIELDS) || value.schema !== 'content-lab.lyric-timing-binding.v1'
      || value.pageId !== LYRIC_PAGE || value.account !== LYRIC_ACCOUNT || value.notionPageId !== LYRIC_NOTION
      || value.soundId !== LYRIC_SOUND || value.durationMs !== 20000
      || value.timingSetId !== authority.id || value.timingSha256 !== authority.sha256
      || value.sourceProgramId !== authority.sourceProgramId || value.sourceProgramSha256 !== authority.sourceProgramSha256
      || !HASH.test(value.sourceSha256) || !/^sha256:[0-9a-f]{64}$/.test(value.recipeSpecHash)
      || value.recipeId !== 'lyric-video:master' || !bounded(value.generationJobId) || !bounded(value.recipeVersion)
      || Object.entries(expected).some(([key,comparison])=>value[key] !== comparison)) fail('LYRIC_TIMING_BINDING_MISMATCH');
  const manifest=await parseLyricManifest(value.manifestJson,authority);
  return Object.freeze({binding:Object.freeze({...value}),catalog:authority,manifest});
}

export function lyricCaption(timing, soundId=LYRIC_SOUND) {
  if (soundId !== LYRIC_SOUND) return null;
  return Object.freeze({identity:`lyric-timing:${timing.catalog.sha256}`,text:timing.manifest.canonicalCaption,
    source:'lyric_timing',sound_id:LYRIC_SOUND,sentiments:Object.freeze([]),corpus_id:null,corpus_revision:null});
}

export function normalizeLyricCaption(value) {
  if (!exact(value,CAPTION_FIELDS) || value.source !== 'lyric_timing' || value.sound_id !== LYRIC_SOUND
      || !/^lyric-timing:[0-9a-f]{64}$/.test(value.identity || '') || typeof value.text !== 'string'
      || !value.text.trim() || value.text.length > 2200 || value.corpus_id !== null || value.corpus_revision !== null
      || !Array.isArray(value.sentiments) || value.sentiments.length !== 0) fail('LYRIC_CAPTION_INVALID');
  return Object.freeze({...value,sentiments:Object.freeze([])});
}

export function requireLyricCaption(value, timing, soundId) {
  const expected=lyricCaption(timing,soundId);
  if (!expected || stableStringify(normalizeLyricCaption(value)) !== stableStringify(expected)) fail('LYRIC_CAPTION_MISMATCH');
  return expected;
}

export function requireLyricReceipt(receipt, timing) {
  const binding=timing.binding,phrases=timing.manifest.phrases;
  const fields={lyric_timing_set_id:binding.timingSetId,lyric_timing_sha256:binding.timingSha256,sound_id:binding.soundId,
    source_program_id:binding.sourceProgramId,source_program_sha256:binding.sourceProgramSha256};
  if (Object.entries(fields).some(([key,value])=>receipt[key] !== value)
      || receipt.schema !== 'posting-prepared-artifact/v2' || receipt.renderer_id !== 'content-lab.prepared-post'
      || receipt.renderer_version !== '2' || !Number.isSafeInteger(receipt.duration_ms) || Math.abs(receipt.duration_ms-20000)>34
      || !Array.isArray(receipt.phrases) || receipt.phrases.length !== 7
      || receipt.phrases.some((phrase,index)=>!exact(phrase,['startMs','endMs','text','pngSha256'])
        || !HASH.test(phrase.pngSha256) || ['startMs','endMs','text'].some(key=>phrase[key] !== phrases[index][key]))) {
    fail('LYRIC_RECEIPT_MISMATCH');
  }
  const frames=[0,6].flatMap(index=>[...new Set([Math.ceil(phrases[index].startMs*30/1000),Math.ceil(phrases[index].endMs*30/1000)-1])]
    .sort((a,b)=>a-b).map(frame=>({phraseIndex:index,frame})));
  if (!Array.isArray(receipt.phrase_frame_evidence) || receipt.phrase_frame_evidence.length !== frames.length
      || receipt.phrase_frame_evidence.some((row,index)=>!exact(row,['phraseIndex','frame','maxMeanGlyphError','sha256'])
        || row.phraseIndex !== frames[index].phraseIndex || row.frame !== frames[index].frame
        || !Number.isFinite(row.maxMeanGlyphError) || row.maxMeanGlyphError<0 || row.maxMeanGlyphError>35 || !HASH.test(row.sha256))) {
    fail('LYRIC_RECEIPT_FRAME_EVIDENCE_INVALID');
  }
  return receipt;
}
