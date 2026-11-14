# Preserve private Scrambler produced assets

A legacy source job may have public clips=[] while Scrambler has already saved
produced clips in scramblerRenderedClips. Refuse legacy reconciliation for every
nonempty or malformed private checkpoint; absent and valid empty lists remain
eligible. A stopped-platform proof and revision CAS do not justify rewriting
artifact-bearing history. Source input references/cuts remain unaffected.

Generated-media retention now treats valid private media/thumbnail references
as protected produced assets, rather than unconfirmed strays. Matching public
manifests retain existing R2-confirmed cleanup. Malformed, unenumerable,
cross-owner, alias, escaping or conflicting private metadata holds the whole
job; unrelated true strays can still age normally for valid checkpoints. No
ledger, job-store, compaction, provider or production changes are introduced.

Causal controls on the previous sources show six stopped-runtime checkpoint
cases terminal-rewritten and all three actual failed-producer cases losing
private media at48/24/6hours. The fixes pass153 focused checks covering those
controls, normal empty checkpoints, R2 positive/negative cleanup and malformed
identity/path cases. Existing full JSONL archives already retain checkpoint
metadata; compaction policy is unchanged. Broader exact-head qualification and
independent source review remain required before publication.
