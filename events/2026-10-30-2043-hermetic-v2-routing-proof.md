# Restore local card-routing coverage without the external factory package

Replace only the six tests' optional import edges with a hermetic module graph.
The actual _v2_scene_cards, _same_entity, _statement and _diagram_steps remain in
use; the original six routing test bodies are AST-identical to main 8aa46b0.
Their original tagger/director/renderer/asset stubs remain explicit dependencies.
This exercises local routing guards, not native factory catalog or renderer
integration; that genuine external acceptance remains unqualified.

Under production Python 3.11, a scrubbed environment and the original offline
sandbox, the baseline selects six skipped tests. The corrected local group runs
nine passing tests: the six original oracles, a strengthened visual kill-switch,
a separate availability gate and complete module/global restoration. The whole
ABN unit-test module passes 302 tests with no skips. Seven isolated compiling
runtime mutations each fail its named behavioral assertion; every patch, command
and failure log is preserved, and the original runtime source is restored.

No runtime, package, provider, network, media-render or production behavior changes.
The qualified 447 checkout and owner worktrees remain untouched. These test-only
results do not invent a new full-suite result or qualify the external dependency.
