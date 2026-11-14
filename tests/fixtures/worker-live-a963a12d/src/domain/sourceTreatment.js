import { normalizeRenderTreatment, normalizeVisualTreatment } from './renderTreatment.js';
import { stableStringify } from './policy.js';
import { clipTreatmentRequest } from './clipVariation.js';

const NEUTRALS = Object.freeze({ brightness: 1, contrast: 1, saturation: 1, warmth: 0, fade: 0, grain: 0, vignette: 0 });
const FIELDS = ['schema', 'sourceSha256', 'visualTreatment', 'sourceRecipeTreatment', 'recipeSpecHash', 'generationJobId'];
const HASH = /^(?:sha256:)?[a-f0-9]{64}$/;

export function effectiveSourceVisualTreatment(value) {
  const visual = normalizeVisualTreatment(value);
  return { filters: { ...NEUTRALS, ...visual.filters }, clipSpeed: visual.clipSpeed, clipCrop: visual.clipCrop };
}

/**
 * Every leaf of the canonical visual treatment that sourcePreparationBlocker
 * compares, each with the value the REQUESTED treatment takes when the recipe
 * omits it. The table is derived by running the comparison's own default path
 * (`effectiveSourceVisualTreatment` over an empty treatment), not written out,
 * so a new filter or a changed neutral appears here with no second edit.
 *
 * It exists because the bounded per-page reads that must not let unpreparable
 * rows fill a window have to apply the same comparison in SQL, before the JS
 * gate ever sees a row. `preparableSourceSql`
 * (src/scheduler/visualRecipeCompatibility.js) GENERATES that predicate from
 * this table, so the field list and the fallbacks exist exactly once.
 */
export function sourceVisualTreatmentDefaults() {
  return Object.freeze(visualLeaves(effectiveSourceVisualTreatment({ filters: {} }), '')
    .map((leaf) => Object.freeze(leaf)));
}

function visualLeaves(value, prefix) {
  return Object.entries(value).flatMap(([key, entry]) => {
    const path = prefix === '' ? key : `${prefix}.${key}`;
    return entry !== null && typeof entry === 'object'
      ? visualLeaves(entry, path)
      : [{ path, fallback: entry }];
  });
}

/** Match Content Lab's final renderer: source bytes must carry a producer
 * receipt for the exact visual treatment already applied. The render service
 * requires this receipt even for a neutral crop/speed/filter request, so a
 * receiptless asset cannot enter scheduler depth or a prepared post. On a page
 * that varies its clips (domain/clipVariation.js) the clip's own speed and
 * crop are what its post asks for, so only the grade meets the recipe. */
export function sourcePreparationBlocker(payload, requestedTreatment, { pageId = null } = {}) {
  if (payload?.sourceTreatment === undefined || payload?.sourceTreatment === null) {
    return 'SOURCE_TREATMENT_PROVENANCE_MISSING';
  }
  try {
    if (typeof payload.contentLabJobId !== 'string' || !payload.contentLabJobId) {
      return 'SOURCE_TREATMENT_BINDING_INVALID';
    }
    const receipt = validateSourceTreatment(payload.sourceTreatment, {
      sourceSha256: payload.sourceSha256, generationJobId: payload.contentLabJobId,
    });
    return stableStringify(receipt.visualTreatment)
      === stableStringify(effectiveSourceVisualTreatment(clipTreatmentRequest(requestedTreatment, payload, pageId)))
      ? null : 'SOURCE_TREATMENT_MISMATCH';
  } catch {
    return 'SOURCE_TREATMENT_BINDING_INVALID';
  }
}

/** Actual producer receipt only. CaptionStyle is recipe context, never evidence
 * of text burned into a source. No requested slot value fills a missing field. */
export function validateSourceTreatment(value, { sourceSha256, generationJobId, recipeSpecHash } = {}) {
  try {
    if (!value || typeof value !== 'object' || Array.isArray(value)
        || Object.keys(value).some((key) => ![...FIELDS, 'derivedFrom'].includes(key))
        || FIELDS.some((key) => !Object.hasOwn(value, key))
        || value.schema !== 'content-lab.source-treatment.v1'
        || !/^[a-f0-9]{64}$/.test(value.sourceSha256) || !HASH.test(value.recipeSpecHash)
        || typeof value.generationJobId !== 'string' || !/^[A-Za-z0-9._:-]{1,200}$/.test(value.generationJobId)
        || !value.visualTreatment || Object.keys(value.visualTreatment).sort().join(',') !== 'clipCrop,clipSpeed,filters'
        || Object.keys(value.visualTreatment.filters).sort().join(',') !== Object.keys(NEUTRALS).sort().join(',')) throw new Error();
    const visual = effectiveSourceVisualTreatment(value.visualTreatment);
    if (stableStringify(visual) !== stableStringify(value.visualTreatment)) throw new Error();
    normalizeRenderTreatment(value.sourceRecipeTreatment);
    if (value.derivedFrom !== undefined) {
      const parent = value.derivedFrom;
      if (!parent || Object.keys(parent).sort().join(',') !== 'generationJobId,sourceSha256'
          || !/^[a-f0-9]{64}$/.test(parent.sourceSha256) || parent.sourceSha256 === value.sourceSha256
          || !/^[A-Za-z0-9._:-]{1,200}$/.test(parent.generationJobId)) throw new Error();
    }
    if (sourceSha256 !== undefined && value.sourceSha256 !== sourceSha256) throw new Error();
    if (generationJobId !== undefined && value.generationJobId !== generationJobId) throw new Error();
    if (recipeSpecHash !== undefined && (!HASH.test(recipeSpecHash)
        || value.recipeSpecHash.replace(/^sha256:/, '') !== recipeSpecHash.replace(/^sha256:/, ''))) throw new Error();
    return JSON.parse(JSON.stringify(value));
  } catch {
    throw Object.assign(new Error('actual source treatment receipt is malformed or bound to different bytes/job/recipe'), {
      code: 'SOURCE_TREATMENT_BINDING_INVALID',
    });
  }
}
