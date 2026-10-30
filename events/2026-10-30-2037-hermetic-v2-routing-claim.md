# Claim the six skipped local v2 card-routing controls

Scope this follow-up to tests/test_abn_factory.py. Six tests already replace the
scene tagger, shot director, card renderer and asset paths, but skip when the
optional external factory package is absent. Their assertions exercise Content
Lab's local vs/quote/diagram guards and episode quote budget, not the native
factory catalog or rendering integration.

Use an explicit hermetic import boundary for those existing local routing tests,
retain their behavioral assertions and availability/kill-switch behavior, and
prove that compiling routing mutations still fail. Do not implement an external
catalog, change production code, install dependencies, invoke a provider, or
claim the real factory integration is covered. Work only on this fresh 8aa-based
branch; preserve the qualified 447 tree and all owner worktrees. Before claiming,
no open PR or relevant owner working diff was found for these two ABN files.
