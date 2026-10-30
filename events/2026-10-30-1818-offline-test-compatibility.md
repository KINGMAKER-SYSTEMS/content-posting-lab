# Execute the offline teardown and ducking-envelope checks

- Time: 2026-10-30T18:18:21Z
- Agent: Codex worker_review (model identity unavailable)
- Branch: codex-review/offline-test-compat-1030
- Base: 04c7747d792214eb62f63718534c5f593ce839dc
- Type: independent test compatibility repair

Two existing tests failed before reaching their intended checks. The offline
guard's child pytest process inherited disabled plugin discovery and did not
execute its async body. Load pytest_asyncio.plugin explicitly while keeping
discovery disabled in that child. Its original oracle remains one passing body
and one teardown error naming the blocked hub.invalid DNS attempt. The audit
hook still refuses before a real DNS lookup; the global conftest is unchanged.

The ducking-envelope test passed a nested tuple sequence to pytest.approx,
which raises TypeError. Check exactly three ordered time/gain pairs with the
supported flat-pair comparison. Existing flat-volume, OpenShot X/Y and other
interpolation assertions remain unchanged; no backend or media code changes.

On Python 3.14.7, pytest 9.2.0 and pytest-asyncio 1.4.1 with plugin autoload
disabled, the two original named tests fail. The repaired two-file run passes
105 tests. Actual child output verifies one body pass and one expected teardown
error, while the parent passes. Injected gain, time, missing-point and order
faults each fail the numeric oracle. Clearing only the child's recorded attempt
after the audit hook refuses makes the unchanged teardown oracle fail. These
mutants were restored, then all 105 tests passed again.

No full suite, media render, provider call, installation, production access,
merge or push is included. Root review and broader qualification remain separate.
