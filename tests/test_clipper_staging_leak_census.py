"""Every handler that creates a staging dir must clear it on EVERY exit.

This is a census, not a sample. PR #149 fixed one leak and its commit message
claimed it was "the one path that did not" clean up. It was not: four more
sites survived that fix, one of them worse than the one fixed -- a failed batch
job stranded every uploaded source video for the batch, and deleting the job
did not remove them because `delete_clipper_job` removes `<job_id>`, never
`_staging_<batch_id>`.

Counting passes is the wrong instrument for a class you can DECIDE. This derives
its work-list from the code itself, so a handler added next month is covered the
day it lands, and it cannot go stale the way a hand-maintained list does.

An exemption is a decision on the record: mark the site `# staging-leak-exempt:`
with a real reason. A marker with no reason is rejected too, because a silenced
check is indistinguishable from a considered one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "routers" / "clipper.py"
EXEMPT = re.compile(r"#\s*staging-leak-exempt:\s*(?P<reason>.+)")


def _creates_staging(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and child.func.attr == "mkdir"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "staging_dir"):
            return True
    return False


def _cleans_on_failure(node: ast.AST) -> bool:
    """True when a `safe_rmtree(staging_dir)` sits on a path a failure reaches.

    A cleanup in the straight-line body does NOT count: that is exactly where
    `_run_batch_job` had it -- inside the try, just before `status = complete`
    -- so every error skipped it.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Try):
            continue
        for region in (*child.handlers, *(child.finalbody and [child] or [])):
            body = region.body if isinstance(region, ast.ExceptHandler) else child.finalbody
            for call in ast.walk(ast.Module(body=body, type_ignores=[])):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "safe_rmtree"):
                    return True
    return False


def test_every_staging_handler_clears_on_failure():
    source = MODULE.read_text()
    tree = ast.parse(source)
    lines = source.split("\n")

    offenders, exempted, checked = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _creates_staging(node):
            continue
        checked.append(node.name)
        segment = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        marker = EXEMPT.search(segment)
        if marker:
            reason = marker.group("reason").strip()
            if len(reason) < 20:
                offenders.append(f"{node.name} (line {node.lineno}): exemption has no real reason")
            else:
                exempted.append(f"{node.name}: {reason}")
            continue
        if not _cleans_on_failure(node):
            offenders.append(
                f"{node.name} (line {node.lineno}): creates a staging dir but no "
                f"safe_rmtree(staging_dir) is reachable from a failure"
            )

    assert checked, "the census found no staging-dir handlers at all — the detector is broken"
    assert not offenders, (
        f"{len(offenders)} handler(s) leak their staging dir on failure:\n  "
        + "\n  ".join(offenders)
        + f"\n\n(censused {len(checked)}: {', '.join(sorted(checked))}"
        + (f"; exempted {len(exempted)}: {'; '.join(exempted)}" if exempted else "")
        + ")"
    )
