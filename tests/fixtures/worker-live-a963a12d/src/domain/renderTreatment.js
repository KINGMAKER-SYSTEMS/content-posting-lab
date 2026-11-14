import { normalizePersistedCaptionStyle } from './captionStyle.js';

const FILTER_FIELDS = Object.freeze({
  brightness: Object.freeze([0, 3]),
  contrast: Object.freeze([0, 3]),
  saturation: Object.freeze([0, 3]),
  warmth: Object.freeze([-1, 1]),
  fade: Object.freeze([0, 1]),
  grain: Object.freeze([0, 1]),
  vignette: Object.freeze([0, 1]),
});

// A page's picture frame. The delivered video is always a 1080x1920 canvas;
// any other ratio is the picture's band inside it, black above and below,
// composed by Content Lab at final render. 9:16 is full-bleed and is never
// stored, so a page that never picks a frame keeps its treatment bytes.
export const PAGE_FRAMES = Object.freeze(['9:16', '16:9', '1:1', '3:4', '4:3']);

// How a framed page's picture sits in its band (Content Lab cuts it; the
// geometry is src/domain/pageFrameGeometry.js): `fill` covers the band and the
// clip crop picks the window, `fit` shows the whole crop window at the source's
// own aspect. It exists only beside a non-9:16 frame; 9:16 has nothing to fit
// into and never carries one. A framed recipe with no fit is not cut as fit:
// Content Lab cuts it 9:16 and the post keeps that picture's middle band.
export const PAGE_FRAME_FITS = Object.freeze(['fill', 'fit']);

export class RenderTreatmentError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'RenderTreatmentError';
    this.code = code;
  }
}

/**
 * Exact effective treatment carried by a schema-v4 schedule slot. Values come
 * from the locked Dossier recipe or a protected calendar replacement; the
 * browser never supplies filter names or arbitrary production controls.
 */
export function normalizeRenderTreatment(value) {
  const visual = normalizeVisualTreatment(value);
  const stylePreset = value.stylePreset === undefined || value.stylePreset === null
    ? null
    : boundedPreset(value.stylePreset);
  // This is the recipe/payload read boundary. Older persisted box/highlight
  // styles relied on Dossier's displayed black default; raw new writes still
  // go through normalizeCaptionStyle and require an explicit color.
  const captionStyle = normalizePersistedCaptionStyle(value.captionStyle, { requireComplete: true });
  const frame = normalizePageFrame(value.frame);
  const frameFit = normalizePageFrameFit(value.frameFit, frame);
  const captionDelivery = normalizeCaptionDelivery(value.captionDelivery);
  return Object.freeze({
    stylePreset,
    filters: visual.filters,
    captionStyle,
    clipSpeed: visual.clipSpeed,
    clipCrop: visual.clipCrop,
    // Absent for burned pages, so every existing treatment hash stays
    // byte-identical (the same trick `inverted` uses). An inverted look cannot
    // be typed (Content Lab refuses in_app + inverted), so it stays burned
    // here, at every door that builds a treatment (a slot edit keeps the
    // stored captionDelivery and may turn invert on): the post renders burned
    // instead of failing.
    ...(captionDelivery === 'in_app' && captionStyle.inverted !== true ? { captionDelivery } : {}),
    ...(frame === '9:16' ? {} : { frame, ...(frameFit ? { frameFit } : {}) }),
  });
}

/** Absent, null or empty is burned (no key). Only the closed value "in_app"
 * survives into the treatment; anything else is refused, never silently dropped. */
export function normalizeCaptionDelivery(value) {
  if (value === undefined || value === null || value === '') return null;
  if (value === 'in_app') return 'in_app';
  throw new RenderTreatmentError('CAPTION_DELIVERY_INVALID', "captionDelivery must be 'in_app' when present");
}

/** Absent or null is 9:16. The frame is not part of the visual subset: every
 * final render is letterboxed to its band (clips cut after a change are also
 * cut to it), so it never makes existing source bytes unusable. */
export function normalizePageFrame(value) {
  if (value === undefined || value === null) return '9:16';
  if (!PAGE_FRAMES.includes(value)) {
    throw new RenderTreatmentError('FRAME_INVALID', `frame must be one of ${PAGE_FRAMES.join(', ')}`);
  }
  return value;
}

/** Absent or null is no saved fit. A fit rides only beside the non-9:16 frame
 * it fits into (pass the normalized frame); a fit on 9:16 or any other value
 * is refused, never silently dropped. Like the frame it is outside the visual
 * subset: the cut records it through `sourceRecipeTreatment`, and clips cut
 * before a fit change stay usable. */
export function normalizePageFrameFit(value, frame) {
  if (value === undefined || value === null) return null;
  if (!PAGE_FRAME_FITS.includes(value) || !PAGE_FRAMES.includes(frame) || frame === '9:16') {
    throw new RenderTreatmentError('FRAME_FIT_INVALID', `frameFit must be one of ${PAGE_FRAME_FITS.join(', ')} beside a 16:9, 1:1, 3:4 or 4:3 frame`);
  }
  return value;
}

/**
 * Canonical pre-render visual subset used when deciding whether existing bytes
 * remain reusable after a nonvisual dossier or Master Pages revision.
 */
export function normalizeVisualTreatment(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new RenderTreatmentError('RENDER_TREATMENT_MISSING', 'render treatment is required');
  }
  const allowed = ['stylePreset', 'filters', 'captionStyle', 'clipSpeed', 'clipCrop', 'frame', 'frameFit', 'captionDelivery'];
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    throw new RenderTreatmentError('RENDER_TREATMENT_INVALID', 'render treatment contains fields outside the locked recipe contract');
  }
  if (value.stylePreset !== undefined && value.stylePreset !== null) boundedPreset(value.stylePreset);
  normalizePageFrameFit(value.frameFit, normalizePageFrame(value.frame));
  normalizeCaptionDelivery(value.captionDelivery);
  const filters = normalizeFilters(value.filters);
  const clipSpeed = finite(value.clipSpeed ?? 1, 0.5, 2, 'clipSpeed');
  const clipCrop = normalizeClipCrop(value.clipCrop ?? { zoom: 1, focusX: 0.5, focusY: 0.5 });
  return Object.freeze({
    filters,
    clipSpeed,
    clipCrop,
  });
}

export function normalizeClipCrop(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).length !== 3
      || !Object.hasOwn(value, 'zoom')
      || !Object.hasOwn(value, 'focusX')
      || !Object.hasOwn(value, 'focusY')) {
    throw new RenderTreatmentError('CLIP_CROP_INVALID', 'clip crop must contain zoom, focusX, and focusY');
  }
  return Object.freeze({
    zoom: finite(value.zoom, 1, 3, 'clipCrop.zoom'),
    focusX: finite(value.focusX, 0, 1, 'clipCrop.focusX'),
    focusY: finite(value.focusY, 0, 1, 'clipCrop.focusY'),
  });
}

function normalizeFilters(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new RenderTreatmentError('FILTERS_INVALID', 'filters must be the locked Content Lab value map');
  }
  const entries = Object.entries(value);
  const unknown = entries.filter(([key]) => !Object.hasOwn(FILTER_FIELDS, key));
  if (unknown.length > 0) {
    throw new RenderTreatmentError('FILTERS_INVALID', 'filters contain fields outside the typed Content Lab contract');
  }
  const normalized = {};
  for (const [key, raw] of entries.sort(([left], [right]) => left.localeCompare(right))) {
    const [minimum, maximum] = FILTER_FIELDS[key];
    normalized[key] = finite(raw, minimum, maximum, `filters.${key}`);
  }
  return Object.freeze(normalized);
}

function boundedPreset(value) {
  if (typeof value !== 'string' || value.trim() !== value || value.length < 1 || value.length > 120) {
    throw new RenderTreatmentError('STYLE_PRESET_INVALID', 'stylePreset must be a bounded locked preset or null');
  }
  return value;
}

function finite(value, minimum, maximum, name) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new RenderTreatmentError('RENDER_TREATMENT_RANGE_INVALID', `${name} must be from ${minimum} to ${maximum}`);
  }
  return value;
}
