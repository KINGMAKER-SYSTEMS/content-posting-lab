import { isTwinSingleImageSource, managedPhotoPlanValid } from '../domain/managedSlideshowPages.js';
import { validateCropDeliveryGroups } from '../domain/cropDelivery.js';
import { earliestSourceStartMs, SOURCE_VIDEO_BOUNDS } from '../domain/sourceVideoBounds.js';
import { stableStringify } from '../domain/policy.js';
import { validateSourceTreatment } from '../domain/sourceTreatment.js';
import { validateLyricCatalogBinding, validateLyricCapsuleShape } from '../domain/lyricTiming.js';
import { FORMAT_SLUG_PATTERN } from '../domain/recipeDemand.js';
import { PostRenderClient } from './postRenderClient.js';
import { CONTENT_LAB_CALL_TIMEOUT_MS } from './contentLabTimeouts.js';

const DEFAULT_TIMEOUT_MS = 3_000;
// Explicit, sane bounds a caller-supplied timeoutMs must fall within. 1ms is
// the practical floor (anything below is indistinguishable from "always
// aborts"); 5 minutes is a generous ceiling for a control-plane call before
// it's clearly hung — well past this, holding the request open serves no one.
const MIN_TIMEOUT_MS = 1;
const MAX_TIMEOUT_MS = 300_000;

function callTimeoutMs(kind, callerBudgetMs = MAX_TIMEOUT_MS) {
  const timeout = CONTENT_LAB_CALL_TIMEOUT_MS[kind];
  if (!Number.isInteger(timeout)) throw closed('CONTENT_LAB_TIMEOUT_INVALID', `unknown Content Lab timeout ${kind}`);
  return Math.min(timeout, callerBudgetMs, MAX_TIMEOUT_MS);
}
const DEFAULT_RETRIES = 2;
const MIN_RETRIES = 0;
const MAX_RETRIES = 3;
const RETRY_STATUSES = new Set([408, 429, 500, 502, 503, 504]);
const JOB_FIELDS = new Set([
  'pageId', 'lane', 'engine', 'lockedRecipeId', 'recipeVersion', 'quantity',
  'constraints', 'sourceIsolation', 'policyHash', 'masterPages', 'masterPagesHash',
]);
const RECIPE_PUBLICATION_FIELDS = new Set([
  'schema', 'pageId', 'lane', 'recipeId', 'engine', 'recipeVersion',
  'dossierRevision', 'recipeSpecHash', 'recipeSpecCanonical',
]);
const SOURCE_IMPORT_FIELDS = new Set([
  'schema', 'pageId', 'format', 'sourceUrl', 'masterPages', 'masterPagesHash',
]);
const PROMPT_KEY = /(prompt|instruction|message)/i;

const RESPONSE_SCHEMA_ID = 'content-lab.response.v1';
const FORMAT_CONTRACT_STATUS_SCHEMA_ID = 'content-lab.format-contract-status.v1';
const DOSSIER_INGREDIENT_CATALOG_SCHEMA_ID = 'content-lab.dossier-ingredient-catalog.v1';
const JOB_STATUS_VALUES = new Set([
  'queued', 'submitted', 'processing', 'running',
  'completed', 'succeeded', 'ready', 'failed', 'error', 'cancelled',
]);

const DEFAULT_BASE_DELAY_MS = 100;
const DEFAULT_MAX_BACKOFF_MS = 2_000;
const DEFAULT_MAX_RETRY_AFTER_MS = 10_000;

// Explicit, conservative caps on response cardinality/size so a compromised or
// misbehaving Content Lab origin cannot force unbounded memory use in the worker.
const MAX_CAPABILITY_ENTRIES = 200;
// A zeroReason code is an opaque string from the Lab's plan-reasons vocabulary;
// it is carried through, never acted on by the worker, so only its shape and
// length are bounded — the code value itself is open.
const MAX_ZERO_REASON_LENGTH = 200;
const MAX_ARTIFACT_ENTRIES = 500;
export const MAX_FORMAT_CONTRACT_ENTRIES = 200;
const MAX_DOSSIER_INGREDIENT_ENTRIES = 20;
const MAX_ARTIFACT_URL_LENGTH = 2_048;
// This is the real fleet-response limit: readBoundedBody enforces it before
// JSON.parse. Do not add a roster row cap beside it. The measured 1,000-page
// roster is 461,050 bytes (23.05% of this limit), while a 500-row cap blocked
// that safe payload at half the operating fleet for no platform reason.
export const MAX_CONTENT_LAB_RESPONSE_BODY_BYTES = 2_000_000;
const MAX_REQUEST_ID_LENGTH = 200;
const MAX_PAGE_ID_LENGTH = 200;
const MAX_IDEMPOTENCY_KEY_LENGTH = 200;
const MAX_RECIPE_SPEC_BYTES = 131_072;
// Shared bounded-safe-token shape for every value that flows verbatim into a
// request header (X-Request-Id, X-RT-Page-Id, Idempotency-Key). No CR/LF, no
// delimiters, no whitespace — nothing that could inject/split a header line.
const HEADER_TOKEN_PATTERN = /^[A-Za-z0-9_.:-]+$/;

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}
function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}
function isNonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0;
}
function isBoundedNumber(min, max) {
  return (value) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
}
function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isBoundedString(value, maxLength = 600) {
  return typeof value === 'string' && value.length > 0 && value.length <= maxLength && value === value.trim();
}

const FIELD_VALIDATORS = {
  nonEmptyString: isNonEmptyString,
  object: isPlainObject,
  array: Array.isArray,
  boolean: (value) => typeof value === 'boolean',
  positiveInteger: isPositiveInteger,
  nonNegativeInteger: isNonNegativeInteger,
  progress: isBoundedNumber(0, 100),
  terminalError: (value) => isBoundedString(value, 300),
};

function isHttpsUrl(value, maxLength) {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength) return false;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  // Require https explicitly — rejects javascript:, data:, file:, ftp:, http:, and
  // any other scheme regardless of how the string is spelled/encoded.
  return parsed.protocol === 'https:';
}

const CAPABILITY_ENTRY_ALLOWED_FIELDS = new Set(['recipeId', 'engine', 'recipeVersion', 'maxQuantity', 'sourceIdentities', 'zeroReason']);
function validateCapabilityEntry(entry, index) {
  if (!isPlainObject(entry)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}] must be an object`);
  }
  const unknown = Object.keys(entry).filter((key) => !CAPABILITY_ENTRY_ALLOWED_FIELDS.has(key));
  if (unknown.length > 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}] has unknown fields: ${unknown.join(', ')}`);
  }
  if (!isNonEmptyString(entry.recipeId)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}].recipeId must be a non-empty string`);
  if (!isNonEmptyString(entry.engine)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}].engine must be a non-empty string`);
  if (!isNonEmptyString(entry.recipeVersion)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}].recipeVersion must be a non-empty string`);
  if (entry.sourceIdentities !== undefined
      && (!Array.isArray(entry.sourceIdentities)
        || entry.sourceIdentities.length > 1000
        || entry.sourceIdentities.some((url) => !isHttpsUrl(url, MAX_ARTIFACT_URL_LENGTH)))) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'capability sourceIdentities must be bounded original HTTPS source URLs');
  }
  if (!isNonNegativeInteger(entry.maxQuantity)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}].maxQuantity must be a non-negative integer`);
  // Content Lab #267 attaches a plan-reason code to maxQuantity-0 entries. The
  // worker carries it through untouched and makes no decision on it, so the
  // code value is open — only the shape is bounded (a non-empty, trimmed string
  // of at most MAX_ZERO_REASON_LENGTH chars).
  if (entry.zeroReason !== undefined && !isBoundedString(entry.zeroReason, MAX_ZERO_REASON_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response entry [${index}].zeroReason must be a non-empty trimmed string of at most ${MAX_ZERO_REASON_LENGTH} characters`);
  }
}

// The roster snapshot's per-page ontology — exactly the fields the Lab's
// contract allowlists (never credentials), each nullable so "Notion does
// not say" crosses as a true statement rather than a dropped key.
const ROSTER_PAGE_ALLOWED_FIELDS = new Set([
  'pageId', 'handle', 'group', 'groupLabel', 'pageType', 'accountType',
  'posterName', 'status', 'project', 'tiktokUrl', 'notionPageId', 'source',
  'contentNiche', 'contentEngine', 'automationMode', 'vaultUrl', 'pipeline',
  'soundsReference', 'archived',
]);
const ROSTER_PAGE_NULLABLE_FIELDS = [
  'group', 'groupLabel', 'pageType', 'accountType', 'posterName',
  'status', 'project', 'tiktokUrl', 'notionPageId', 'source', 'contentNiche',
  'contentEngine', 'automationMode', 'vaultUrl', 'pipeline', 'soundsReference',
];

function validateRosterPageEntry(entry, index) {
  if (!isPlainObject(entry)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}] must be an object`);
  }
  const unknown = Object.keys(entry).filter((key) => !ROSTER_PAGE_ALLOWED_FIELDS.has(key));
  if (unknown.length > 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}] has unknown fields: ${unknown.join(', ')}`);
  }
  if (!isNonEmptyString(entry.pageId) || entry.pageId !== entry.pageId.trim()) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].pageId must be a trimmed non-empty string`);
  }
  if (!isNonEmptyString(entry.handle)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].handle must be a non-empty string`);
  for (const field of ROSTER_PAGE_NULLABLE_FIELDS) {
    if (!Object.hasOwn(entry, field)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].${field} is required; use null when Master Pages does not say`);
    }
    if (entry[field] !== null && typeof entry[field] !== 'string') {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].${field} must be a string or null`);
    }
  }
  if (!Object.hasOwn(entry, 'archived') || typeof entry.archived !== 'boolean') {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].archived must be an explicit boolean`);
  }
  if (entry.vaultUrl !== null && !isHttpsUrl(entry.vaultUrl, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `roster response entry [${index}].vaultUrl must be a well-formed https URL or null`);
  }
}

function validateMasterPagesIntent(entry, index) {
  if (!isPlainObject(entry) || entry.schema !== 'master-pages.page-intent.v1') {
    throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `Master Pages intent [${index}] schema is invalid`);
  }
  const { schema: _schema, ...page } = entry;
  validateRosterPageEntry(page, index);
}

const DOSSIER_FORMAT_FIELDS_V1 = new Set([
  'formatId', 'recipeId', 'contentNiche', 'contentEngine', 'materialSource',
  'definitionStatus', 'executionStatus', 'formatContractVersion',
  'executor', 'ingredients', 'review', 'blockers',
]);
const DOSSIER_FORMAT_FIELDS_WITH_SELECTIONS = new Set([
  ...DOSSIER_FORMAT_FIELDS_V1, 'productionSelections',
]);
const DOSSIER_INGREDIENT_FIELDS = new Set([
  'ingredientId', 'kind', 'required', 'status', 'binding', 'options',
]);
const DOSSIER_PRODUCTION_SELECTION_FIELDS = new Set([
  'catalogVersion', 'providerId', 'modelId', 'promptModuleId',
  'referenceSetId', 'sourceLibraryId',
]);

function validateNumericDomain(schema, label, minimumKey, maximumKey) {
  if (![minimumKey, maximumKey, 'step', 'default'].every((key) => Object.hasOwn(schema, key))
      || ![schema[minimumKey], schema[maximumKey], schema.step, schema.default]
        .every((value) => Number.isFinite(value))
      || schema[minimumKey] > schema[maximumKey]
      || schema.step <= 0
      || schema.default < schema[minimumKey]
      || schema.default > schema[maximumKey]) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} has an incomplete numeric domain`);
  }
}

function isOpaqueControlEntry(value) {
  if (value === null || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value === 'string') return value.length <= 200;
  if (!isPlainObject(value)) return false;
  return !Object.hasOwn(value, 'type') || isBoundedString(value.type, 64);
}

function validateIngredientControls(value, label) {
  if (value === undefined) return;
  if (!isPlainObject(value)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be an object`);
  for (const [key, schema] of Object.entries(value)) {
    if (key === '_advanced') continue;
    // The Lab's control vocabulary is open (its OpenAPI types this payload as
    // free-form, and the live catalog already carries entries this adapter
    // never named -- `image_required: true`, a bare flag beside the typed
    // controls, took the whole catalog down on 2026-09-11 when this arm
    // refused everything outside toggle/select/range). A control entry is a
    // bounded primitive (a flag, a number, a short string, null) or an object;
    // an object's `type`, when present, must be a bounded string, and only the
    // three kinds this adapter RENDERS keep their shape checks -- every other
    // entry passes through opaque for the console to show as unsupported. A
    // list is never a control and is still refused by name.
    if (!isOpaqueControlEntry(schema)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${key} is invalid`);
    }
    if (!isPlainObject(schema)) continue;
    if (schema.label !== undefined && !isBoundedString(schema.label, 200)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${key}.label is invalid`);
    }
    if (schema.type === 'range') validateNumericDomain(schema, `${label}.${key}`, 'min', 'max');
    if (schema.type === 'toggle'
        && Object.hasOwn(schema, 'default') && typeof schema.default !== 'boolean') {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${key}.default is invalid`);
    }
    if (schema.type === 'select'
        && (!Array.isArray(schema.options) || schema.options.length === 0)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${key}.options is invalid`);
    }
  }
}

function validateIngredientFilters(value, label) {
  if (value === undefined || value === null) return;
  if (!isPlainObject(value)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be an object`);
  for (const [key, schema] of Object.entries(value)) {
    if (!isPlainObject(schema) || !isBoundedString(schema.label, 200)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${key} is invalid`);
    }
    validateNumericDomain(schema, `${label}.${key}`, 'minimum', 'maximum');
  }
}

function validateDossierProductionSelection(entry, label, formatId) {
  const timed = formatId === 'lyric-video';
  assertExactFields(entry, timed ? new Set([...DOSSIER_PRODUCTION_SELECTION_FIELDS,'controls']) : DOSSIER_PRODUCTION_SELECTION_FIELDS, label);
  if (timed && (!isPlainObject(entry.controls) || Object.keys(entry.controls).length !== 1
      || !isBoundedString(entry.controls.lyricTimingSetId,512))) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.controls is invalid`);
  }
  const nullableIds = [
    entry.providerId, entry.modelId, entry.promptModuleId,
    entry.referenceSetId, entry.sourceLibraryId,
  ];
  if (!SHA256_VERSION_PATTERN.test(entry.catalogVersion)
      || nullableIds.some((value) => value !== null && !isBoundedString(value, 1_024))
      || (entry.providerId === null) !== (entry.modelId === null)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} is invalid`);
  }
}

function validateDossierIngredient(entry, label) {
  if (!isPlainObject(entry)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be an object`);
  const timing = entry.ingredientId === 'lyric-timing';
  assertExactFields(entry, timing ? new Set([...DOSSIER_INGREDIENT_FIELDS,'selectionControl']) : DOSSIER_INGREDIENT_FIELDS, label);
  if (timing) {
    if (entry.selectionControl !== 'lyricTimingSetId' || entry.kind !== 'timed_lyrics') {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.selectionControl is invalid`);
    }
    try {
      if (entry.binding !== null) validateLyricCatalogBinding(entry.binding);
      if (Array.isArray(entry.options)) entry.options.forEach(validateLyricCatalogBinding);
    } catch { throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.timing binding is invalid`); }
  }
  if (![entry.ingredientId, entry.kind, entry.status].every((value) => isBoundedString(value, 100))
      || typeof entry.required !== 'boolean'
      || (entry.binding !== null && !isPlainObject(entry.binding))
      || !Array.isArray(entry.options)
      || entry.options.length > MAX_CAPABILITY_ENTRIES
      || entry.options.some((option) => !isPlainObject(option))) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} is invalid`);
  }
  validateIngredientControls(entry.binding?.controls, `${label}.binding.controls`);
  validateIngredientFilters(entry.binding?.filters, `${label}.binding.filters`);
  entry.options.forEach((option, index) => {
    validateIngredientControls(option.controls, `${label}.options[${index}].controls`);
    validateIngredientFilters(option.filters, `${label}.options[${index}].filters`);
  });
}

function validateDossierFormat(entry, index) {
  const label = `dossier ingredient format [${index}]`;
  if (!isPlainObject(entry)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be an object`);
  assertExactFields(
    entry,
    Object.hasOwn(entry, 'productionSelections')
      ? DOSSIER_FORMAT_FIELDS_WITH_SELECTIONS : DOSSIER_FORMAT_FIELDS_V1,
    label,
  );
  // formatId and the format contract's formatSlug are ONE namespace, not two:
  // dossierLiveContext.js:135 joins `entry.formatId === recipeKey.formatSlug`,
  // and that slug is bounded by the one shared rule (FORMAT_SLUG_PATTERN,
  // domain/recipeDemand.js:9, used here and at :628); the operator's submit door
  // must agree and now reads that same constant, not a copy of it (ops/sourceImports.js:4,:739). A free 240-char bound
  // let the Lab emit a candidate this deployment accepts into the catalog, lists
  // in the console dropdown as authorized and not stale (dossier/nativeClient.js
  // :531-537), and then refuses at submit as SOURCE_IMPORT_REQUEST_INVALID 400 --
  // the operator's request named invalid for a value the console handed them, and
  // no other format is reachable for that page. A non-slug formatId is the Lab and
  // this deployment disagreeing about the namespace, so it is refused at the Lab
  // door as `CONTENT_LAB_SCHEMA_MISMATCH`, whose remedy is the true one.
  if (!FORMAT_SLUG_PATTERN.test(entry.formatId)
      || ![entry.recipeId, entry.contentNiche, entry.contentEngine, entry.materialSource]
        .every((value) => isBoundedString(value, 240))
      || !['complete', 'incomplete'].includes(entry.definitionStatus)
      || !['commissioned', 'uncommissioned'].includes(entry.executionStatus)
      || !SHA256_VERSION_PATTERN.test(entry.formatContractVersion)
      || (entry.executor !== null && !isPlainObject(entry.executor))
      || !Array.isArray(entry.ingredients)
      || entry.ingredients.length > MAX_DOSSIER_INGREDIENT_ENTRIES
      || !Array.isArray(entry.blockers)
      || entry.blockers.some((blocker) => !isBoundedString(blocker, 200))
      || !isPlainObject(entry.review)
      || !Object.hasOwn(entry.review, 'authority')
      || !Array.isArray(entry.review.gates)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} is invalid`);
  }
  entry.ingredients.forEach((ingredient, ingredientIndex) => (
    validateDossierIngredient(ingredient, `${label}.ingredients[${ingredientIndex}]`)
  ));
  if (Object.hasOwn(entry, 'productionSelections')) {
    if (!Array.isArray(entry.productionSelections)
        || entry.productionSelections.length > MAX_CAPABILITY_ENTRIES) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.productionSelections is invalid`);
    }
    entry.productionSelections.forEach((selection, selectionIndex) => (
      validateDossierProductionSelection(
        selection, `${label}.productionSelections[${selectionIndex}]`, entry.formatId,
      )
    ));
    const identities = entry.productionSelections.map((selection) => JSON.stringify([
      selection.providerId, selection.modelId, selection.promptModuleId,
      selection.referenceSetId, selection.sourceLibraryId,
    ]));
    if (new Set(identities).size !== identities.length) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.productionSelections must be unique`);
    }
  }
}

const ARTIFACT_ENTRY_ALLOWED_FIELDS = new Set(['url', 'type', 'sha256', 'bytes', 'source', 'thumbnail', 'delivery', 'sourceTreatment', 'lyricTiming']);
const ARTIFACT_APPROVED_LIBRARY_SOURCE_FIELDS = new Set([
  'recipeId', 'recipeVersion', 'path', 'sha256', 'bytes', 'pageId',
  'masterPagesHash', 'contentNiche', 'contentEngine', 'vaultUrl',
]);
const ARTIFACT_SOURCE_DNA_FIELDS = new Set([
  'recipeId', 'sourceLibraryId', 'sourceLibraryVersion', 'master', 'cutWindow',
  'pageId', 'masterPagesHash', 'contentNiche', 'contentEngine', 'vaultUrl',
]);
const ARTIFACT_SLIDESHOW_SOURCE_FIELDS = new Set([
  'schema', 'kind', 'recipeId', 'recipeVersion', 'sourceLibraryId',
  'librarySnapshotHash', 'templateSlug', 'planSignature', 'rendererRevision',
  'renderJobId', 'media', 'pageId', 'masterPagesHash', 'contentNiche',
  'contentEngine', 'vaultUrl',
]);
const ARTIFACT_SLIDESHOW_MEDIA_FIELDS = new Set(['objectKey', 'bytes', 'etag', 'sha256']);
const ARTIFACT_SOURCE_DNA_MASTER_FIELDS = new Set([
  'sourceId', 'sha256', 'bytes', 'filename', 'mimeType', 'storageKey',
  'durationMs', 'sourceOffsetMs', 'provenance',
]);
const ARTIFACT_SOURCE_DNA_PROVENANCE_FIELDS = new Set([
  'acquiredAt', 'authority', 'sourceUrl',
]);
const ARTIFACT_SOURCE_DNA_CUT_FIELDS = new Set([
  'libraryStartMs', 'libraryEndMs', 'durationMs', 'originalStartMs', 'originalEndMs',
]);
const ARTIFACT_PAGE_SOURCE_IMPORT_FIELDS = new Set([
  'schema', 'kind', 'pageId', 'format', 'sourceUrl', 'sha256', 'bytes',
  'mimeType', 'media', 'original', 'masterPagesHash', 'contentNiche', 'contentEngine', 'vaultUrl',
]);
const ARTIFACT_PAGE_SOURCE_IMPORT_MEDIA_FIELDS = new Set([
  'durationMs', 'width', 'height', 'videoCodec', 'pixelFormat', 'fps', 'audioStreams',
]);
const ARTIFACT_THUMBNAIL_ALLOWED_FIELDS = new Set(['url', 'sha256', 'bytes', 'type']);
const ARTIFACT_DELIVERY_FIELDS = new Set(['aspectRatio', 'width', 'height', 'crop']);
const ARTIFACT_CROP_FIELDS = new Set(['mode', 'index', 'count', 'groupId', 'sourceSha256']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function validateArtifactSourceAuthority(source, index) {
  const label = `artifacts response entry [${index}].source`;
  for (const field of ['recipeId', 'recipeVersion', 'path', 'pageId', 'contentNiche', 'contentEngine', 'vaultUrl']) {
    if (!isNonEmptyString(source[field])) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${field} must be a non-empty string`);
    }
  }
  if (!SHA256_PATTERN.test(source.sha256)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.sha256 must be lowercase SHA-256 hex`);
  }
  if (!isPositiveInteger(source.bytes)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.bytes must be a positive integer`);
  }
  if (!/^sha256:[0-9a-f]{64}$/.test(source.masterPagesHash)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.masterPagesHash must be a hash-bound Master Pages revision`);
  }
  if (!isHttpsUrl(source.vaultUrl, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.vaultUrl must be a well-formed https URL`);
  }
}

function validateArtifactSourceDna(source, index) {
  const label = `artifacts response entry [${index}].source`;
  assertExactFields(source, ARTIFACT_SOURCE_DNA_FIELDS, label);
  for (const field of ['recipeId', 'sourceLibraryId', 'pageId', 'contentNiche', 'contentEngine']) {
    if (!isBoundedString(source[field], 240)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.${field} must be a bounded non-empty string`);
    }
  }
  if (!SHA256_VERSION_PATTERN.test(source.sourceLibraryVersion)
      || !SHA256_VERSION_PATTERN.test(source.masterPagesHash)
      || !isHttpsUrl(source.vaultUrl, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} authority is invalid`);
  }

  assertExactFields(source.master, ARTIFACT_SOURCE_DNA_MASTER_FIELDS, `${label}.master`);
  const master = source.master;
  if (!isBoundedString(master.sourceId, 240)
      || !SHA256_PATTERN.test(master.sha256)
      || !Number.isSafeInteger(master.bytes) || master.bytes <= 0
      || !isBoundedString(master.filename, 512)
      || master.mimeType !== 'video/mp4'
      || !isBoundedString(master.storageKey, 1_024)
      || !Number.isSafeInteger(master.durationMs) || master.durationMs <= 0
      || !Number.isSafeInteger(master.sourceOffsetMs) || master.sourceOffsetMs < 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.master is invalid`);
  }

  assertExactFields(master.provenance, ARTIFACT_SOURCE_DNA_PROVENANCE_FIELDS, `${label}.master.provenance`);
  const provenance = master.provenance;
  if (!isBoundedString(provenance.acquiredAt, 100)
      || !Number.isFinite(Date.parse(provenance.acquiredAt))
      || !isBoundedString(provenance.authority, 600)
      || !isHttpsUrl(provenance.sourceUrl, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.master.provenance is invalid`);
  }

  assertExactFields(source.cutWindow, ARTIFACT_SOURCE_DNA_CUT_FIELDS, `${label}.cutWindow`);
  const cut = source.cutWindow;
  const cutFields = [...ARTIFACT_SOURCE_DNA_CUT_FIELDS];
  if (cutFields.some((field) => !Number.isSafeInteger(cut[field]) || cut[field] < 0)
      || cut.durationMs < SOURCE_VIDEO_BOUNDS.minDurationMs || cut.durationMs > SOURCE_VIDEO_BOUNDS.maxDurationMs
      || cut.libraryEndMs !== cut.libraryStartMs + cut.durationMs
      || cut.originalStartMs !== master.sourceOffsetMs + cut.libraryStartMs
      || cut.originalEndMs !== master.sourceOffsetMs + cut.libraryEndMs
      || cut.libraryEndMs > master.durationMs
      || cut.originalStartMs < earliestSourceStartMs(source)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.cutWindow must be a 5–10 second window within the permitted immutable master timeline`);
  }
}

function validatePageSourceImport(source, index) {
  const label = `artifacts response entry [${index}].source`;
  assertExactFields(source, ARTIFACT_PAGE_SOURCE_IMPORT_FIELDS, label);
  if (source.schema !== 'content-lab.page-source-import-source.v1'
      || source.kind !== 'page_source_import'
      || !isBoundedString(source.pageId, 200)
      || !isBoundedString(source.format, 100)
      || !isHttpsUrl(source.sourceUrl, MAX_ARTIFACT_URL_LENGTH)
      || !SHA256_PATTERN.test(source.sha256)
      || !Number.isSafeInteger(source.bytes) || source.bytes <= 0
      || source.mimeType !== 'video/mp4'
      || !SHA256_VERSION_PATTERN.test(source.masterPagesHash)
      || !isBoundedString(source.contentNiche, 240)
      || source.contentEngine !== 'sourced_video'
      || !isHttpsUrl(source.vaultUrl, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} source-import authority is invalid`);
  }
  assertExactFields(source.media, ARTIFACT_PAGE_SOURCE_IMPORT_MEDIA_FIELDS, `${label}.media`);
  if (!validImportedMedia(source.media)
      || source.media.width !== 1080
      || source.media.height !== 1920
      || source.media.videoCodec !== 'h264'
      || source.media.pixelFormat !== 'yuv420p'
      || Math.abs(source.media.fps - 30) > 0.01
      || source.media.audioStreams !== 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.media is invalid`);
  }
  assertExactFields(source.original, new Set(['sha256', 'bytes', 'mimeType', 'media']), `${label}.original`);
  assertExactFields(source.original.media, ARTIFACT_PAGE_SOURCE_IMPORT_MEDIA_FIELDS, `${label}.original.media`);
  if (!SHA256_PATTERN.test(source.original.sha256)
      || !Number.isSafeInteger(source.original.bytes) || source.original.bytes <= 0
      || source.original.mimeType !== 'video/mp4'
      || !validImportedMedia(source.original.media)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.original is invalid`);
  }
}

function validImportedMedia(media) {
  return isPlainObject(media)
    && Number.isSafeInteger(media.durationMs) && media.durationMs > 0
    && Number.isSafeInteger(media.width) && media.width > 0
    && Number.isSafeInteger(media.height) && media.height > 0
    && isBoundedString(media.videoCodec, 100)
    && isBoundedString(media.pixelFormat, 100)
    && typeof media.fps === 'number' && Number.isFinite(media.fps) && media.fps > 0
    && Number.isSafeInteger(media.audioStreams) && media.audioStreams >= 0;
}

function validateSlideshowSource(source, index) {
  const label = `artifacts response entry [${index}].source`;
  const photoFormat = ['image-video:master','single-image-video:master'].includes(source.recipeId);
  assertExactFields(source, photoFormat ? new Set([...ARTIFACT_SLIDESHOW_SOURCE_FIELDS, 'photoPlan'])
    : ARTIFACT_SLIDESHOW_SOURCE_FIELDS, label);
  if (photoFormat && !managedPhotoPlanValid(source)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} photo plan is invalid`);
  }
  if (source.schema !== 'content-lab.syzygy-slideshow-source.v1'
      || source.kind !== 'syzygy_slideshow'
      || (!['sourced_slideshow', 'lyrics_slideshows'].includes(source.contentEngine) && !isTwinSingleImageSource(source))
      || (source.recipeId === 'single-image-video:master' && !isTwinSingleImageSource(source))
      || !['recipeId', 'recipeVersion', 'sourceLibraryId', 'templateSlug',
        'renderJobId', 'pageId', 'contentNiche'].every(field => isBoundedString(source[field], 240))
      || !SHA256_PATTERN.test(source.librarySnapshotHash)
      || !SHA256_PATTERN.test(source.planSignature)
      || !isBoundedString(source.rendererRevision, 64)
      || !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(source.rendererRevision)
      || !SHA256_VERSION_PATTERN.test(source.masterPagesHash)
      || !isHttpsUrl(source.vaultUrl, MAX_ARTIFACT_URL_LENGTH)
      || !Array.isArray(source.media) || source.media.length === 0 || source.media.length > 1000) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} slideshow receipt is invalid`);
  }
  for (const media of source.media) {
    assertExactFields(media, ARTIFACT_SLIDESHOW_MEDIA_FIELDS, `${label}.media`);
    if (!isBoundedString(media.objectKey, 512) || !isPositiveInteger(media.bytes)
        || !SHA256_PATTERN.test(media.sha256)
        || (media.etag !== null && !isBoundedString(media.etag, 1024))) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} slideshow media is invalid`);
    }
  }
}

function validateArtifactSource(source, index) {
  if (!isPlainObject(source)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].source must be an object`);
  }
  // A slideshow library is not a source-video master/cut window. Recognize
  // the Lab's existing receipt before the generic sourceLibraryId dispatch.
  if (source.schema === 'content-lab.syzygy-slideshow-source.v1' || source.kind === 'syzygy_slideshow') {
    validateSlideshowSource(source, index);
    return;
  }
  if (Object.hasOwn(source, 'master') || Object.hasOwn(source, 'cutWindow') || Object.hasOwn(source, 'sourceLibraryId')) {
    validateArtifactSourceDna(source, index);
    return;
  }
  if (source.schema === 'content-lab.page-source-import-source.v1' || source.kind === 'page_source_import') {
    validatePageSourceImport(source, index);
    return;
  }
  assertExactFields(source, ARTIFACT_APPROVED_LIBRARY_SOURCE_FIELDS, `artifacts response entry [${index}].source`);
  validateArtifactSourceAuthority(source, index);
}

function validateArtifactThumbnail(thumbnail, index) {
  if (!isPlainObject(thumbnail)
    || Object.keys(thumbnail).some((key) => !ARTIFACT_THUMBNAIL_ALLOWED_FIELDS.has(key))
    || Object.keys(thumbnail).length !== ARTIFACT_THUMBNAIL_ALLOWED_FIELDS.size) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].thumbnail must carry the complete thumbnail contract`);
  }
  if (!isHttpsUrl(thumbnail.url, MAX_ARTIFACT_URL_LENGTH)
    || !SHA256_PATTERN.test(thumbnail.sha256)
    || !isPositiveInteger(thumbnail.bytes)
    || thumbnail.type !== 'image/jpeg') {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].thumbnail is invalid`);
  }
}

function validateArtifactDelivery(delivery, index) {
  const label = `artifacts response entry [${index}].delivery`;
  assertExactFields(delivery, ARTIFACT_DELIVERY_FIELDS, label);
  if (delivery.aspectRatio !== '9:16'
      || !Number.isSafeInteger(delivery.width) || delivery.width <= 0
      || !Number.isSafeInteger(delivery.height) || delivery.height <= 0
      || Math.abs((delivery.width / delivery.height) - (9 / 16)) > 0.003) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must describe exact vertical delivery geometry`);
  }
  assertExactFields(delivery.crop, ARTIFACT_CROP_FIELDS, `${label}.crop`);
  const crop = delivery.crop;
  const expectedCounts = { dual: 2, triptych: 3, both: 5 };
  if (!Object.hasOwn(expectedCounts, crop.mode)
      || crop.count !== expectedCounts[crop.mode]
      || !Number.isSafeInteger(crop.index) || crop.index < 0 || crop.index >= crop.count
      || !SHA256_PATTERN.test(crop.sourceSha256)
      || crop.groupId !== `sha256:${crop.sourceSha256}`) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.crop must trace one exact output to its provider master`);
  }
}

function validateArtifactEntry(entry, index) {
  if (!isPlainObject(entry)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}] must be an object`);
  }
  const unknown = Object.keys(entry).filter((key) => !ARTIFACT_ENTRY_ALLOWED_FIELDS.has(key));
  if (unknown.length > 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}] has unknown fields: ${unknown.join(', ')}`);
  }
  if (!isNonEmptyString(entry.url)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].url must be a non-empty string`);
  if (!isHttpsUrl(entry.url, MAX_ARTIFACT_URL_LENGTH)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].url must be a well-formed https URL`);
  }
  if (!isNonEmptyString(entry.type)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].type must be a non-empty string`);
  const hasSha = Object.hasOwn(entry, 'sha256');
  const hasBytes = Object.hasOwn(entry, 'bytes');
  if (hasSha !== hasBytes) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}] must carry sha256 and bytes together`);
  }
  if (hasSha && !SHA256_PATTERN.test(entry.sha256)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].sha256 must be lowercase SHA-256 hex`);
  }
  if (hasBytes && !isPositiveInteger(entry.bytes)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].bytes must be a positive integer`);
  }
  if (Object.hasOwn(entry, 'source')) {
    if (!hasSha) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].source requires output sha256 and bytes`);
    }
    validateArtifactSource(entry.source, index);
  }
  if (Object.hasOwn(entry, 'thumbnail')) validateArtifactThumbnail(entry.thumbnail, index);
  if (Object.hasOwn(entry, 'delivery')) validateArtifactDelivery(entry.delivery, index);
  if (Object.hasOwn(entry, 'lyricTiming')) {
    try { validateLyricCapsuleShape(entry.lyricTiming); }
    catch { throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response entry [${index}].lyricTiming is invalid`); }
  }
  if (Object.hasOwn(entry, 'sourceTreatment')) {
    if (!hasSha) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'source treatment requires output SHA and length');
    try { validateSourceTreatment(entry.sourceTreatment, { sourceSha256: entry.sha256 }); }
    catch { throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'source treatment receipt is invalid'); }
  }
}

// Recovery must not discard valid paid siblings because another receipt is
// malformed. Keep their positions for the runner's durable rejection ledger;
// never return the rejected entry's URL or provenance as admissible output.
function validateStalePaidArtifacts(payload) {
  if (payload.artifacts.length > MAX_ARTIFACT_ENTRIES) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'artifacts response exceeds the entry cap');
  }
  const rejected = new Set();
  const groups = new Map();
  payload.artifacts.forEach((entry, index) => {
    try { validateArtifactEntry(entry, index); }
    catch (error) {
      if (error?.code !== 'CONTENT_LAB_SCHEMA_MISMATCH') throw error;
      rejected.add(index);
      return;
    }
    if (entry.delivery) {
      const key = entry.delivery.crop.groupId;
      const group = groups.get(key) ?? [];
      group.push(index);
      groups.set(key, group);
    }
  });
  for (const indices of groups.values()) {
    try { validateCropDeliveryGroups(indices.map(index => payload.artifacts[index])); }
    catch (error) {
      if (error?.code !== 'ARTIFACT_CROP_CONTRACT_INVALID') throw error;
      for (const index of indices) rejected.add(index);
    }
  }
  payload.artifacts = payload.artifacts.map((entry, index) => rejected.has(index)
    ? { type: 'rejected', sha256: SHA256_PATTERN.test(entry?.sha256) ? entry.sha256 : '',
      recoveryRejection: 'CONTENT_LAB_SCHEMA_MISMATCH' }
    : entry);
}

const SHA256_VERSION_PATTERN = /^sha256:[0-9a-f]{64}$/;
const SAFE_CONTRACT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const FORMAT_CONTRACT_FIELDS = new Set([
  'formatSlug', 'contentNiche', 'contentEngine', 'materialSource', 'assetType',
  'definitionStatus', 'executionStatus', 'creativeAuthority', 'dimensions',
  'output', 'reviewAuthority', 'reviewGates', 'distribution', 'definitionGaps',
  'formatContractVersion', 'executor',
]);
const FORMAT_DIMENSIONS = Object.freeze([
  'subject', 'setting', 'shotGrammar', 'lighting', 'motion', 'duration',
  'textPolicy', 'negativeRules', 'referenceExamples',
]);
const FORMAT_DIMENSION_FIELDS = new Set(['status', 'rule', 'authority']);
const FORMAT_OUTPUT_FIELDS = new Set(['aspectRatio', 'container', 'durationPolicy', 'audioPolicy']);
const FORMAT_DISTRIBUTION_FIELDS = new Set(['captions', 'sounds']);
const FORMAT_AUTHORITY_FIELDS = new Set(['kind', 'id', 'version']);
const FORMAT_EXECUTOR_FIELDS = new Set(['kind', 'id', 'version', 'maxQuantity']);

function assertExactFields(value, allowed, label) {
  if (!isPlainObject(value)) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be an object`);
  const fields = Object.keys(value);
  const unknown = fields.filter((field) => !allowed.has(field));
  if (unknown.length > 0 || fields.length !== allowed.size) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} fields are invalid`);
  }
}

function validateFormatAuthority(authority, label) {
  if (authority === null) return;
  assertExactFields(authority, FORMAT_AUTHORITY_FIELDS, label);
  if (!isBoundedString(authority.kind, 100)
      || !SAFE_CONTRACT_ID_PATTERN.test(authority.id)
      || !SHA256_VERSION_PATTERN.test(authority.version)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} is invalid`);
  }
}

function validateFormatExecutor(executor, entry, label) {
  if (entry.executionStatus === 'uncommissioned') {
    if (executor !== null) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} must be null when uncommissioned`);
    return;
  }
  assertExactFields(executor, FORMAT_EXECUTOR_FIELDS, label);
  if (!isBoundedString(executor.kind, 100)
      || !SAFE_CONTRACT_ID_PATTERN.test(executor.id)
      || !SHA256_VERSION_PATTERN.test(executor.version)
      || !Number.isInteger(executor.maxQuantity)
      || executor.maxQuantity < 1
      || executor.maxQuantity > 20) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} is invalid`);
  }
  if (entry.definitionStatus !== 'complete' || entry.creativeAuthority === null
      || executor.kind !== entry.creativeAuthority.kind
      || executor.id !== entry.creativeAuthority.id
      || executor.version !== entry.creativeAuthority.version) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} diverges from the creative authority`);
  }
}

function validateFormatContractEntry(entry, index) {
  const label = `format-contracts response entry [${index}]`;
  assertExactFields(entry, FORMAT_CONTRACT_FIELDS, label);
  if (!FORMAT_SLUG_PATTERN.test(entry.formatSlug)
      || !isBoundedString(entry.contentNiche, 240)
      || !isBoundedString(entry.contentEngine, 100)
      || !isBoundedString(entry.materialSource, 100)
      || entry.assetType !== 'video/mp4'
      || !['complete', 'incomplete'].includes(entry.definitionStatus)
      || !['commissioned', 'uncommissioned'].includes(entry.executionStatus)
      || !SHA256_VERSION_PATTERN.test(entry.formatContractVersion)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label} identity is invalid`);
  }
  validateFormatAuthority(entry.creativeAuthority, `${label}.creativeAuthority`);
  assertExactFields(entry.dimensions, new Set(FORMAT_DIMENSIONS), `${label}.dimensions`);
  const undefinedDimensions = [];
  for (const name of FORMAT_DIMENSIONS) {
    const dimension = entry.dimensions[name];
    assertExactFields(dimension, FORMAT_DIMENSION_FIELDS, `${label}.dimensions.${name}`);
    if (!['defined', 'undefined'].includes(dimension.status)) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.dimensions.${name}.status is invalid`);
    }
    if (dimension.status === 'defined') {
      if (!isBoundedString(dimension.rule, 600) || !isBoundedString(dimension.authority, 240)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.dimensions.${name} has no bounded rule authority`);
      }
    } else if (dimension.rule !== null || dimension.authority !== null) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.dimensions.${name} exposes an undefined rule`);
    } else {
      undefinedDimensions.push(name);
    }
  }
  assertExactFields(entry.output, FORMAT_OUTPUT_FIELDS, `${label}.output`);
  if (entry.output.aspectRatio !== '9:16' || entry.output.container !== 'video/mp4'
      || !['executor_bound', 'source_bound'].includes(entry.output.durationPolicy)
      || entry.output.audioPolicy !== 'campaign_sound_bound_downstream') {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.output is invalid`);
  }
  if (entry.reviewAuthority !== null && !isBoundedString(entry.reviewAuthority, 240)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.reviewAuthority is invalid`);
  }
  if (!Array.isArray(entry.reviewGates) || entry.reviewGates.length === 0 || entry.reviewGates.length > 20
      || entry.reviewGates.some((gate) => !isBoundedString(gate, 100))
      || new Set(entry.reviewGates).size !== entry.reviewGates.length) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.reviewGates is invalid`);
  }
  assertExactFields(entry.distribution, FORMAT_DISTRIBUTION_FIELDS, `${label}.distribution`);
  if (entry.distribution.captions !== 'page_binding_required' || entry.distribution.sounds !== 'page_binding_required') {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.distribution is invalid`);
  }
  if (!Array.isArray(entry.definitionGaps)
      || entry.definitionGaps.some((gap) => !isBoundedString(gap, 100))
      || JSON.stringify(entry.definitionGaps) !== JSON.stringify([...new Set(entry.definitionGaps)].sort())) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.definitionGaps is invalid`);
  }
  const expectedGaps = [
    ...(entry.creativeAuthority === null ? ['creativeAuthority'] : []),
    ...(entry.reviewAuthority === null ? ['reviewAuthority'] : []),
    ...undefinedDimensions,
  ].sort();
  if (JSON.stringify(entry.definitionGaps) !== JSON.stringify(expectedGaps)
      || (entry.definitionStatus === 'complete') !== (entry.definitionGaps.length === 0)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `${label}.definitionGaps does not match its definition`);
  }
  validateFormatExecutor(entry.executor, entry, `${label}.executor`);
}

const RESPONSE_SCHEMAS = {
  formatContracts: {
    schema: FORMAT_CONTRACT_STATUS_SCHEMA_ID,
    required: {
      contractsRegistryVersion: 'nonEmptyString',
      engineRegistryVersion: 'nonEmptyString',
      formats: 'array',
    },
    optional: {},
    validateEntries: (payload) => {
      if (!SHA256_VERSION_PATTERN.test(payload.contractsRegistryVersion)
          || !SHA256_VERSION_PATTERN.test(payload.engineRegistryVersion)
          || payload.formats.length > MAX_FORMAT_CONTRACT_ENTRIES) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'format-contracts response registry versions or size are invalid');
      }
      payload.formats.forEach((entry, index) => validateFormatContractEntry(entry, index));
      const slugs = payload.formats.map((entry) => entry.formatSlug);
      if (new Set(slugs).size !== slugs.length || JSON.stringify(slugs) !== JSON.stringify([...slugs].sort())) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'format-contracts response entries must be unique and sorted');
      }
    },
  },
  dossierIngredients: {
    schema: DOSSIER_INGREDIENT_CATALOG_SCHEMA_ID,
    required: {
      pageId: 'nonEmptyString',
      masterPages: 'object',
      masterPagesHash: 'nonEmptyString',
      catalogVersion: 'nonEmptyString',
      currentFormatCandidates: 'array',
      formats: 'array',
    },
    optional: {},
    validateEntries: (payload) => {
      validateMasterPagesIntent(payload.masterPages, 'catalog.masterPages');
      if (!SHA256_VERSION_PATTERN.test(payload.masterPagesHash)
          || !SHA256_VERSION_PATTERN.test(payload.catalogVersion)
          || payload.formats.length > MAX_FORMAT_CONTRACT_ENTRIES
          || payload.currentFormatCandidates.length > MAX_FORMAT_CONTRACT_ENTRIES
          || payload.currentFormatCandidates.some((value) => !isBoundedString(value, 100))) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'dossier ingredient catalog identity or size is invalid');
      }
      payload.formats.forEach((entry, index) => validateDossierFormat(entry, index));
      const ids = payload.formats.map((entry) => entry.formatId);
      if (new Set(ids).size !== ids.length || JSON.stringify(ids) !== JSON.stringify([...ids].sort())) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'dossier ingredient formats must be unique and sorted');
      }
      if (payload.currentFormatCandidates.some((formatId) => !ids.includes(formatId))) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'current format candidates must exist in the catalog');
      }
    },
  },
  capabilities: {
    required: { capabilities: 'array' },
    optional: {},
    tolerant: { zeroPlanRecipes: 'zeroPlanRecipes' },
    validateEntries: (payload) => {
      if (payload.capabilities.length > MAX_CAPABILITY_ENTRIES) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `capabilities response has ${payload.capabilities.length} entries, exceeding the ${MAX_CAPABILITY_ENTRIES}-entry cap`);
      }
      payload.capabilities.forEach((entry, index) => validateCapabilityEntry(entry, index));
    },
  },
  create: {
    required: { jobId: 'nonEmptyString', status: 'nonEmptyString' },
    optional: {},
    validateEntries: (payload) => {
      if (!JOB_STATUS_VALUES.has(payload.status)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'create response status is not a recognized job status');
      }
    },
  },
  status: {
    required: { jobId: 'nonEmptyString', status: 'nonEmptyString' },
    optional: { progress: 'progress', error: 'terminalError' }, tolerant: { errorClass: 'failureClass', errorDetail: 'failureDetail' },
    validateEntries: (payload) => {
      if (!JOB_STATUS_VALUES.has(payload.status)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'status response status is not a recognized job status');
      }
      if (payload.error !== undefined && !['failed', 'error', 'cancelled'].includes(payload.status)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'status response error is allowed only for a terminal failure');
      }
    },
  },
  artifacts: {
    required: { jobId: 'nonEmptyString', artifacts: 'array' },
    optional: {},
    validateEntries: (payload) => {
      if (payload.artifacts.length > MAX_ARTIFACT_ENTRIES) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `artifacts response has ${payload.artifacts.length} entries, exceeding the ${MAX_ARTIFACT_ENTRIES}-entry cap`);
      }
      payload.artifacts.forEach((entry, index) => validateArtifactEntry(entry, index));
      try { validateCropDeliveryGroups(payload.artifacts); } catch {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'artifacts must contain complete unique crop groups');
      }
    },
  },
  stalePaidArtifacts: {
    required: { jobId: 'nonEmptyString', artifacts: 'array' },
    optional: {},
    validateEntries: validateStalePaidArtifacts,
  },
  roster: {
    required: {
      snapshotVersion: 'nonEmptyString', projectionHash: 'nonEmptyString', pages: 'array',
    },
    optional: { capturedAt: 'nonEmptyString' },
    validateEntries: (payload) => {
      if (!/^sha256:[0-9a-f]{64}$/.test(payload.projectionHash)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'roster response projectionHash must be full sha256 hex');
      }
      payload.pages.forEach((entry, index) => validateRosterPageEntry(entry, index));
    },
  },
  rosterRefresh: {
    required: {
      added: 'nonNegativeInteger',
      updated: 'nonNegativeInteger',
      totalInNotion: 'nonNegativeInteger',
      projectedCount: 'nonNegativeInteger',
      snapshotVersion: 'nonEmptyString',
      projectionHash: 'nonEmptyString',
      complete: 'boolean',
      errorCount: 'nonNegativeInteger',
    },
    optional: {},
    validateEntries: (payload) => {
      if (!/^sha256:[0-9a-f]{64}$/.test(payload.projectionHash)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'roster refresh projectionHash must be full sha256 hex');
      }
    },
  },
  visualAdmission: {
    schema: 'content-lab.visual-admission.v1',
    required: { verdict: 'nonEmptyString', jobId: 'nonEmptyString', outputIndex: 'nonNegativeInteger',
      pageId: 'nonEmptyString', sha256: 'nonEmptyString', bytes: 'positiveInteger',
      scannedAt: 'nonEmptyString', sampling: 'object', model: 'object', ocr: 'object', reason: 'nonEmptyString' },
    optional: {},
  },
  recipe: {
    required: {
      recipeId: 'nonEmptyString',
      engine: 'nonEmptyString',
      recipeVersion: 'nonEmptyString',
      dossierRevision: 'nonEmptyString',
      recipeSpecHash: 'nonEmptyString',
      status: 'nonEmptyString',
    },
    optional: {},
    validateEntries: (payload) => {
      if (payload.status !== 'registered') {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'recipe publication status must be registered');
      }
      if (!/^sha256:[0-9a-f]{64}$/.test(payload.recipeSpecHash)) {
        throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'recipe publication hash must be sha256 hex');
      }
    },
  },
};

function validateResponseEnvelope(kind, payload) {
  const def = RESPONSE_SCHEMAS[kind];
  if (!def) throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `unknown Content Lab response kind: ${kind}`);
  if (!isPlainObject(payload)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `Content Lab ${kind} response must be a JSON object`);
  }
  if (payload.schema !== (def.schema ?? RESPONSE_SCHEMA_ID)) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', 'Content Lab response schema mismatch');
  }
  const allowedKeys = new Set(['schema', ...Object.keys(def.required), ...Object.keys(def.optional), ...Object.keys(def.tolerant ?? {})]);
  const unknownKeys = Object.keys(payload).filter((key) => !allowedKeys.has(key));
  if (unknownKeys.length > 0) {
    throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `Content Lab ${kind} response has unknown fields: ${unknownKeys.join(', ')}`);
  }
  // Status responses only: the Lab answers a terminal status (failed/error/
  // cancelled) with `error: ""` to mean "no provider error text". That is not a
  // bounded terminal error (isBoundedString requires length > 0), so treat it as
  // ABSENT and let the job take the normal terminal-failure path instead of
  // failing CONTENT_LAB_SCHEMA_MISMATCH and retrying every 6 h forever. A
  // non-empty error keeps today's rule; whitespace-only and over-length strings
  // still fail, and an empty error on a non-terminal status still fails (the
  // terminal-status check below excludes it).
  if (kind === 'status' && payload.error === '' && ['failed', 'error', 'cancelled'].includes(payload.status)) delete payload.error;
  for (const [field, validatorName] of Object.entries(def.required)) {
    if (!FIELD_VALIDATORS[validatorName](payload[field])) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `Content Lab ${kind} response missing/invalid required field "${field}"`);
    }
  }
  for (const [field, validatorName] of Object.entries(def.optional)) {
    if (payload[field] !== undefined && !FIELD_VALIDATORS[validatorName](payload[field])) {
      throw closed('CONTENT_LAB_SCHEMA_MISMATCH', `Content Lab ${kind} response has invalid optional field "${field}"`);
    }
  }
  if (typeof def.validateEntries === 'function') def.validateEntries(payload);
  return dropMalformedTolerantFields(kind, def, payload);
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Fail-closed timeoutMs validation, performed once at construction — never
// silently coerced. Omitting timeoutMs entirely preserves the documented
// default; any explicitly supplied value must be a finite number strictly
// within [MIN_TIMEOUT_MS, MAX_TIMEOUT_MS]. In particular this rejects 0,
// negative numbers, NaN, and +/-Infinity instead of the old `Math.max(1, ...
// || DEFAULT)` coercion, which silently turned every invalid input (0,
// negative, NaN, a string) into either 1ms or the default — masking caller
// bugs instead of failing closed.
function resolveTimeoutMs(timeoutMs) {
  if (timeoutMs === undefined) return DEFAULT_TIMEOUT_MS;
  if (!isBoundedNumber(MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)(timeoutMs)) {
    throw closed(
      'CONTENT_LAB_TIMEOUT_INVALID',
      `timeoutMs must be a finite number between ${MIN_TIMEOUT_MS} and ${MAX_TIMEOUT_MS} ms (or omitted for the ${DEFAULT_TIMEOUT_MS}ms default)`,
    );
  }
  return timeoutMs;
}

// S12 (defect) — retries/baseDelayMs/maxBackoffMs/maxRetryAfterMs were each
// coerced with `Number(x) || fallback` (or `Math.max`/`Math.min` over that),
// which silently accepts and normalizes every non-finite, wrong-type, or
// out-of-range input rather than rejecting it:
//   - `Number(x) || fallback` treats NaN AND 0 identically (both falsy) —
//     an explicit, legal `baseDelayMs: 0` (no backoff) silently became
//     whatever `NaN` also becomes: fallback. Zero must be preserved when the
//     field's contract allows it, not silently overwritten.
//   - `Number('not-a-number')` is `NaN`, so a wrong-type value never throws;
//     it silently falls back to a default instead of surfacing the caller's
//     mistake.
//   - `Number(Infinity)` is `Infinity`, which is truthy — `Number(x) || 0`
//     does NOT catch it, so `retries: Infinity` before the `Math.min(MAX_
//     RETRIES, ...)` clamp only "worked" because retries specifically had an
//     upper clamp; baseDelayMs/maxRetryAfterMs had NO upper bound at all,
//     so `baseDelayMs: Infinity` (or any absurdly large finite number) was
//     accepted verbatim and would make every retry's backoff/sleep call
//     effectively hang forever.
// Every one of these must fail closed (throw) instead of coercing.
const MAX_BASE_DELAY_MS = 60_000; // 1 minute — generous ceiling for a single backoff step
const MIN_BASE_DELAY_MS = 0;
const MAX_MAX_BACKOFF_MS = 300_000; // 5 minutes — generous ceiling for the backoff cap itself
const MIN_MAX_BACKOFF_MS = 0;
const MAX_MAX_RETRY_AFTER_MS = 300_000; // 5 minutes — same ceiling as the backoff cap
const MIN_MAX_RETRY_AFTER_MS = 0;

// Strictly type-checked: `value` must be an actual JS `number` primitive
// (via isBoundedNumber's `typeof value === 'number'` guard), never coerced
// from another type. A prior version of this helper used `Number(value)`,
// which silently coerces every wrong type into a plausible-looking number —
// `Number(null) === 0`, `Number('') === 0`, `Number([]) === 0`,
// `Number(false) === 0`, `Number('2') === 2` — so `null`, `''`, `[]`,
// `false`, and numeric strings all slipped through bounds checks as if they
// were legitimate `0` (or parsed numeric) input instead of failing closed as
// the wrong-type values they actually are.
function resolveBoundedNonNegative(value, { name, code, min, max, integer = false, fallback }) {
  if (value === undefined) return fallback;
  if (!isBoundedNumber(min, max)(value) || (integer && !Number.isInteger(value))) {
    throw closed(
      code,
      `${name} must be a finite ${integer ? 'integer' : 'number'} between ${min} and ${max}${integer ? '' : ' ms'} (or omitted for the ${fallback}${integer ? '' : 'ms'} default) (got: ${JSON.stringify(value)})`,
    );
  }
  return value;
}

function resolveRetries(retries) {
  return resolveBoundedNonNegative(retries, {
    name: 'retries', code: 'CONTENT_LAB_RETRIES_INVALID', min: MIN_RETRIES, max: MAX_RETRIES, integer: true, fallback: DEFAULT_RETRIES,
  });
}

function resolveBaseDelayMs(baseDelayMs) {
  return resolveBoundedNonNegative(baseDelayMs, {
    name: 'baseDelayMs', code: 'CONTENT_LAB_BASE_DELAY_INVALID', min: MIN_BASE_DELAY_MS, max: MAX_BASE_DELAY_MS, fallback: DEFAULT_BASE_DELAY_MS,
  });
}

// maxBackoffMs additionally must never be below the (already-resolved)
// baseDelayMs — a cap tighter than the floor it's meant to cap would make
// the exponential-backoff calculation (`Math.min(maxBackoffMs, baseDelayMs *
// 2 ** attempt)`) silently clamp every retry down to a delay shorter than
// what the caller explicitly configured as the minimum step.
//
// When maxBackoffMs is omitted, the default must still respect a
// caller-supplied baseDelayMs larger than the library's own default backoff
// cap (2000ms) — e.g. `baseDelayMs: 60_000` with no maxBackoffMs must not
// silently shrink the cap below the floor the caller just set. This mirrors
// the pre-S12 `Math.max(baseDelayMs, ...)` widening for the omitted case
// only; an EXPLICIT maxBackoffMs below baseDelayMs is a real inconsistency
// and must still fail closed rather than being silently widened.
function resolveMaxBackoffMs(maxBackoffMs, resolvedBaseDelayMs) {
  if (maxBackoffMs === undefined) return Math.max(DEFAULT_MAX_BACKOFF_MS, resolvedBaseDelayMs);
  const resolved = resolveBoundedNonNegative(maxBackoffMs, {
    name: 'maxBackoffMs', code: 'CONTENT_LAB_MAX_BACKOFF_INVALID', min: MIN_MAX_BACKOFF_MS, max: MAX_MAX_BACKOFF_MS, fallback: DEFAULT_MAX_BACKOFF_MS,
  });
  if (resolved < resolvedBaseDelayMs) {
    throw closed('CONTENT_LAB_MAX_BACKOFF_INVALID', `maxBackoffMs (${resolved}) must be >= baseDelayMs (${resolvedBaseDelayMs})`);
  }
  return resolved;
}

// Preserves an explicit 0 as a zero-wait Retry-After cap instead of treating it
// as falsy and silently reinstating the default cap.
function resolveMaxRetryAfterMs(maxRetryAfterMs) {
  return resolveBoundedNonNegative(maxRetryAfterMs, {
    name: 'maxRetryAfterMs', code: 'CONTENT_LAB_MAX_RETRY_AFTER_INVALID', min: MIN_MAX_RETRY_AFTER_MS, max: MAX_MAX_RETRY_AFTER_MS, fallback: DEFAULT_MAX_RETRY_AFTER_MS,
  });
}

// Every caller-supplied value that flows verbatim into a request header
// (requestId, pageId, idempotencyKey) must be validated as a bounded, safe
// token BEFORE it ever reaches header construction/fetch — a raw string here
// could otherwise inject CRLF/extra headers or force unbounded memory via an
// oversized value. Validation happens synchronously, outside any retry loop,
// so a bad token fails closed with zero network calls and no retries.
function validateHeaderToken(value, { required, code, label, maxLength }) {
  if (value === undefined || value === null) {
    if (required) throw closed(code, `${label} is required`);
    return undefined;
  }
  if (
    typeof value !== 'string' ||
    value.length === 0 ||
    value.length > maxLength ||
    !HEADER_TOKEN_PATTERN.test(value)
  ) {
    throw closed(code, `${label} must be a bounded, safe token`);
  }
  return value;
}

function validateRequestId(requestId) {
  return validateHeaderToken(requestId, {
    required: false,
    code: 'CONTENT_LAB_REQUEST_ID_INVALID',
    label: 'requestId',
    maxLength: MAX_REQUEST_ID_LENGTH,
  });
}

function validatePageId(pageId) {
  return validateHeaderToken(pageId, {
    required: true,
    code: 'CONTENT_LAB_PAGE_ID_INVALID',
    label: 'pageId',
    maxLength: MAX_PAGE_ID_LENGTH,
  });
}

function validateIdempotencyKey(idempotencyKey) {
  return validateHeaderToken(idempotencyKey, {
    required: false,
    code: 'CONTENT_LAB_IDEMPOTENCY_KEY_INVALID',
    label: 'idempotencyKey',
    maxLength: MAX_IDEMPOTENCY_KEY_LENGTH,
  });
}

function abortError() {
  const error = new Error('The operation was aborted');
  error.name = 'AbortError';
  return error;
}

function cancelBodyReader(reader) {
  // Cancellation is best-effort cleanup. Its promise can itself remain pending;
  // waiting for it would defeat the request timeout and strand the retry.
  try {
    if (typeof reader.cancel === 'function') Promise.resolve(reader.cancel()).catch(() => {});
  } catch { /* preserve the request's original outcome */ }
}

// Reads the response body with a hard byte cap enforced DURING the read — never
// buffering an unbounded/oversized body into memory before checking its size.
// When a real streamable body is available (`response.body.getReader()`), bytes
// are accumulated chunk-by-chunk and the moment the running total exceeds
// `maxBytes` the reader is cancelled immediately and the remaining chunks are
// never requested/consumed. A bounded text() fallback (checked after the fact,
// as before) is retained only for mocks/environments that don't expose a
// streamable body. Both paths race against the shared AbortController so a
// hanging/slow body — streamed or not — stays bound by the request timeout
// instead of only the time-to-first-byte. UTF-8 decoding uses a single
// streaming TextDecoder across chunks so multi-byte characters split across
// chunk boundaries decode correctly.
async function readBoundedBody(response, controller, maxBytes) {
  const abortPromise = new Promise((_, reject) => {
    if (controller.signal.aborted) {
      reject(abortError());
      return;
    }
    controller.signal.addEventListener('abort', () => reject(abortError()), { once: true });
  });

  const reader = typeof response?.body?.getReader === 'function' ? response.body.getReader() : null;
  if (reader) {
    try {
      return await Promise.race([readStreamedBody(reader, maxBytes), abortPromise]);
    } catch (error) {
      // Stop further reads without making the timeout depend on cleanup.
      cancelBodyReader(reader);
      throw error;
    }
  }

  // Fallback: no streamable body (mocks, or a fetch impl that doesn't expose one).
  // Never trusts Content-Length alone — the byte cap is enforced on the actual
  // decoded text length.
  const text = await Promise.race([response.text(), abortPromise]);
  const byteLength = new TextEncoder().encode(text).length;
  if (byteLength > maxBytes) {
    throw closed('CONTENT_LAB_RESPONSE_TOO_LARGE', `Content Lab response body of ${byteLength} bytes exceeds the ${maxBytes}-byte cap`);
  }
  return text;
}

async function readStreamedBody(reader, maxBytes) {
  const decoder = new TextDecoder('utf-8', { fatal: false });
  let text = '';
  let receivedBytes = 0;
  for (;;) {
    const result = await reader.read();
    if (!result || typeof result !== 'object' || typeof result.done !== 'boolean') {
      throw closed('CONTENT_LAB_STREAM_CHUNK_INVALID', 'Content Lab response stream returned an invalid read result');
    }
    const { done, value } = result;
    if (done) break;
    // The Streams API contract for a byte stream's reader.read() guarantees
    // each chunk is a Uint8Array. A chunk of any other shape (a string, a
    // plain array, an ArrayBuffer, ...) means the "response body" isn't a
    // real conformant byte stream — decoder.decode() would throw a bare
    // TypeError for it, which (having no .code) would otherwise fall through
    // the generic error classifier below as a retryable CONTENT_LAB_UNAVAILABLE
    // error. That's wrong: a non-conformant stream is a deterministic
    // contract violation, not a transient network blip, so it must fail
    // closed immediately and never be retried.
    if (!(value instanceof Uint8Array)) {
      throw closed('CONTENT_LAB_STREAM_CHUNK_INVALID', 'Content Lab response stream produced a non-Uint8Array chunk');
    }
    if (value.byteLength === 0) continue;
    receivedBytes += value.byteLength;
    if (receivedBytes > maxBytes) {
      // The caller cancels; never request/consume the remaining chunks.
      throw closed('CONTENT_LAB_RESPONSE_TOO_LARGE', `Content Lab response body exceeded the ${maxBytes}-byte cap mid-stream`);
    }
    text += decoder.decode(value, { stream: true });
  }
  text += decoder.decode();
  return text;
}

export class ContentLabClient {
  constructor({
    baseUrl,
    expectedOrigin,
    token,
    apiKey,
    fetchImpl = globalThis.fetch,
    // timeoutMs/retries/baseDelayMs/maxBackoffMs/maxRetryAfterMs deliberately
    // have NO destructuring default here — each resolver function below owns
    // its own "omitted -> documented default" logic via an explicit
    // `=== undefined` check. Giving these params a destructuring default
    // would make "omitted" and "explicitly passed the default value"
    // indistinguishable by the time the resolver runs, which silently broke
    // resolveMaxBackoffMs's baseDelayMs-consistency default-widening: e.g.
    // `{ baseDelayMs: 60_000 }` with maxBackoffMs omitted must still widen
    // the default backoff cap to 60_000 — but with a destructuring default
    // of `DEFAULT_MAX_BACKOFF_MS`, resolveMaxBackoffMs would receive `2000`
    // as if it had been explicitly supplied, sees `2000 < 60000`, and fails
    // closed on a value the caller never actually passed.
    timeoutMs,
    retries,
    lane = 'content-bucket-control-plane',
    sleepImpl = defaultSleep,
    randomImpl = Math.random,
    baseDelayMs,
    maxBackoffMs,
    maxRetryAfterMs,
  } = {}) {
    if (!expectedOrigin) throw closed('CONTENT_LAB_EXPECTED_ORIGIN_MISSING', 'expected Content Lab origin is required');
    let expected;
    try { expected = new URL(expectedOrigin); } catch {
      throw closed('CONTENT_LAB_EXPECTED_ORIGIN_INVALID', 'expected Content Lab origin must be a bare HTTPS origin');
    }
    if (expected.protocol !== 'https:' || expected.pathname !== '/' || expected.username || expected.password) {
      throw closed('CONTENT_LAB_EXPECTED_ORIGIN_INVALID', 'expected Content Lab origin must be a bare HTTPS origin');
    }
    let url;
    try { url = new URL(baseUrl || expected.origin); } catch {
      throw closed('CONTENT_LAB_BASE_HOST_MISMATCH', 'Content Lab base URL must match the explicitly supplied HTTPS origin');
    }
    if (url.protocol !== 'https:' || url.origin !== expected.origin || url.pathname.replace(/\/$/, '') !== '' || url.username || url.password) {
      throw closed('CONTENT_LAB_BASE_HOST_MISMATCH', 'Content Lab base URL must match the explicitly supplied HTTPS origin');
    }
    // `token` and `apiKey` flow VERBATIM into `Authorization` and `X-Api-Key`
    // in #headers below, which is exactly what the bounded-safe-token note at
    // the top of this file exists for — and that note enumerated only the three
    // caller-supplied header values, so these two were never checked. The
    // sibling adapter built from these same two fields (`get postRenders`, ->
    // `adapters/postRenderClient.js` `POST_RENDER_AUTH_MISSING`) refuses both by name.
    //
    // Left unchecked, a CONTROL_PLANE env value with a stray CR/LF (or a
    // non-string CONTENT_LAB_API_KEY from a mistyped binding) made
    // `new Headers(...)` throw a bare TypeError INSIDE `this.fetch(...)`; the
    // catch at the bottom of #request sees no `CONTENT_LAB_` code and classifies
    // it `CONTENT_LAB_UNAVAILABLE`, which is RETRYABLE — so every Cron burned
    // `retries + 1` attempts and their backoff on a permanent misconfiguration
    // that can never succeed, while the post-render lane on the same credential
    // failed closed immediately by name.
    if (!token || typeof token !== 'string' || !token.trim() || /[\r\n]/.test(token)) {
      throw closed('CONTENT_LAB_AUTH_MISSING', 'bearer token is required');
    }
    if (apiKey !== undefined && (typeof apiKey !== 'string' || !apiKey.trim() || /[\r\n]/.test(apiKey))) {
      throw closed('CONTENT_LAB_AUTH_MISSING', 'api key must be a header-safe string');
    }
    if (typeof fetchImpl !== 'function') throw closed('CONTENT_LAB_FETCH_MISSING', 'fetch implementation is required');
    if (typeof sleepImpl !== 'function') throw closed('CONTENT_LAB_SLEEP_INVALID', 'sleep implementation must be a function');
    if (typeof randomImpl !== 'function') throw closed('CONTENT_LAB_RANDOM_INVALID', 'random implementation must be a function');
    this.baseUrl = url.origin;
    this.expectedOrigin = expected.origin;
    this.token = token;
    this.apiKey = apiKey;
    // Cloudflare's host-provided fetch is receiver-sensitive. Storing it and
    // later calling `this.fetch(...)` changes its receiver to the client
    // instance and throws `Illegal invocation` before a request leaves the
    // Worker. The lexical wrapper keeps the injected function a plain call;
    // Node's fetch happens to tolerate the old form, so this must stay pinned
    // by an explicit receiver-sensitive regression test.
    this.fetch = (...args) => fetchImpl(...args);
    this.timeoutMs = resolveTimeoutMs(timeoutMs);
    this.callerBudgetMs = timeoutMs === undefined ? MAX_TIMEOUT_MS : this.timeoutMs;
    this.createTimeoutMs = callTimeoutMs('createJob', this.callerBudgetMs);
    this.retries = resolveRetries(retries);
    this.lane = lane;
    this.sleep = sleepImpl;
    this.random = randomImpl;
    this.baseDelayMs = resolveBaseDelayMs(baseDelayMs);
    this.maxBackoffMs = resolveMaxBackoffMs(maxBackoffMs, this.baseDelayMs);
    this.maxRetryAfterMs = resolveMaxRetryAfterMs(maxRetryAfterMs);
  }

  get postRenders() {
    return new PostRenderClient({ baseUrl: this.baseUrl, expectedOrigin: this.expectedOrigin,
      token: this.token, apiKey: this.apiKey, fetchImpl: this.fetch,
      timeoutMs: callTimeoutMs('postRenderStatus', this.callerBudgetMs),
      artifactTimeoutMs: callTimeoutMs('postRenderArtifact', this.callerBudgetMs) });
  }

  // `retries` narrows the client's configured retries for one call (a GET
  // render passes 0 and fails soft instead); omitted keeps the configured count.
  getCapabilities({ pageId, requestId, retries } = {}) {
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    return this.#request('/api/control-plane/v1/capabilities', {
      pageId, requestId, idempotent: true, kind: 'capabilities', ...this.#retriesFor(retries),
      timeoutMs: callTimeoutMs('capabilities', this.callerBudgetMs),
    });
  }

  getFormatContracts({ requestId, retries } = {}) {
    return this.#request('/api/control-plane/v1/format-contracts', {
      requestId, idempotent: true, kind: 'formatContracts', ...this.#retriesFor(retries),
      timeoutMs: callTimeoutMs('formatContracts', this.callerBudgetMs),
    });
  }

  // A per-call retry count may only lower the configured one, never raise it.
  #retriesFor(retries) {
    return Number.isInteger(retries) && retries >= 0 && retries < this.retries ? { retries } : {};
  }

  async getDossierIngredients({ pageId, masterPages, masterPagesHash, requestId } = {}) {
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    validateMasterPagesIntent(masterPages, 'dossierIngredients.masterPages');
    if (masterPages.pageId !== pageId || !/^sha256:[0-9a-f]{64}$/.test(masterPagesHash || '')) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'dossier ingredient Master Pages identity/hash does not match pageId');
    }
    const payload = await this.#request('/api/control-plane/v1/dossier-ingredients', {
      method: 'POST',
      body: {
        schema: 'content-lab.dossier-ingredient-request.v1',
        masterPages,
        masterPagesHash,
      },
      pageId,
      requestId,
      idempotent: true,
      kind: 'dossierIngredients', timeoutMs: callTimeoutMs('dossierIngredients', this.callerBudgetMs), retries: 0,
    });
    if (payload.pageId !== pageId
        || payload.masterPages.pageId !== pageId
        || payload.masterPagesHash !== masterPagesHash
        || stableStringify(payload.masterPages) !== stableStringify(masterPages)) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'dossier ingredient response does not match requested Master Pages authority');
    }
    return payload;
  }

  // The versioned Notion-informed fleet snapshot — the ONLY door Master
  // Pages ontology walks through. Fleet-wide by nature, so it is scoped by
  // the adapter's lane header rather than a page id (the Lab requires
  // X-RT-Lane on this route, which this client always sends).
  getRosterSnapshot({ requestId, retries } = {}) {
    return this.#request('/api/control-plane/v1/roster', {
      requestId, idempotent: true, kind: 'roster', ...this.#retriesFor(retries),
      timeoutMs: callTimeoutMs('roster', this.callerBudgetMs),
    });
  }

  refreshRoster({ requestId } = {}) {
    return this.#request('/api/control-plane/v1/roster/refresh', {
      method: 'POST', body: {}, requestId, idempotent: true, kind: 'rosterRefresh',
      timeoutMs: callTimeoutMs('rosterRefresh', this.callerBudgetMs),
    });
  }

  importSource(sourceImport, { requestId, idempotencyKey } = {}) {
    if (!isPlainObject(sourceImport)) {
      throw closed('CONTENT_LAB_SOURCE_IMPORT_INVALID', 'source import object is required');
    }
    assertExactFields(sourceImport, SOURCE_IMPORT_FIELDS, 'source import');
    if (sourceImport.schema !== 'content-lab.source-import-request.v1'
        || !isBoundedString(sourceImport.pageId, MAX_PAGE_ID_LENGTH)
        || !isBoundedString(sourceImport.format, 100)
        || !isHttpsUrl(sourceImport.sourceUrl, MAX_ARTIFACT_URL_LENGTH)
        || !isPlainObject(sourceImport.masterPages)
        || !SHA256_VERSION_PATTERN.test(sourceImport.masterPagesHash || '')) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'source import contract is invalid');
    }
    validateMasterPagesIntent(sourceImport.masterPages, 'sourceImport.masterPages');
    if (sourceImport.masterPages.pageId !== sourceImport.pageId
        || sourceImport.masterPages.contentEngine !== 'sourced_video') {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'source import must match one sourced-video Master Pages page');
    }
    if (!idempotencyKey) {
      throw closed('CONTENT_LAB_IDEMPOTENCY_REQUIRED', 'source import idempotency key is required');
    }
    return this.#request('/api/control-plane/v1/source-imports', {
      method: 'POST',
      body: sourceImport,
      pageId: sourceImport.pageId,
      requestId,
      idempotencyKey,
      idempotent: true,
      timeoutMs: callTimeoutMs('sourceImport', this.callerBudgetMs),
      kind: 'create',
    });
  }

  createJob(job, { requestId, idempotencyKey } = {}) {
    try {
      assertNoFreeFormPrompt(job);
      if (!job || typeof job !== 'object') throw closed('CONTENT_LAB_JOB_INVALID', 'job object is required');
      const requiredFields = ['pageId', 'lane', 'engine', 'lockedRecipeId', 'recipeVersion', 'quantity', 'constraints', 'sourceIsolation', 'policyHash', 'masterPages', 'masterPagesHash'];
      if (requiredFields.some((field) => job[field] === undefined || job[field] === null || job[field] === '')) {
        throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `job requires ${requiredFields.join(', ')}`);
      }
      const unknownFields = Object.keys(job).filter((field) => !JOB_FIELDS.has(field));
      if (unknownFields.length > 0) throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `unknown job fields: ${unknownFields.join(', ')}`);
      if (job.lane !== this.lane) throw closed('CONTENT_LAB_LANE_MISMATCH', 'job lane must match the dedicated adapter lane');
      if (!Number.isInteger(job.quantity) || job.quantity <= 0) {
        throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'job quantity must be a positive integer');
      }
      validateMasterPagesIntent(job.masterPages, 'job.masterPages');
      if (job.masterPages.pageId !== job.pageId || !/^sha256:[0-9a-f]{64}$/.test(job.masterPagesHash)) {
        throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'job Master Pages identity/hash does not match pageId');
      }
      if (!idempotencyKey) throw closed('CONTENT_LAB_IDEMPOTENCY_REQUIRED', 'job idempotency key is required');
    } catch (error) {
      error.createAckUnknown = false; // Local validation has not sent a POST.
      throw error;
    }
    return this.#request('/api/control-plane/v1/jobs', {
      method: 'POST',
      timeoutMs: this.createTimeoutMs,
      body: job,
      pageId: job.pageId,
      requestId,
      idempotencyKey,
      idempotent: true,
      kind: 'create',
    });
  }

  publishRecipe(publication, { requestId, idempotencyKey } = {}) {
    if (!isPlainObject(publication)) {
      throw closed('CONTENT_LAB_RECIPE_INVALID', 'recipe publication object is required');
    }
    const unknownFields = Object.keys(publication).filter((field) => !RECIPE_PUBLICATION_FIELDS.has(field));
    if (unknownFields.length > 0) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `unknown recipe publication fields: ${unknownFields.join(', ')}`);
    }
    const requiredFields = [...RECIPE_PUBLICATION_FIELDS];
    if (requiredFields.some((field) => !isNonEmptyString(publication[field]))) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `recipe publication requires ${requiredFields.join(', ')}`);
    }
    if (publication.schema !== 'content-lab.recipe-publication.v1') {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'recipe publication schema mismatch');
    }
    if (publication.lane !== this.lane) {
      throw closed('CONTENT_LAB_LANE_MISMATCH', 'recipe publication lane must match the dedicated adapter lane');
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(publication.recipeSpecHash)) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'recipeSpecHash must be sha256 hex');
    }
    if (new TextEncoder().encode(publication.recipeSpecCanonical).byteLength > MAX_RECIPE_SPEC_BYTES) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', `recipeSpecCanonical exceeds ${MAX_RECIPE_SPEC_BYTES} bytes`);
    }
    let recipeSpec;
    try { recipeSpec = JSON.parse(publication.recipeSpecCanonical); } catch {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'recipeSpecCanonical must be valid JSON');
    }
    if (!isPlainObject(recipeSpec) || !['dossier.recipe-spec.v2', 'dossier.recipe-spec.v3', 'dossier.recipe-spec.v4'].includes(recipeSpec.schema)) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'recipeSpecCanonical schema mismatch');
    }
    assertNoFreeFormPrompt(recipeSpec, 'recipeSpec');
    if (!idempotencyKey) {
      throw closed('CONTENT_LAB_IDEMPOTENCY_REQUIRED', 'recipe publication idempotency key is required');
    }
    return this.#request('/api/control-plane/v1/recipes', {
      method: 'POST',
      body: publication,
      pageId: publication.pageId,
      requestId,
      idempotencyKey,
      idempotent: true,
      kind: 'recipe',
      timeoutMs: callTimeoutMs('publishRecipe', this.callerBudgetMs),
    });
  }

  getJobStatus({ jobId, pageId, requestId } = {}) {
    if (!jobId) throw closed('CONTENT_LAB_JOB_REQUIRED', 'jobId is required');
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    return this.#request(`/api/control-plane/v1/jobs/${encodeURIComponent(jobId)}`, {
      pageId, requestId, idempotent: true, kind: 'status',
      timeoutMs: callTimeoutMs('jobStatus', this.callerBudgetMs),
    });
  }

  getArtifacts({ jobId, pageId, requestId } = {}) {
    if (!jobId) throw closed('CONTENT_LAB_JOB_REQUIRED', 'jobId is required');
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    return this.#request(`/api/control-plane/v1/jobs/${encodeURIComponent(jobId)}/artifacts`, {
      pageId, requestId, idempotent: true, kind: 'artifacts',
      timeoutMs: callTimeoutMs('artifacts', this.callerBudgetMs),
    });
  }

  getStalePaidArtifacts({ jobId, pageId, requestId } = {}) {
    if (!jobId) throw closed('CONTENT_LAB_JOB_REQUIRED', 'jobId is required');
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    return this.#request(`/api/control-plane/v1/jobs/${encodeURIComponent(jobId)}/artifacts`, {
      pageId, requestId, idempotent: true, kind: 'stalePaidArtifacts',
      timeoutMs: callTimeoutMs('artifacts', this.callerBudgetMs),
    });
  }

  getVisualAdmission({ jobId, pageId, outputIndex, sha256, bytes, requestId } = {}) {
    if (!isNonEmptyString(jobId) || !Number.isSafeInteger(outputIndex) || outputIndex < 0
      || !SHA256_PATTERN.test(sha256) || !Number.isSafeInteger(bytes) || bytes <= 0) {
      throw closed('CONTENT_LAB_CONTRACT_MISMATCH', 'visual admission requires exact output identity and bytes');
    }
    if (!pageId) throw closed('CONTENT_LAB_PAGE_REQUIRED', 'pageId is required');
    return this.#request(`/api/control-plane/v1/jobs/${encodeURIComponent(jobId)}/visual-admission/${outputIndex}`, {
      method: 'POST', body: { sha256, bytes }, pageId, requestId,
      kind: 'visualAdmission', timeoutMs: callTimeoutMs('visualAdmission', this.callerBudgetMs), retries: 0,
    });
  }

  async #request(path, { method = 'GET', body, pageId, requestId, idempotencyKey, idempotent = false, kind, timeoutMs = this.timeoutMs, retries = this.retries } = {}) {
    const url = `${this.baseUrl}${path}`;
    // pageId and idempotencyKey flow verbatim into headers, so — same as
    // requestId — they are validated as bounded, safe tokens up front, outside
    // the retry loop: an invalid value fails closed with zero network calls
    // and no retries.
    // pageId is absent only on the fleet-wide routes (the roster snapshot,
    // the roster refresh and the format-contract read model); every
    // page-scoped caller validates its own before getting here.
    const validatedPageId = pageId === undefined || pageId === null ? null : validatePageId(pageId);
    const validatedIdempotencyKey = validateIdempotencyKey(idempotencyKey);
    const validatedRequestId = validateRequestId(requestId);
    // One X-Request-Id is generated per logical call and reused on every retry attempt.
    const effectiveRequestId = validatedRequestId || generateRequestId(validatedPageId);
    let lastError;
    // Import/registration/status calls keep their existing behavior. A job
    // create alone must remember an earlier ambiguous attempt across retries.
    const jobCreate = kind === 'create' && path === '/api/control-plane/v1/jobs';
    let createAckUnknown = false;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      let submitted = false;
      let responseStatus = null;
      const earlierCreateAckUnknown = createAckUnknown;
      try {
        const request = {
          method,
          // The constructor approves this origin only. A redirect is a
          // non-success response, never authority to contact another origin.
          redirect: 'manual',
          headers: this.#headers({ pageId: validatedPageId, requestId: effectiveRequestId, idempotencyKey: validatedIdempotencyKey }),
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller.signal,
        };
        submitted = true;
        const response = await this.fetch(url, request);
        if (!response || typeof response.status !== 'number') throw closed('CONTENT_LAB_RESPONSE_INVALID', 'invalid response');
        responseStatus = response.status;
        // Only an initial definite refusal establishes that this create was
        // unpaid. A positive response may already be durable before its body
        // becomes unreadable, and later refusals cannot revoke that ambiguity.
        if (jobCreate && !(responseStatus >= 400 && responseStatus < 500
          && responseStatus !== 408 && responseStatus !== 429)) {
          createAckUnknown = true;
        }
        if (response.ok) {
          // The AbortController/timer stays live through the full body read so a
          // hanging or slow-drip body is still bound by the request timeout, not
          // just the time to receive headers.
          const payload = await parseJson(response, kind, controller, MAX_CONTENT_LAB_RESPONSE_BODY_BYTES);
          clearTimeout(timer);
          return payload;
        }
        // Non-2xx: every branch below either retries or throws, and in both
        // cases the caller must not hold this attempt's connection open with
        // an unconsumed body. The general doors read a bounded prefix now,
        // under the same abort timer, to classify an outage (non-JSON body)
        // from a contract answer (JSON body) — so retried 503s (etc.) release
        // their body on every single attempt, not just the last one.
        let errorBodyText = '';
        let errorBodyComplete = true;
        // A 409 on the recipe registration door is the Lab's DECISION about
        // this publication, not contention (registration is serialised by a
        // file lock). It keeps one record per page+recipe+engine+recipeVersion.
        // Since Content Lab eb5f259 (#166, live 2026-09-25) it ACCEPTS a
        // re-registration whose recipe bytes are identical under a new Dossier
        // revision and idempotency key; it still answers "recipe tuple is
        // already registered with different bytes" when the recipe bytes
        // differ, and 409s its other validation refusals. Retried as an
        // idempotent 409 each ended as CONTENT_LAB_REQUEST_FAILED, a 503
        // "outage" with the reason dropped (zcode/silhouette-authority.md
        // section 6). It is refused by name, fail-closed, carrying the Lab's
        // own bounded reason.
        if (response.status === 409 && kind === 'recipe') {
          const reason = await recipeRefusalReason(response, controller);
          clearTimeout(timer);
          const refusal = closed(
            reason && /already registered with different bytes/i.test(reason)
              ? 'CONTENT_LAB_RECIPE_TUPLE_REGISTERED_DIFFERENT_BYTES' : 'CONTENT_LAB_REQUEST_REJECTED',
            `Content Lab refused the recipe registration (409)${reason ? `: ${reason}` : ''}`,
          );
          refusal.failClosed = true;
          refusal.upstreamStatus = 409;
          refusal.upstreamReason = reason;
          throw refusal;
        }
        // The Lab's definite "source windows exhausted" verdict on job create
        // (Content Lab #197) must survive as a distinct, fail-closed refusal so
        // the runner records the page out of footage instead of treating it as
        // contention. The refusal body is read here (only the create door can
        // carry it); any other 409 keeps the existing idempotent-retry handling.
        if (response.status === 429 && jobCreate) {
          const budget = await admissionBudgetRefusal(response, controller);
          if (budget) {
            // This refusal created no job. It cannot disprove an earlier POST
            // whose acknowledgement was lost in this same logical request.
            createAckUnknown = earlierCreateAckUnknown;
            const refusal = closed('CONTENT_LAB_REQUEST_FAILED', 'Content Lab generation budget reached');
            Object.assign(refusal, budget, { upstreamStatus: 429, noRetry: true });
            throw refusal;
          }
        } else if (response.status === 409 && kind === 'create') {
          const reason = await recipeRefusalReason(response, controller);
          clearTimeout(timer);
          if (reason === 'source_windows_exhausted') {
            const exhausted = closed('CONTENT_LAB_SOURCE_WINDOWS_EXHAUSTED', 'Content Lab reports the page source windows are exhausted (409)');
            exhausted.failClosed = true;
            exhausted.upstreamStatus = 409;
            exhausted.upstreamReason = reason;
            throw exhausted;
          }
          // The refusal read above already consumed the body; skip the general
          // drain for this one door.
        } else {
          const prefix = await readErrorBodyPrefix(response, controller);
          errorBodyText = prefix.text;
          errorBodyComplete = prefix.complete;
        }
        if (response.status === 401 || response.status === 403) {
          clearTimeout(timer);
          throw closed('CONTENT_LAB_AUTH_FAILED', 'Content Lab auth failed');
        }
        let bodyIsJson = false;
        if (errorBodyText.trim()) {
          try { JSON.parse(errorBodyText); bodyIsJson = true; } catch { bodyIsJson = false; }
        }
        if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 409 && response.status !== 429) {
          clearTimeout(timer);
          // A non-JSON (HTML/text) 4xx body means the Lab never answered with
          // its envelope — a proxy/app-platform "Application not found" page —
          // so it is an outage, not a contract decision. A JSON body is the
          // Lab's own answer: 404 keeps its job-lost meaning, everything else
          // is a bounded refusal. An EMPTY body, a JSON primitive, and a body
          // whose bounded read was cut off before we could classify it all keep
          // the status-derived code: only a body we actually read as HTML/text
          // is an outage, and an oversized JSON refusal must not be mistaken
          // for one (the prefix read is not the response cap, so over-cap here
          // still means "unknown", fail-closed by status).
          const prefixLooksHtml = errorBodyText.trimStart().startsWith('<');
          const definitelyNonJson = errorBodyComplete && errorBodyText.trim() !== '' && !bodyIsJson;
          if (definitelyNonJson || (!errorBodyComplete && prefixLooksHtml)) {
            const unavailable = closed('CONTENT_LAB_UNAVAILABLE',
              `Content Lab unavailable (HTTP ${response.status})`);
            unavailable.noRetry = true;
            unavailable.upstreamStatus = response.status;
            throw unavailable;
          }
          // A complete status response naming the missing job is authoritative;
          // empty/proxy/other-route 404s cannot prove this paid job is gone.
          if (response.status === 404 && kind === 'status' && errorBodyComplete && isLabJobNotFoundBody(errorBodyText)) {
            const notFound = closed('CONTENT_LAB_CONTRACT_MISMATCH', `Content Lab request rejected with ${response.status}`);
            notFound.jobNotFound = true;
            throw notFound;
          }
          throw closed(response.status === 404 ? 'CONTENT_LAB_CONTRACT_MISMATCH' : 'CONTENT_LAB_REQUEST_REJECTED',
            `Content Lab request rejected with ${response.status}`);
        }
        // 5xx, 408, 429, and the non-special 409 doors share the retryable arm.
        // A 5xx is the Lab (or a proxy in front of it) saying it is down —
        // CONTENT_LAB_UNAVAILABLE; the rest keep the existing
        // CONTENT_LAB_REQUEST_FAILED. Retry behavior is unchanged from before:
        // retryable statuses retry, others fail closed.
        clearTimeout(timer);
        lastError = closed(response.status >= 500 ? 'CONTENT_LAB_UNAVAILABLE' : 'CONTENT_LAB_REQUEST_FAILED',
          response.status >= 500
            ? `Content Lab unavailable (HTTP ${response.status})`
            : `Content Lab request failed with ${response.status}`);
        // Retain the status as a bounded diagnostic, not the upstream body.
        // The body can carry secrets or untrusted HTML; the numeric response is
        // enough for the control plane to distinguish an unavailable service
        // from a transport timeout in its durable operator record.
        lastError.upstreamStatus = response.status;
        if (!retryable(response.status, idempotent) || attempt === retries) {
          lastError.noRetry = true;
          throw lastError;
        }
        await this.#delay(attempt, response);
      } catch (error) {
        clearTimeout(timer);
        const isKnown = error instanceof Error && typeof error.code === 'string' && error.code.startsWith('CONTENT_LAB_');
        lastError = isKnown
          ? error
          : closed('CONTENT_LAB_UNAVAILABLE', error?.name === 'AbortError'
            ? 'Content Lab unavailable: request timed out'
            : 'Content Lab unavailable: network failure');
        if (jobCreate) {
          if (submitted && responseStatus === null) createAckUnknown = true;
          lastError.createAckUnknown = createAckUnknown;
          if (Number.isInteger(responseStatus) && responseStatus >= 100 && responseStatus <= 599) {
            lastError.upstreamStatus = responseStatus;
          }
        }
        if (lastError.failClosed || lastError.noRetry || attempt === retries) throw lastError;
        await this.#delay(attempt, null);
      }
    }
    throw lastError;
  }

  async #delay(attempt, response) {
    const retryAfterMs = boundRetryAfterMs(parseRetryAfterMs(response), this.maxRetryAfterMs);
    const waitMs = retryAfterMs !== null ? retryAfterMs : this.#backoffDelayMs(attempt);
    if (waitMs > 0) await this.sleep(waitMs);
  }

  #backoffDelayMs(attempt) {
    const capped = Math.min(this.maxBackoffMs, this.baseDelayMs * 2 ** attempt);
    return Math.round(this.random() * capped);
  }

  #headers({ pageId, requestId, idempotencyKey }) {
    const headers = {
      Authorization: `Bearer ${this.token}`,
      ...(this.apiKey === undefined ? {} : { 'X-Api-Key': this.apiKey }),
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Content-Lab-Contract': 'v1',
      'X-Request-Id': requestId,
      'X-RT-Lane': this.lane,
    };
    // Only the fleet-wide calls (roster snapshot, roster refresh,
    // format contracts) have no page id; sending the header with an empty
    // value would trip the Lab's bounded-header checks for no reason.
    if (pageId) headers['X-RT-Page-Id'] = pageId;
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
    return headers;
  }
}

export function assertNoFreeFormPrompt(value, path = 'job') {
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertNoFreeFormPrompt(entry, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== 'object') return;
  for (const [key, entry] of Object.entries(value)) {
    const typedPromptReference = path === 'recipeSpec.production' && key === 'promptModuleId';
    const typedPromptControl = path === 'recipeSpec.production.controls'
      && key === 'optimize_prompt'
      && typeof entry === 'boolean';
    if (PROMPT_KEY.test(key) && !typedPromptReference && !typedPromptControl) {
      throw closed('CONTENT_LAB_FREEFORM_PROMPT_REJECTED', `free-form prompt/instruction field rejected at ${path}.${key}`);
    }
    assertNoFreeFormPrompt(entry, `${path}.${key}`);
  }
}

async function parseJson(response, kind, controller, maxBytes) {
  const text = await readBoundedBody(response, controller, maxBytes);
  if (text.length === 0) {
    const unavailable = closed('CONTENT_LAB_UNAVAILABLE', 'Content Lab unavailable: empty response body');
    unavailable.noRetry = true;
    throw unavailable;
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    const unavailable = closed('CONTENT_LAB_UNAVAILABLE',
      'Content Lab unavailable: response body is not JSON');
    unavailable.noRetry = true;
    throw unavailable;
  }
  return validateResponseEnvelope(kind, payload);
}

// The Lab's refusal reason from a JSON error body (`detail`, `error` or
// `message`), as one bounded line of printable text, or null. Nothing else
// from the body is kept: only the Lab's own refusal sentence is surfaced.
const MAX_REFUSAL_BODY_BYTES = 4_096;
async function admissionBudgetRefusal(response, controller) {
  let payload;
  try {
    payload = JSON.parse(await readBoundedBody(response, controller, MAX_REFUSAL_BODY_BYTES));
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return null;
  }
  const detail = payload?.detail;
  if (detail?.error !== 'generation_daily_budget_reached') return null;
  // The admission endpoint supplies positive decimal seconds from the same
  // clock as its UTC body. Persist the absolute UTC deadline rather than
  // shifting it by transport latency. Missing/invalid timing remains a named
  // refusal, with ordinary bounded backoff instead of an invented reset.
  const retryAfter = response.headers?.get?.('retry-after');
  const validRetryAfter = typeof retryAfter === 'string' && /^[1-9]\d{0,4}$/.test(retryAfter)
    && Number(retryAfter) <= 86_400;
  const reset = validRetryAfter ? canonicalBudgetReset(detail?.resets_at) : null;
  return {
    error: 'generation_daily_budget_reached', errorClass: 'generation_budget',
    errorDetail: reset ? `daily_budget reset=${reset.replace('.000Z', 'Z')}` : 'daily_budget',
  };
}

function canonicalBudgetReset(value) {
  if (typeof value !== 'string'
      || !/^\d{4}-\d{2}-\d{2}T00:00:00(?:\.000)?(?:Z|\+00:00)$/.test(value)) return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  const canonical = new Date(milliseconds).toISOString();
  const utc = value.replace(/\+00:00$/, 'Z');
  const normalized = utc.endsWith('.000Z') ? utc : utc.replace(/Z$/, '.000Z');
  return canonical === normalized ? canonical : null;
}

// A reset is scheduling metadata only for the Lab's named budget refusal.
// Other provider details retain their existing bounded diagnostic meaning.
export function generationBudgetReset(result, observedAt) {
  if ((result?.error ?? result?.remoteError) !== 'generation_daily_budget_reached'
      || result?.errorClass !== 'generation_budget' || !isLabFailureDetail(result?.errorDetail)) return null;
  const match = /^daily_budget reset=(\d{4}-\d{2}-\d{2}T00:00:00Z)$/.exec(result.errorDetail);
  const reset = match ? canonicalBudgetReset(match[1]) : null;
  if (observedAt !== undefined && (!Number.isFinite(Date.parse(observedAt))
      || Math.abs(Date.parse(reset) - Date.parse(observedAt)) > 24 * 60 * 60 * 1000)) return null;
  return reset;
}

async function recipeRefusalReason(response, controller) {
  let text;
  try {
    text = await readBoundedBody(response, controller, MAX_REFUSAL_BODY_BYTES);
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return null;
  }
  let payload;
  try { payload = JSON.parse(text); } catch { return null; }
  const value = [payload?.detail, payload?.error, payload?.message].find((entry) => typeof entry === 'string');
  if (!value) return null;
  const line = value.replace(/[\s\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, 200);
  return line || null;
}

// Reads a bounded prefix of a non-2xx body for outage-vs-contract classification
// only. Unlike readBoundedBody, an oversized or otherwise
// unreadable body is reported as `complete: false` with whatever prefix was
// decoded — never as a fail-closed oversize refusal, and never silently treated
// as "not JSON" by the caller: the caller must fall back to the status-derived
// code unless the prefix is clearly an HTML/text proxy page. Abort/timeout still
// propagates so a hanging body keeps the request bound.
async function readErrorBodyPrefix(response, controller) {
  const abortPromise = new Promise((_, reject) => {
    if (controller.signal.aborted) {
      reject(abortError());
      return;
    }
    controller.signal.addEventListener('abort', () => reject(abortError()), { once: true });
  });

  const reader = typeof response?.body?.getReader === 'function' ? response.body.getReader() : null;
  if (reader) {
    try {
      const { text, truncated } = await Promise.race([
        readStreamedBodyPrefix(reader, MAX_REFUSAL_BODY_BYTES),
        abortPromise,
      ]);
      if (truncated) cancelBodyReader(reader);
      return { text, complete: !truncated };
    } catch (error) {
      cancelBodyReader(reader);
      if (error?.name === 'AbortError') throw error;
      return { text: '', complete: false };
    }
  }

  // Fallback: no streamable body (mocks, or a fetch impl that doesn't expose one).
  try {
    const text = await Promise.race([response.text(), abortPromise]);
    const byteLength = new TextEncoder().encode(text).length;
    if (byteLength > MAX_REFUSAL_BODY_BYTES) {
      return { text: text.slice(0, MAX_REFUSAL_BODY_BYTES), complete: false };
    }
    return { text, complete: true };
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return { text: '', complete: false };
  }
}

// A prefix-only twin of readStreamedBody: decodes chunks until the byte cap is
// crossed, then returns the decoded prefix with `truncated: true` instead of
// throwing RESPONSE_TOO_LARGE. The caller cancels the reader on truncation.
async function readStreamedBodyPrefix(reader, maxBytes) {
  const decoder = new TextDecoder('utf-8', { fatal: false });
  let text = '';
  let receivedBytes = 0;
  for (;;) {
    const result = await reader.read();
    if (!result || typeof result !== 'object' || typeof result.done !== 'boolean') {
      throw closed('CONTENT_LAB_STREAM_CHUNK_INVALID', 'Content Lab response stream returned an invalid read result');
    }
    const { done, value } = result;
    if (done) break;
    if (!(value instanceof Uint8Array)) {
      throw closed('CONTENT_LAB_STREAM_CHUNK_INVALID', 'Content Lab response stream produced a non-Uint8Array chunk');
    }
    if (value.byteLength === 0) continue;
    const remaining = maxBytes - receivedBytes;
    const prefix = value.subarray(0, remaining);
    receivedBytes += prefix.byteLength;
    text += decoder.decode(prefix, { stream: true });
    if (value.byteLength > remaining) return { text, truncated: true };
  }
  text += decoder.decode();
  return { text, truncated: false };
}

function retryable(status, idempotent) {
  if (status === 409) return idempotent;
  return RETRY_STATUSES.has(status);
}

function parseRetryAfterMs(response) {
  if (!response || typeof response.headers?.get !== 'function') return null;
  const raw = response.headers.get('Retry-After');
  if (raw === null || raw === undefined) return null;
  const trimmed = String(raw).trim();
  if (trimmed === '') return null;
  if (/^\d+$/.test(trimmed)) {
    const seconds = Number(trimmed);
    return Number.isFinite(seconds) ? seconds * 1000 : null;
  }
  const dateMs = Date.parse(trimmed);
  if (Number.isNaN(dateMs)) return null;
  return Math.max(0, dateMs - Date.now());
}

function boundRetryAfterMs(ms, maxMs) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return null;
  return Math.min(Math.max(0, ms), maxMs);
}

function generateRequestId(pageId) {
  const suffix = typeof globalThis.crypto?.randomUUID === 'function' ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `cp-${String(pageId || 'global').replace(/[^a-zA-Z0-9_.:-]/g, '-')}-${suffix}`;
}

function isLabJobNotFoundBody(text) {
  let body;
  try { body = JSON.parse(text); } catch { return false; }
  return Boolean(body) && typeof body === 'object' && !Array.isArray(body) && body.detail === 'job not found';
}

function closed(code, message) {
  const error = new Error(message);
  error.code = code;
  // Diagnostic (hotfix/lab-schema-detail): the named 503 carries only the code;
  // the message names WHICH field of the Lab's answer failed the contract.
  if (code === 'CONTENT_LAB_SCHEMA_MISMATCH') console.error('content lab schema mismatch', { detail: String(message).slice(0, 300) });
  if (
    code === 'CONTENT_LAB_AUTH_FAILED' ||
    code === 'CONTENT_LAB_SCHEMA_MISMATCH' ||
    code === 'CONTENT_LAB_CONTRACT_MISMATCH' ||
    code === 'CONTENT_LAB_REQUEST_REJECTED' ||
    code === 'CONTENT_LAB_RESPONSE_TOO_LARGE' ||
    code === 'CONTENT_LAB_REQUEST_ID_INVALID' ||
    code === 'CONTENT_LAB_PAGE_ID_INVALID' ||
    code === 'CONTENT_LAB_IDEMPOTENCY_KEY_INVALID' ||
    code === 'CONTENT_LAB_TIMEOUT_INVALID' ||
    code === 'CONTENT_LAB_STREAM_CHUNK_INVALID' ||
    code === 'CONTENT_LAB_RETRIES_INVALID' ||
    code === 'CONTENT_LAB_BASE_DELAY_INVALID' ||
    code === 'CONTENT_LAB_MAX_BACKOFF_INVALID' ||
    code === 'CONTENT_LAB_MAX_RETRY_AFTER_INVALID'
  ) {
    error.failClosed = true;
  }
  return error;
}

// Independently reviewed clauses outside the bounded census generator.
// Census diagnostic D00-type: Number.isFinite(value) is true only for primitive numbers, so deleting this single type conjunct cannot admit a non-number while Number.isFinite remains.
// Census diagnostic D00-finite: With the number-type conjunct retained and finite numeric min/max at every caller, NaN fails both comparisons and +Infinity/-Infinity fails at least one inclusive bound; deleting only Number.isFinite cannot change acceptance.

// Tolerated optional fields: Content Lab's provider failure cause on a terminal
// job status (errorClass e.g. "moderation", errorDetail e.g. "E005"). Unlike
// `optional`, a malformed value never fails the poll: it is dropped and logged,
// so a Lab-side formatting slip cannot turn every status poll into a schema
// mismatch. Any other unknown field is still refused above. Defined at the end
// of the file so the line-keyed citation registries above keep their anchors.
// The two charset predicates are exported so the store side (replenishRun.js)
// re-checks the same bound before anything reaches cp_jobs.output_json.
const LAB_FAILURE_CLASS_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/;
// No quotes, angle brackets, ampersands, backslashes, backticks, tabs or newlines.
const LAB_FAILURE_DETAIL_PATTERN = /^[A-Za-z0-9 _.:,;/()#=+-]{1,64}$/;

export function isLabFailureClass(value) {
  return typeof value === 'string' && LAB_FAILURE_CLASS_PATTERN.test(value);
}

export function isLabFailureDetail(value) {
  return typeof value === 'string' && value.trim() === value && LAB_FAILURE_DETAIL_PATTERN.test(value);
}

// Content Lab #267 top-level `zeroPlanRecipes` list: [{recipeId?, engine,
// recipeVersion?, zeroReason}]. It is advisory only — the worker never acts on
// it — so a malformed list is dropped (never a hard failure). A well-formed
// list is an array of small objects whose four known keys, when present, are
// bounded strings; anything else is dropped by the tolerant-field path.
const ZERO_PLAN_RECIPE_FIELDS = new Set(['recipeId', 'engine', 'recipeVersion', 'zeroReason']);
function isZeroPlanRecipes(value) {
  if (!Array.isArray(value) || value.length > MAX_CAPABILITY_ENTRIES) return false;
  return value.every((entry) => {
    if (!isPlainObject(entry)) return false;
    const unknown = Object.keys(entry).filter((key) => !ZERO_PLAN_RECIPE_FIELDS.has(key));
    if (unknown.length > 0) return false;
    if (entry.recipeId !== undefined && !isNonEmptyString(entry.recipeId)) return false;
    if (entry.engine !== undefined && !isNonEmptyString(entry.engine)) return false;
    if (entry.recipeVersion !== undefined && !isNonEmptyString(entry.recipeVersion)) return false;
    if (entry.zeroReason !== undefined && !isBoundedString(entry.zeroReason, MAX_ZERO_REASON_LENGTH)) return false;
    return true;
  });
}

function tolerantFieldValid(validatorName, value, payload) {
  if (validatorName === 'zeroPlanRecipes') return isZeroPlanRecipes(value);
  if (!['failed', 'error', 'cancelled'].includes(payload.status)) return false;
  if (validatorName === 'failureClass') return isLabFailureClass(value);
  if (validatorName === 'failureDetail') return isLabFailureDetail(value);
  return false;
}

// A cause sent on a non-terminal status is a Lab contract slip that would repeat
// on every poll of that job. Warn once per job id per isolate. The set is bounded
// in count (cleared at 512) and in bytes: a job id longer than 200 chars is never
// remembered (it warns on every poll instead), because the schema bounds jobId
// only as non-empty and a retained multi-megabyte id would pin its bytes for the
// isolate's life. It is skipped, not sliced: a V8 slice keeps its parent alive.
const MAX_WARNED_NON_TERMINAL_JOBS = 512;
const MAX_WARNED_JOB_ID_LENGTH = 200;
const warnedNonTerminalJobs = new Set();

function firstNonTerminalWarning(payload) {
  if (['failed', 'error', 'cancelled'].includes(payload.status)) return true;
  const jobKey = String(payload.jobId);
  if (jobKey.length > MAX_WARNED_JOB_ID_LENGTH) return true;
  if (warnedNonTerminalJobs.has(jobKey)) return false;
  if (warnedNonTerminalJobs.size >= MAX_WARNED_NON_TERMINAL_JOBS) warnedNonTerminalJobs.clear();
  warnedNonTerminalJobs.add(jobKey);
  return true;
}

function dropMalformedTolerantFields(kind, def, payload) {
  let warn = null;
  for (const [field, validatorName] of Object.entries(def.tolerant ?? {})) {
    if (payload[field] === undefined || tolerantFieldValid(validatorName, payload[field], payload)) continue;
    warn ??= firstNonTerminalWarning(payload);
    if (warn) {
      console.warn('content lab tolerated field dropped', {
        kind, field, status: String(payload.status).slice(0, 32), value: String(JSON.stringify(payload[field])).slice(0, 80),
      });
    }
    delete payload[field];
  }
  return payload;
}
