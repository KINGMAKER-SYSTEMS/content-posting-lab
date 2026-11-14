// PER-CUT CLIP VARIATION (Eric 2026-10-22, between.the.lines66; expanded gb20b).
//
// A page whose library is one master cut into a few clips posts near-identical
// videos when every clip is cut at the recipe's one speed and framing; TikTok
// reads that as reused content. For a page listed here Content Lab gives every
// new cut its own speed, zoom and focus (content-posting-lab
// services/clip_variation.py) and records the values it applied in the clip's
// treatment receipt, the same receipt the final render checks.
//
// The Worker side is one rule: for these pages a clip satisfies the recipe when
// each filter stays within its recipe band, whatever speed and crop the Lab cut
// it at, and the post
// asks for exactly the speed and crop the clip carries. Slot and clip then
// match, so the final render takes the clip as it is: no regeneration, no
// per-post override. Caption, caption style and sound are untouched. Its own banded filters
// are carried onto the post too.
//
// Brightness is one-sided: a varied clip may be at most one band darker than the
// page look, never brighter than the look, and never above the 0.8 ceiling. A
// look already above 0.8 gets no brightness variation (the clip carries the look
// exactly); contrast, saturation and warmth still vary both ways.
// The list is the page_id set the Lab uses; the two must be changed together.
export const CLIP_VARIATION_PAGES = Object.freeze([
  'acct:between-the-lines66',
  'acct:user4426276720185',
  'acct:velvet-hours',
  'acct:yearningcentralized',
  'acct:fog-line72',
  'acct:center-fever',
]);

// How far a cut's grade may sit from the recipe's own value, per filter (the
// Lab's services/clip_variation.py GRADE_BANDS draws inside these). A filter
// not listed must equal the recipe exactly. Brightness is one-sided (see
// jitteredGrade and the SQL window): never brighter, at most one band darker,
// never above the ceiling unless it equals the look.
export const GRADE_BANDS = Object.freeze({ brightness: 0.04, contrast: 0.05, saturation: 0.06, warmth: 0.03 });
// Never brighter than 0.8 for a NEW cut, whatever the page look. A look already
// above 0.8 is carried exactly (no variation), which the gate admits as "equals
// the look".
export const BRIGHTNESS_CEILING = 0.8;
// The renderer's neutral value of each filter (domain/sourceTreatment.js NEUTRALS).
const FILTER_NEUTRALS = Object.freeze({ brightness: 1, contrast: 1, saturation: 1, warmth: 0, fade: 0, grain: 0, vignette: 0 });
const BAND_EPSILON = 1e-9;

export function variesClips(pageId) {
  return typeof pageId === 'string' && CLIP_VARIATION_PAGES.includes(pageId);
}

// Does the one-sided brightness band refuse this stored grade against the page
// look? The two refusal arms the gate applies: brighter than the look, or above
// the 0.8 ceiling while differing from the look. (A grade darker than one band
// below the look is refused by jitteredGrade's band check too, but it cannot be
// produced by the older symmetric band this helper exists to sweep up.)
export function brightnessRefused(brightness, lookBrightness) {
  if (typeof brightness !== 'number' || typeof lookBrightness !== 'number') return true;
  return brightness - lookBrightness > BAND_EPSILON
    || (brightness > BRIGHTNESS_CEILING + BAND_EPSILON && Math.abs(brightness - lookBrightness) > BAND_EPSILON);
}

/**
 * The treatment this asset can be asked for on this page: the requested one,
 * with the clip's own receipt speed and crop on a varied page, and its grade
 * when every filter sits inside GRADE_BANDS of the recipe (brightness: the
 * one-sided rule above). Anything that does not read as a receipt keeps the
 * request unchanged, so the normal gate still refuses it by its own name.
 */
export function clipTreatmentRequest(requested, payload, pageId) {
  const visual = payload?.sourceTreatment?.visualTreatment;
  if (!variesClips(pageId) || !requested || typeof requested !== 'object'
      || !visual || typeof visual !== 'object'
      || typeof visual.clipSpeed !== 'number' || !visual.clipCrop || typeof visual.clipCrop !== 'object') {
    return requested;
  }
  // Key order as normalizeClipCrop writes it, so a byte comparison of two crops holds.
  const { zoom, focusX, focusY } = visual.clipCrop;
  return { ...requested, clipSpeed: visual.clipSpeed, clipCrop: { zoom, focusX, focusY },
    ...(jitteredGrade(requested.filters, visual.filters) ?? {}) };
}

// The clip's own filters when its grade differs from the recipe and every
// filter sits inside its band; null keeps the request's filters (an unjittered
// clip keeps today's bytes, and an out-of-band grade is refused by the gate).
// Brightness follows the one-sided rule: never brighter than the look, at most
// one band darker, never above the ceiling unless it equals the look.
function jitteredGrade(requestedFilters, clipFilters) {
  if (!clipFilters || typeof clipFilters !== 'object' || Array.isArray(clipFilters)) return null;
  let moved = false;
  for (const [name, neutral] of Object.entries(FILTER_NEUTRALS)) {
    const recipe = requestedFilters?.[name] ?? neutral;
    const clip = clipFilters[name] ?? neutral;
    if (typeof clip !== 'number' || typeof recipe !== 'number') return null;
    if (name === 'brightness') {
      if (clip - recipe > BAND_EPSILON) return null;
      if (recipe - clip > (GRADE_BANDS.brightness ?? 0) + BAND_EPSILON) return null;
      if (clip > BRIGHTNESS_CEILING + BAND_EPSILON && Math.abs(clip - recipe) > BAND_EPSILON) return null;
      if (clip !== recipe) moved = true;
      continue;
    }
    if (Math.abs(clip - recipe) > (GRADE_BANDS[name] ?? 0) + BAND_EPSILON) return null;
    if (clip !== recipe) moved = true;
  }
  // Key order as normalizeRenderTreatment writes filters (sorted), so a byte
  // comparison against a stored slot holds.
  return moved ? { filters: Object.fromEntries(Object.entries(clipFilters).sort(([a], [b]) => a.localeCompare(b))) } : null;
}

/** The (speed, zoom, focus) a post renders with, as one comparable key. */
export function clipLookKey(treatment) {
  const crop = treatment?.clipCrop ?? {};
  return `${treatment?.clipSpeed ?? 1}|${crop.zoom ?? 1}|${crop.focusX ?? 0.5}|${crop.focusY ?? 0.5}`;
}
