export const BLOCKED_POLICY = Object.freeze({
  mode: 'BLOCKED',
  reason: 'no approved policy source resolved',
});

// Canonical, closed enum for the resolved policy's business `mode`. Any
// source-contributed value outside this set (typo, retired business state,
// or attacker-shaped input) must never reach a consumer — it is treated as
// an invalid_mode blocker and forced to BLOCKED below, the same as a missing
// mode. `ACTIVE` is the only mode the live gate is ever allowed to arm on
// (see liveGate.js's `validResolvedPolicy`), so this enum is intentionally
// minimal rather than pre-inventing unrequested business states.
export const VALID_POLICY_MODES = Object.freeze(['ACTIVE', 'BLOCKED']);
const VALID_POLICY_MODE_SET = new Set(VALID_POLICY_MODES);

const SOURCE_ORDER = Object.freeze([
  ['pageOverride', 'page_override'],
  ['campaignRequirement', 'campaign_requirement'],
  ['nicheDefault', 'niche_default'],
]);
const UNION_FIELDS = new Set(['exclusions']);
const INTERSECTION_FIELDS = new Set(['allowedFormats', 'allowedEngines', 'allowedNiches']);
const SOURCE_META_KEYS = new Set(['id', 'version', 'sourceId', 'sourceVersion', 'policy']);
const VOLATILE_KEYS = new Set([
  'audit', 'attestation', 'createdAt', 'displayLabel', 'expiresAt', 'generatedAt',
  'lastSeenAt', 'nonce', 'observedAt', 'requestId', 'resolvedAt', 'timestamp', 'updatedAt',
]);

export function stableStringify(value) {
  return JSON.stringify(canonicalize(value));
}

export async function sha256Hex(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

// Do these exact stored bytes hash to the revision that names them? The stored
// hash is a publication's identity; a reader that parses the JSON without asking
// would accept a hash-preserving change to the bytes as the locked recipe (pass
// 20: five authority reads did). One fence for every reader, so the check cannot
// drift between them. Verified pairs are cached because the materializer asks
// this for every inventory row of every compiled page -- thousands of rows over
// a few dozen distinct specs. A mismatch is never cached: it is corruption, and
// each reader must see it for itself.
const RECIPE_SPEC_HASH = /^sha256:[0-9a-f]{64}$/;
const VERIFIED_RECIPE_SPEC_LIMIT = 2_048;
// Account for two bytes per UTF-16 code unit, even for compact ASCII strings.
// Entry count alone can retain hundreds of MiB across current/historical recipes.
const VERIFIED_RECIPE_SPEC_BYTES = 4 * 1024 * 1024;
const verifiedRecipeSpecJsonByHash = new Map();
let verifiedRecipeSpecBytes = 0;
export async function recipeSpecHashMatches(recipeSpecJson, recipeSpecHash) {
  if (typeof recipeSpecJson !== 'string' || !RECIPE_SPEC_HASH.test(recipeSpecHash || '')) return false;
  if (verifiedRecipeSpecJsonByHash.get(recipeSpecHash) === recipeSpecJson) return true;
  if (`sha256:${await sha256Hex(recipeSpecJson)}` !== recipeSpecHash) return false;
  const entryBytes = recipeSpecJson.length * 2;
  if (entryBytes > VERIFIED_RECIPE_SPEC_BYTES) return true;
  // Concurrent checks of the same uncached pair may both finish hashing.
  if (verifiedRecipeSpecJsonByHash.get(recipeSpecHash) === recipeSpecJson) return true;
  while (verifiedRecipeSpecJsonByHash.size >= VERIFIED_RECIPE_SPEC_LIMIT
      || verifiedRecipeSpecBytes + entryBytes > VERIFIED_RECIPE_SPEC_BYTES) {
    const oldestHash = verifiedRecipeSpecJsonByHash.keys().next().value;
    verifiedRecipeSpecBytes -= verifiedRecipeSpecJsonByHash.get(oldestHash).length * 2;
    verifiedRecipeSpecJsonByHash.delete(oldestHash);
  }
  verifiedRecipeSpecJsonByHash.set(recipeSpecHash, recipeSpecJson);
  verifiedRecipeSpecBytes += entryBytes;
  return true;
}

export async function policyHash(resolvedPolicy) {
  return `sha256:${await sha256Hex(stableStringify(stripVolatile(resolvedPolicy)))}`;
}

export async function resolvePolicy({
  pageId,
  pageOverride = null,
  campaignRequirement = null,
  nicheDefault = null,
  blockedPolicy = BLOCKED_POLICY,
} = {}) {
  const raw = { pageOverride, campaignRequirement, nicheDefault };
  const sources = SOURCE_ORDER.map(([key, kind]) => normalizeSource(raw[key], kind)).filter(Boolean);
  const fieldNames = new Set(sources.flatMap((source) => Object.keys(source.values)));
  const policy = {};
  const provenance = {};
  const blockers = [];

  if (sources.length === 0) {
    const blocked = canonicalize(stripVolatile(blockedPolicy) || BLOCKED_POLICY);
    for (const [field, value] of Object.entries(blocked)) {
      defineOwn(policy, field, value);
      defineOwn(provenance, field, { kind: 'blocked', id: 'blocked', version: 1 });
    }
    blockers.push('no_approved_policy_source');
  } else {
    for (const field of [...fieldNames].sort()) {
      const contributors = sources.filter((source) => Object.hasOwn(source.values, field));
      if (contributors.length === 0) continue;
      if (UNION_FIELDS.has(field) || INTERSECTION_FIELDS.has(field)) {
        // A required array field (`exclusions`/`allowedFormats`/etc.) whose
        // contributed value isn't actually an array (hand-edited source row,
        // malformed upstream payload, ...) must never blow up resolution
        // with a raw TypeError — that would crash the caller instead of
        // producing a resolved (and hashable) policy. Fail closed
        // deterministically instead: record a stable `invalid_array:<field>`
        // blocker (which forces BLOCKED below, same as any other blocker)
        // and treat the malformed contributor as contributing nothing to
        // this field, so the union/intersection math stays well-defined.
        const malformed = contributors.filter((source) => !Array.isArray(source.values[field]));
        for (const source of malformed) blockers.push(`invalid_array:${field}:${source.kind}`);
        const wellFormed = contributors.filter((source) => Array.isArray(source.values[field]));
        if (UNION_FIELDS.has(field)) {
          const arrays = wellFormed.map((source) => source.values[field]);
          defineOwn(policy, field, canonicalUnique(arrays.flat()));
          defineOwn(provenance, field, wellFormed.map(sourceProvenance));
        } else {
          const arrays = wellFormed.map((source) => canonicalUnique(source.values[field]));
          defineOwn(policy, field, intersectCanonical(arrays));
          defineOwn(provenance, field, wellFormed.map(sourceProvenance));
          if (policy[field].length === 0) blockers.push(`empty_intersection:${field}`);
        }
      } else {
        defineOwn(policy, field, contributors[0].values[field]);
        defineOwn(provenance, field, sourceProvenance(contributors[0]));
      }
    }
  }

  if (!Object.hasOwn(policy, 'mode')) {
    policy.mode = 'BLOCKED';
    provenance.mode = { kind: 'blocked', id: 'blocked', version: 1 };
    blockers.push('missing_required:mode');
  } else if (!VALID_POLICY_MODE_SET.has(policy.mode)) {
    // A contributing source set `mode` to something outside the canonical
    // enum. Never let an unrecognized mode value pass through: fail closed
    // deterministically via the same blocker-forced BLOCKED path used for a
    // missing mode (below), which also stamps truthful provenance and folds
    // the failure into the policy hash.
    blockers.push('invalid_mode');
  }
  const uniqueBlockers = [...new Set(blockers)].sort();
  if (uniqueBlockers.length > 0) {
    // A non-empty blocker set always forces mode to BLOCKED. When the mode
    // wasn't already BLOCKED (i.e. it came from a real source contributor,
    // or from the missing-sources fallback which already stamped it), the
    // prior provenance would otherwise keep pointing at that truthful-but-
    // superseded source and misrepresent why the policy is actually BLOCKED.
    // Overwrite it with a deterministic, blocker-derived provenance so the
    // (hashed) resolved policy never lies about a forced BLOCKED mode.
    if (policy.mode !== 'BLOCKED') {
      provenance.mode = { kind: 'blocked', id: `forced:${uniqueBlockers.join(',')}`, version: 1 };
    }
    policy.mode = 'BLOCKED';
  }

  const resolved = canonicalize({
    pageId: pageId || null,
    policy,
    provenance,
    blockers: uniqueBlockers,
  });
  // Deep-freeze: `canonicalize` builds fresh plain objects/arrays at every
  // level, so a shallow `Object.freeze` on the top-level result (the prior
  // behavior) left `policy`, `provenance`, and every nested array/object
  // mutable — a consumer (or a bug) could still rewrite
  // `resolved.policy.mode` in place after resolution, silently invalidating
  // the very hash computed over the (now stale) contents below. Freezing
  // every level closes that: the only way to change the resolved policy is
  // to call `resolvePolicy` again, which recomputes the hash to match.
  return deepFreeze({ ...resolved, hash: await policyHash(resolved) });
}

export function deepFreeze(value) {
  if (Array.isArray(value)) {
    for (const entry of value) deepFreeze(entry);
    return Object.freeze(value);
  }
  if (isPlainObject(value) && !Object.isFrozen(value)) {
    for (const key of Object.keys(value)) deepFreeze(value[key]);
    return Object.freeze(value);
  }
  return value;
}

function normalizeSource(input, kind) {
  if (input === undefined || input === null) return null;
  if (!isPlainObject(input)) throw new TypeError(`${kind} must be an object`);
  const wrapped = isPlainObject(input.policy);
  const values = stripVolatile(wrapped ? input.policy : input);
  if (!wrapped) for (const key of SOURCE_META_KEYS) delete values[key];
  if (Object.keys(values).length === 0) return null;
  return {
    kind,
    id: String(input.id ?? input.sourceId ?? `${kind}:anonymous`),
    version: input.version ?? input.sourceVersion ?? 1,
    values: canonicalize(values),
  };
}

function sourceProvenance(source) {
  return canonicalize({ kind: source.kind, id: source.id, version: source.version });
}

function canonicalUnique(values) {
  const byCanonical = new Map();
  for (const value of values.map(canonicalize)) byCanonical.set(stableStringify(value), value);
  return [...byCanonical.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([, value]) => value);
}

function intersectCanonical(arrays) {
  if (arrays.length === 0) return [];
  const later = arrays.slice(1).map((values) => new Set(values.map(stableStringify)));
  const intersected = arrays[0].filter((value) => later.every((set) => set.has(stableStringify(value))));
  // S10 (defect) — this field is documented (see UNION_FIELDS/
  // INTERSECTION_FIELDS above) as an unordered SET: two sources contributing
  // the identical set of values in a different internal order must resolve
  // to the identical canonical result and hash. Without an explicit sort
  // here, this fell through to `canonicalize()`'s blanket "sort every
  // primitive array" behavior to get that determinism — which, as a side
  // effect, ALSO silently reordered every other (genuinely order-sensitive)
  // primitive-array policy field. Now that canonicalize() preserves array
  // order in general (see its own comment below), this field's own
  // order-independence has to be asserted explicitly, the same way
  // canonicalUnique already does for UNION_FIELDS.
  return canonicalUnique(intersected);
}

export function stripVolatile(value) {
  if (Array.isArray(value)) return value.map(stripVolatile).filter((entry) => entry !== undefined);
  if (!isPlainObject(value)) return value;
  const out = {};
  for (const key of Object.keys(value).sort()) {
    if (VOLATILE_KEYS.has(key)) continue;
    const clean = stripVolatile(value[key]);
    if (clean !== undefined) defineOwn(out, key, clean);
  }
  return out;
}

export function canonicalize(value) {
  if (Array.isArray(value)) {
    // S10 (defect) — this used to unconditionally sort any array whose
    // elements are all primitives (bool/number/string), regardless of
    // whether the field is semantically an unordered set. That's correct
    // for UNION_FIELDS/INTERSECTION_FIELDS (allowedFormats, allowedEngines,
    // allowedNiches, exclusions) — but those already assert their own
    // order-independence explicitly via canonicalUnique/intersectCanonical
    // above. For every OTHER primitive-array policy field (anything a
    // single source contributes directly, e.g. an ordered list), this
    // blanket sort silently discarded the configured order before the
    // resolved policy was returned to callers AND before its hash was
    // computed — so the order callers actually configured was neither
    // preserved in the response nor bound by the hash meant to attest to
    // it. canonicalize() must therefore be order-PRESERVING for arrays in
    // general; only fields that are genuinely unordered sets sort
    // themselves, explicitly, upstream of this function.
    return value.map(canonicalize);
  }
  if (!isPlainObject(value)) return value;
  const out = {};
  for (const key of Object.keys(value).sort()) {
    const clean = canonicalize(value[key]);
    if (clean !== undefined) defineOwn(out, key, clean);
  }
  return out;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype;
}

// Assigns `key` as a genuine *own data property* of `out`, never through
// `out[key] = value` bracket/index assignment. That distinction matters for
// exactly one key: `"__proto__"`. `JSON.parse` (and object spread) create a
// literal `"__proto__"` own property safely, but a fresh `{}` has NO own
// `"__proto__"` property yet — so `out['__proto__'] = value` doesn't create
// one either; it invokes `Object.prototype`'s inherited `__proto__` *setter*,
// which (a) silently drops the key from `out`'s own keys (so it vanishes from
// `Object.keys`/`JSON.stringify`/the policy hash — a "key exemption" an
// attacker-shaped source could exploit to smuggle a field past canonicalize
// without it ever being hashed) and (b) replaces `out`'s actual prototype
// with the attacker-supplied value (a classic prototype-pollution gadget
// for any later `for...in`/`in`/plain-property read that isn't hasOwn-
// guarded). `Object.defineProperty` always creates/overwrites a genuine own
// data property regardless of the key's name, so `"__proto__"` round-trips
// as ordinary data — preserved, hashed, and never a pollution vector.
function defineOwn(target, key, value) {
  Object.defineProperty(target, key, { value, writable: true, enumerable: true, configurable: true });
  return target;
}

// Reviewed census survivors; these reasons are qualified to the stated schema and callers.
// Census annotation conjunct:src/domain/policy.js:238:669: Current policy/hash callers supply ordinary JSON-contract objects or arrays; no current caller supplies a custom-prototype object affected by this check. Direct custom-object behavior is excluded.
