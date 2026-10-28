// Commissioned page mappings; registered Poster and current page state stay authoritative.
const PAGES = Object.freeze([
  ['acct:culture-core-1', 'culture.core.1', '3a41465b-b829-8018-81f6-f2e081537783', 'slideshow', 'sourced_slideshow', 'ugc-slideshow'],
  ['acct:pen-pal3', 'pen.pal3', '3ed1465b-b829-809f-afb4-da66093e7f9d', 'slideshow', 'sourced_slideshow', 'image-video'],
  ['acct:twin47996', 'twin47996', '3ed1465b-b829-8056-aa9b-f8579c3210b8', 'POV', 'sourced_image', 'single-image-video'],
].map(([pageId, handle, notionPageId, niche, engine, format]) =>
  Object.freeze({pageId, handle, notionPageId, niche, engine, format})));

export function isManagedSlideshowPage(pageId) {
  return PAGES.some(row => row.pageId === pageId);
}

export function managedSlideshowProjection(page) {
  return PAGES.find(row => page?.pageId === row.pageId && page.handle === row.handle
    && page.notionPageId === row.notionPageId && page.contentNiche === row.niche
    && page.contentEngine === row.engine) ?? null;
}

// Only the new single-image format extends the existing slideshow receipt engine set.
// Recipe/publication/job and current Master Pages binding are verified at intake.
export function isTwinSingleImageSource(source) {
  const twin = PAGES[2];
  return source?.contentEngine === twin.engine && source.pageId === twin.pageId
    && source.contentNiche === twin.niche && source.recipeId === `${twin.format}:master`
    && Array.isArray(source.media) && source.media.length === 1;
}

const exactKeys = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const finite = value => typeof value === 'number' && Number.isFinite(value);
const objectKey = /^[a-z0-9][a-z0-9-]{0,63}\/[a-z0-9][a-z0-9-]{0,99}\/photos\/[a-z0-9][a-z0-9_-]{0,199}\.[a-z0-9]{1,10}$/;

// Preserve the normal producer's closed photoPlan, including ordered original
// keys and crop intent. Its signature is compared with the receipt identity;
// this parser does not claim to recompute the producer's canonical plan hash.
export function managedPhotoPlanValid(source) {
  const twin = source?.pageId === 'acct:twin47996';
  const penpal = source?.pageId === 'acct:pen-pal3';
  if (!twin && !penpal) return false;
  const row = PAGES[twin ? 2 : 1], count = twin ? 1 : 3;
  const lane = twin ? 'interiors' : 'animals', subject = twin ? 'twin47996' : 'pen-pal3';
  if (source.recipeId !== `${row.format}:master` || source.contentEngine !== row.engine
    || source.contentNiche !== row.niche) return false;
  const plan = source.photoPlan;
  const variant = plan?.variantIndex;
  const varied = Number.isInteger(variant) && variant >= 1 && variant <= 61 * 61 * 2 * 21;
  const planKeys = ['planSlug','pageId','libraryId','lane','subject','totalDurationSeconds','brightnessGainFromOriginals','media','signature'];
  if (varied) planKeys.push('variantIndex');
  if (!exactKeys(plan,planKeys)
    || plan.planSlug !== 'page-photo-hold' || source.templateSlug !== plan.planSlug
    || plan.pageId !== row.pageId || plan.libraryId !== `syzygy:${lane}:${subject}`
    || source.sourceLibraryId !== plan.libraryId || plan.lane !== lane || plan.subject !== subject
    || (varied ? (!finite(plan.totalDurationSeconds) || plan.totalDurationSeconds < 11 || plan.totalDurationSeconds > 13) : plan.totalDurationSeconds !== 12) || plan.brightnessGainFromOriginals !== 0.85
    || !/^[a-f0-9]{64}$/.test(plan.signature) || source.planSignature !== plan.signature
    || !Array.isArray(plan.media) || plan.media.length !== count
    || !Array.isArray(source.media) || source.media.length !== count) return false;
  const seen = new Set(), originals = new Set();
  return plan.media.every((item,index) => {
    if (!exactKeys(item,['key','sourceKey','sha256','bytes','duration','focalX','focalY','cropMode','kenBurns'])
      || !objectKey.test(item.key) || !objectKey.test(item.sourceKey)
      || !item.key.startsWith(`${lane}/${subject}/photos/`)
      || item.key.split('/').at(-1) !== item.sourceKey.split('/').at(-1)
      || seen.has(item.key) || originals.has(item.sourceKey)
      || item.duration !== plan.totalDurationSeconds / count || item.cropMode !== 'cover'
      || !finite(item.focalX) || item.focalX < 0 || item.focalX > 1
      || !finite(item.focalY) || item.focalY < 0 || item.focalY > 1) return false;
    seen.add(item.key); originals.add(item.sourceKey);
    const kb = item.kenBurns;
    if (kb !== null && (!exactKeys(kb,['dir','zoom']) || !['in','out','none'].includes(kb.dir)
      || !finite(kb.zoom) || kb.zoom < 0 || kb.zoom > 0.5
      || (kb.dir === 'none' ? kb.zoom !== 0 : kb.zoom <= 0))) return false;
    if (!varied && twin && kb !== null && (kb.dir !== 'none' || kb.zoom !== 0)) return false;
    const media = source.media[index];
    return media?.objectKey === item.key && media.sha256 === item.sha256 && media.bytes === item.bytes;
  });
}
