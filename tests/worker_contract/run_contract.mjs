// Cross-repo contract runner: validate every Lab variant source against the
// Worker's real managedPhotoPlanValid (byte-copied mirror of
// control-plane-worker/src/domain/managedSlideshowPages.js).
//
// Usage: node run_contract.mjs [fixture.json]
// Exit 0 when every source is valid; exit 1 and print the failing indices
// otherwise (used to prove the mutation contract below).
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { managedPhotoPlanValid } from './managedSlideshowPages.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const fixturePath = process.argv[2] ?? join(here, 'pentwin-variant-sources.json');
const sources = JSON.parse(readFileSync(fixturePath, 'utf8'));

const failures = [];
for (let i = 0; i < sources.length; i += 1) {
  if (!managedPhotoPlanValid(sources[i])) failures.push(i);
}

if (failures.length > 0) {
  console.error(`FAIL: ${failures.length}/${sources.length} sources invalid: ${failures.join(',')}`);
  process.exit(1);
}
console.log(`OK: ${sources.length}/${sources.length} sources valid`);
