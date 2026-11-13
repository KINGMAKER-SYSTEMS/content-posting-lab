export const STYLE_KEYS = Object.freeze([
  'font', 'size_pt', 'color', 'outline', 'position', 'align', 'case',
  'background', 'background_color', 'offset_pct', 'line_balance', 'inverted',
  'outline_width_px', 'line_breaks',
]);

const ENUMS = Object.freeze({
  position: Object.freeze(['top', 'middle', 'bottom']),
  align: Object.freeze(['left', 'center', 'right']),
  case: Object.freeze(['as_written', 'lower', 'upper', 'title']),
  background: Object.freeze(['none', 'box', 'highlight']),
});

const HEX = /^#[0-9a-f]{6}$/i;
const FONT = /^TikTokSans[A-Za-z0-9.-]{0,112}\.ttf$/;
export const CAPTION_STYLE_DEFAULT_BACKGROUND_COLOR = '#000000';

export class CaptionStyleError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'CaptionStyleError';
    this.code = code;
  }
}

/**
 * Exact JavaScript wire validator for dossier-contracts::CaptionStyle.
 * It deliberately uses the Rust field names instead of inventing a second UI
 * vocabulary. Empty stays empty: a missing page default is never disguised as
 * a locked style.
 */
export function normalizeCaptionStyle(value, { requireComplete = false } = {}) {
  if (value === undefined || value === null) value = {};
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new CaptionStyleError('CAPTION_STYLE_INVALID', 'caption style must be an object');
  }
  const unknown = Object.keys(value).filter((key) => !STYLE_KEYS.includes(key));
  if (unknown.length > 0) {
    throw new CaptionStyleError('CAPTION_STYLE_INVALID', `caption style contains unknown field ${unknown.sort()[0]}`);
  }

  const result = {};
  if (value.font !== undefined && value.font !== null && value.font !== '') {
    if (typeof value.font !== 'string' || !FONT.test(value.font)) {
      throw new CaptionStyleError('CAPTION_STYLE_FONT_INVALID', 'font must name a Content Lab TikTokSans TTF');
    }
    result.font = value.font;
  }
  if (value.size_pt !== undefined && value.size_pt !== null) {
    result.size_pt = finiteRange(value.size_pt, 12, 96, 'size_pt');
  }
  for (const key of ['color', 'outline', 'background_color']) {
    if (value[key] === undefined || value[key] === null || value[key] === '') continue;
    if (typeof value[key] !== 'string' || !HEX.test(value[key])) {
      throw new CaptionStyleError('CAPTION_STYLE_COLOR_INVALID', `${key} must be #rrggbb`);
    }
    result[key] = value[key].toLowerCase();
  }
  for (const [key, options] of Object.entries(ENUMS)) {
    if (value[key] === undefined || value[key] === null || value[key] === '') continue;
    if (!options.includes(value[key])) {
      throw new CaptionStyleError('CAPTION_STYLE_ENUM_INVALID', `${key} is invalid`);
    }
    result[key] = value[key];
  }
  if (value.inverted !== undefined && value.inverted !== null) {
    if (typeof value.inverted !== 'boolean') {
      throw new CaptionStyleError('CAPTION_STYLE_INVERT_INVALID', 'inverted must be a boolean');
    }
    result.inverted = value.inverted;
  }
  if (value.offset_pct !== undefined && value.offset_pct !== null) {
    result.offset_pct = finiteRange(value.offset_pct, -40, 40, 'offset_pct');
  }
  if (value.line_balance !== undefined && value.line_balance !== null) {
    if (!Number.isInteger(value.line_balance) || value.line_balance < 0 || value.line_balance > 100) {
      throw new CaptionStyleError('CAPTION_STYLE_LINE_BALANCE_INVALID', 'line_balance must be an integer from 0 to 100');
    }
    result.line_balance = value.line_balance;
  }
  if (value.outline_width_px !== undefined && value.outline_width_px !== null) {
    if (!Number.isInteger(value.outline_width_px) || value.outline_width_px < 0 || value.outline_width_px > 20) {
      throw new CaptionStyleError('CAPTION_STYLE_OUTLINE_WIDTH_INVALID', 'outline_width_px must be an integer from 0 to 20');
    }
    result.outline_width_px = value.outline_width_px;
  }
  if (value.line_breaks !== undefined && value.line_breaks !== null) {
    if (!Array.isArray(value.line_breaks) || value.line_breaks.length < 1 || value.line_breaks.length > 24
        || value.line_breaks.some((line) => typeof line !== 'string' || line.length > 500
          || line.includes('\n') || line.includes('\r'))) {
      throw new CaptionStyleError('CAPTION_STYLE_LINES_INVALID', 'line_breaks must be 1 through 24 bounded lines');
    }
    result.line_breaks = Object.freeze([...value.line_breaks]);
  }

  // Content Lab's CaptionStyle model (services/caption_render.py
  // validate_background; the pinned contract's rules[0]) refuses a box or
  // highlight background with no background_color -- a style this function
  // let through would be accepted here and then refused at render time.
  const effectiveBackground = result.background ?? 'none';
  if (['box', 'highlight'].includes(effectiveBackground) && !result.background_color) {
    throw new CaptionStyleError('CAPTION_STYLE_BACKGROUND_COLOR_REQUIRED', 'background_color is required for box or highlight');
  }

  if (requireComplete) {
    const missing = ['font', 'size_pt', 'color', 'position', 'align', 'line_balance']
      .filter((key) => result[key] === undefined);
    if (missing.length > 0) {
      throw new CaptionStyleError('CAPTION_STYLE_INCOMPLETE', `caption style is missing ${missing.join(', ')}`);
    }
  }
  return Object.freeze(result);
}

/**
 * Normalize a caption style read from an already-persisted recipe. Older
 * recipes could store a box/highlight background while relying on Dossier's
 * displayed black default. Keep those rows renderable while raw write
 * validation (`normalizeCaptionStyle`) continues to require an explicit color.
 */
export function normalizePersistedCaptionStyle(value, options = {}) {
  if (value && typeof value === 'object' && !Array.isArray(value)
      && ['box', 'highlight'].includes(value.background)
      && (value.background_color === undefined || value.background_color === null || value.background_color === '')) {
    value = { ...value, background_color: CAPTION_STYLE_DEFAULT_BACKGROUND_COLOR };
  }
  return normalizeCaptionStyle(value, options);
}

export function isCompleteCaptionStyle(value) {
  try {
    normalizeCaptionStyle(value, { requireComplete: true });
    return true;
  } catch {
    return false;
  }
}

// The renderer (Content Lab) treats an ABSENT `outline_width_px` as 3px —
// exactly the width the Dossier's Outline-on toggle restores to
// (`outlineWidthToggle` in captionLayouts.js). The page recipe's caption style
// carries `case`, `background` and `offset_pct` as explicit seeds
// (materializer.js), but `outline_width_px` is deliberately NOT one of those
// seeds: it is the LAST fallback, applied only after a per-caption saved layout
// has had its say (applyCaptionLayout lets the page win its PAGE_RECIPE_KEYS
// and the layout win everything else). Seeding it into the page style would pin
// every caption to 3px and erase a saved per-caption width.
export const CAPTION_STYLE_COMPILE_DEFAULTS = Object.freeze({
  case: 'as_written', background: 'none', offset_pct: 0, outline_width_px: 3,
});

// The canonical final form of a slot's caption style: every key the renderer
// would otherwise default is explicit. Use this form wherever an old payload
// (missing the keys) and a new one (carrying the defaults) must not diverge —
// absent outline width == 3, absent case == as_written, absent background ==
// none, absent offset == 0. A null value is absent (normalizeCaptionStyle
// drops it), so it takes the default rather than deleting the key.
// A box/highlight that reached compile without a colour is completed here
// with the same DEFAULT_BACKGROUND_COLOR applyCaptionLayout fills for saved
// layouts (captionLayouts.js): compile is the LAST step before render and
// must never emit a colourless box the Lab would refuse. The
// CAPTION_STYLE_BACKGROUND_COLOR_REQUIRED guard in normalizeCaptionStyle
// still refuses that shape on raw saves, upstream of the per-caption catch.
export function compiledCaptionStyle(value) {
  // Fill the colour BEFORE the guard can refuse it: the inner normalize
  // throws CAPTION_STYLE_BACKGROUND_COLOR_REQUIRED on a colourless box, but
  // compile is the last step before render and completes it instead -- the
  // same DEFAULT_BACKGROUND_COLOR applyCaptionLayout fills for saved layouts.
  const raw = (value && typeof value === 'object' && !Array.isArray(value)) ? { ...value } : value;
  if (raw && (raw.background === 'box' || raw.background === 'highlight') && !raw.background_color) {
    raw.background_color = CAPTION_STYLE_DEFAULT_BACKGROUND_COLOR;
  }
  return normalizeCaptionStyle({
    ...CAPTION_STYLE_COMPILE_DEFAULTS, ...normalizeCaptionStyle(raw, { requireComplete: true }),
  }, { requireComplete: true });
}

function finiteRange(value, minimum, maximum, name) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new CaptionStyleError('CAPTION_STYLE_RANGE_INVALID', `${name} must be from ${minimum} to ${maximum}`);
  }
  return value;
}
