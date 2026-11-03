# Worker live validator (vendored) — control-plane-worker/src @ a963a12d

Source: the LIVE Worker twin, `worker-src-live-a963a12d.tgz`
(`control-plane-worker/src` at commit `a963a12d`), which is byte-identical for
`contentLabClient.js` to tonight's Worker candidate. The live Worker now
running in production (0920bcd8) also carries a byte-identical
`contentLabClient.js`, so this vendored copy matches both. GitHub
`rotational-posting-agent` main is STALE and must never be used as this source.

These files are an unmodified byte-copy of the Worker validator closure needed
to run `validateResponseEnvelope('dossierIngredients', …)` in Node:

- `src/adapters/contentLabClient.js` — the real validator (unmodified).
- `src/adapters/contentLabTimeouts.js`, `src/adapters/postRenderClient.js` and
  `src/domain/*.js` — import closure. `postRenderClient.js` is a stub: the
  dossierIngredients envelope validator never constructs a `PostRenderClient`,
  and the real file pulls the Worker's D1/scheduler closure that has no bearing
  on this validation path.
- `run_dossier_ingredients_contract.mjs` — Node harness. `contentLabClient.js`
  does not export its internal `validateResponseEnvelope`, so the harness
  appends ONE export line to a scratch copy in the same directory (leaving the
  vendored file untouched) and dynamic-imports it.

REFRESH RULE: when the Worker's `validateDossierProductionSelection` /
`validateDossierFormat` / `validateResponseEnvelope` validator changes, replace
these files from the new live Worker twin and update
`tests/test_pool_supplied_worker_contract.py::WORKER_VALIDATOR_SHA256`
(sha256 of `src/adapters/contentLabClient.js`).
