// Cross-repo contract runner: validate a Lab dossier-ingredients catalog against
// the Worker's real envelope validator, byte-for-byte.
//
//   node run_dossier_ingredients_contract.mjs <catalog.json>
//
// Exit 0 when the Worker accepts the payload (prints `ACCEPTED <pageId>`);
// exit 1 when it rejects it (prints `REJECTED <pageId> <code> - <message>`).
//
// contentLabClient.js does not export its internal validateResponseEnvelope, so
// this runner appends ONE export line to a scratch copy in the same directory
// (keeping every validation byte unmodified) and dynamic-imports it. The
// vendored contentLabClient.js file itself is never rewritten.
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const catalogPath = process.argv[2];
if (!catalogPath) {
  console.error('usage: node run_dossier_ingredients_contract.mjs <catalog.json>');
  process.exit(2);
}
const payload = JSON.parse(readFileSync(catalogPath, 'utf8'));

const sourcePath = join(here, 'src/adapters/contentLabClient.js');
const source = readFileSync(sourcePath, 'utf8');
const probePath = join(here, 'src/adapters/.probe-contentLabClient.mjs');
writeFileSync(probePath, `${source}\nexport { validateResponseEnvelope };\n`);
try {
  const mod = await import(`${probePath}?t=${Date.now()}`);
  const result = mod.validateResponseEnvelope('dossierIngredients', payload);
  console.log(`ACCEPTED ${result.pageId}`);
} catch (error) {
  console.error(`REJECTED ${payload.pageId ?? '?'} ${error?.code ?? 'ERROR'} - ${error?.message ?? error}`);
  process.exitCode = 1;
} finally {
  rmSync(probePath, { force: true });
}
