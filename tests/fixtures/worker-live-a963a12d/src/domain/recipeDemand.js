// The recipe format slug is ONE namespace across this deployment, so its bound
// is declared once, here, and imported. It was written out three times before
// pass 60 and the third copy had already drifted: the Lab door and the
// operator's submit door admit 100 characters, the caption corpus reader
// admitted 64, so a legal 65-character key would be accepted at both doors and
// then throw inside `buildDefaults`, taking `loadCaptionCorpusCatalog` down for
// the materializer, the dossier gateway and the authority reconcile for the life
// of the isolate. A corpus id is NOT a format slug and keeps its own bound.
export const FORMAT_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,99}$/;

// Dossier's Rust contract admits weights in [0, 1]. Zero-weight entries remain
// saved page choices; the publication and visual readers select positive entries.
export function recipeFormatWeightIsValid(weight) {
  return Number.isFinite(weight) && weight >= 0 && weight <= 1;
}
