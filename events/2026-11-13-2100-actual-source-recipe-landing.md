# Land the applied source.recipe controls onto current main

Merged current main (ec11584, after #337) into this branch. Main's executor
already advertises the actual Scrambler render speed, the applied Scrambler
reverse/mirror and split-swap, and the Layer B loop flag in source.recipe,
with the drained `_settle_source_call` wrappers around every media call. The
conflicting runtime hunks in routers/control_plane.py and
services/control_plane_sources.py were therefore resolved to main's composed
executor unchanged; this branch's base-8aa runtime patch is not applied.

What lands is the eleven-case regression file
tests/test_source_recipe_applied_transforms.py, which passes unchanged on
current main, plus a Verification line in AGENTS.md. No runtime, encoder,
feature default or Worker field changes. CONTENT_LAB_ADVERTISE_ORIGIN stays
off by default; Worker consumer activation remains a separate step.
