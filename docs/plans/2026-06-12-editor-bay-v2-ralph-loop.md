# Editor Bay v2 Ralph Loop

Status: CONT-18 implementation scaffold
Scope: local loop runner, stop-hooks, context-wipe handoff, and Linear/PR summary output.

## Purpose

Each Editor Bay v2 implementation slice must run a three-pass refinement loop before completion.
The loop is intentionally mechanical:

1. The agent leaves a durable pass artifact with remarks, changes, verification evidence, unresolved risks, and the next-pass prompt.
2. The local context-wipe bash removes scratch context for that pass and writes a marker.
3. The next pass starts from the artifact and remarks, not stale conversational context.
4. Stop-hooks block completion until all three passes are present, wiped, verified, and risk-free.

This does not delete Codex global memory or user files. It clears only the loop scratch folder under
`.codex/ralph-loop/<issue>/scratch/pass-N` and records a wipe marker. The human/agent process must
actually start a fresh pass after the marker is written.

## Artifact Layout

All artifacts live under:

```bash
.codex/ralph-loop/<ISSUE_ID>/
```

Files:

```text
manifest.json
pass-1.json
context-wipe-pass-1.marker
pass-2.json
context-wipe-pass-2.marker
pass-3.json
context-wipe-pass-3.marker
gate-report.json
```

## Initialize A Slice

Run from `/Users/risingtidesdev/dev/content-posting-lab`:

```bash
python tools/ralph_loop.py init \
  --repo-root /Users/risingtidesdev/dev/content-posting-lab \
  CONT-13 \
  --branch cont-13-editor-bay-timeline-core
```

## Record A Pass

Every pass must include at least one remark, one change summary, one passing verification item with
evidence, and a next-pass prompt.

```bash
python tools/ralph_loop.py record-pass \
  --repo-root /Users/risingtidesdev/dev/content-posting-lab \
  CONT-13 \
  --pass-number 1 \
  --remark "Reviewed command replay and found missing conflict coverage." \
  --change "Added revision conflict tests and validation path." \
  --verification "pytest timeline tests|passed|python -m pytest tests/test_editor_timeline.py -q" \
  --next-pass-prompt "Start fresh. Read pass-1.json and verify command replay plus conflicts."
```

If a pass has unresolved work, record it explicitly:

```bash
  --unresolved-risk "Renderer evidence is missing for the OpenShot adapter"
```

Any unresolved risk blocks completion until a later pass overwrites or resolves that pass artifact.

## Run The Context-Wipe Bash

After the pass artifact is written:

```bash
bash tools/ralph_loop_context_wipe.sh \
  CONT-13 \
  1 \
  /Users/risingtidesdev/dev/content-posting-lab
```

The script removes:

```text
.codex/ralph-loop/CONT-13/scratch/pass-1
```

and writes:

```text
.codex/ralph-loop/CONT-13/context-wipe-pass-1.marker
```

## Check Stop-Hooks

```bash
python tools/ralph_loop.py check \
  --repo-root /Users/risingtidesdev/dev/content-posting-lab \
  CONT-13
```

Exit code:

- `0`: clear
- `1`: blocked

Stop-hooks block on:

- missing manifest
- wrong repo boundary
- missing pass artifact
- missing remarks
- missing change summary
- missing verification evidence
- failed verification item
- verification item without evidence
- unresolved risks
- missing context-wipe marker

## Render Linear Or PR Summary

Paste this output into the Linear issue or PR:

```bash
python tools/ralph_loop.py linear-comment \
  --repo-root /Users/risingtidesdev/dev/content-posting-lab \
  CONT-13
```

## Dry Run

Use this to prove the loop machinery on a fixture issue:

```bash
python tools/ralph_loop.py dry-run \
  --repo-root /Users/risingtidesdev/dev/content-posting-lab \
  --issue CONT-18-dry-run
```

Expected output contains:

```text
Stop-hook status: clear
Pass 1
Pass 2
Pass 3
Verification
```

## Slice Completion Rule

No Editor Bay v2 child slice should be marked complete until:

1. `python tools/ralph_loop.py check ... <ISSUE_ID>` exits `0`.
2. The Linear/PR summary has been posted.
3. Slice-specific tests, browser checks, or render evidence are included in pass verification.
4. Any waiver is explicit in the issue and explains why the third clean pass is not required.
