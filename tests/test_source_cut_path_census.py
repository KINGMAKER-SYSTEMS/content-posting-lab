"""CI guard: every sourced-video cut path goes through plan_source_cuts.

The re-cut variety rule (operator 2026-09-30: each re-cut uses a different
part of the master at a different length, and supply never stops) lives in
exactly one place, services.control_plane_sources.plan_source_cuts. A new
code path that builds dossier_source_dna jobs, writes ``sourceCuts`` or
renders a source window without that planner would silently skip the rule.
This census reads every backend module's syntax tree and fails on any such
path, so the class is closed mechanically instead of by review.

A site is any of:

* a ``SourceCut(...)`` construction,
* a dict literal with a ``"sourceCuts"`` key, or with
  ``"sourceKind": "dossier_source_dna"`` (a new source-recut job),
* an assignment to ``...["sourceCuts"]`` or a ``sourceCuts=`` keyword
  (rewriting a job's cuts),
* a call passing ``clip_start_ms=`` (the cut executor renders a window).

Rules: ``SourceCut`` is constructed only inside plan_source_cuts; a job/cut
site's enclosing function calls plan_source_cuts; an executor site's
enclosing function re-derives each cut with source_cut_is_planned (the
planner's own vocabulary check) before rendering it.
"""

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {"tests", "frontend", "node_modules", "__pycache__", "artifacts", "static"}
PLANNER_MODULE = "services/control_plane_sources.py"


def _backend_modules():
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIPPED_DIRS and not name.startswith(".")
        )
        for name in sorted(files):
            if name.endswith(".py"):
                path = Path(directory) / name
                yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _calls_named(node, name):
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called == name:
                return True
    return False


def _string(node, value):
    return isinstance(node, ast.Constant) and node.value == value


def census(source, filename):
    """Return (sites, violations) for one module's source text."""
    tree = ast.parse(source, filename=filename)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing_functions(node):
        found = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(node)
        return found

    sites, violations = [], []

    def check(node, kind, required):
        where = f"{filename}:{node.lineno} {kind}"
        sites.append(where)
        functions = enclosing_functions(node)
        if not functions or not _calls_named(functions[0], required):
            violations.append(f"{where} (enclosing function does not call {required})")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called == "SourceCut":
                where = f"{filename}:{node.lineno} SourceCut()"
                sites.append(where)
                names = [function.name for function in enclosing_functions(node)]
                if filename != PLANNER_MODULE or "plan_source_cuts" not in names:
                    violations.append(f"{where} (SourceCut built outside plan_source_cuts)")
            for keyword in node.keywords:
                if keyword.arg == "sourceCuts":
                    check(node, "sourceCuts= keyword", "plan_source_cuts")
                if keyword.arg == "clip_start_ms":
                    check(node, "cut executor call", "source_cut_is_planned")
        elif isinstance(node, ast.Dict):
            keys = list(node.keys)
            if any(_string(key, "sourceCuts") for key in keys):
                check(node, '"sourceCuts" dict', "plan_source_cuts")
            elif any(
                _string(key, "sourceKind") and _string(value, "dossier_source_dna")
                for key, value in zip(keys, node.values)
            ):
                check(node, "dossier_source_dna job dict", "plan_source_cuts")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _string(target.slice, "sourceCuts"):
                    check(node, '["sourceCuts"] assignment', "plan_source_cuts")
    return sites, violations


def test_every_source_cut_path_goes_through_the_planner():
    sites, violations = [], []
    for filename, source in _backend_modules():
        found, bad = census(source, filename)
        sites.extend(found)
        violations.extend(bad)
    assert violations == []
    # The census must see the real paths, or it would pass by finding nothing.
    assert any(site.startswith(f"{PLANNER_MODULE}:") and "SourceCut()" in site for site in sites)
    assert any(site.startswith("routers/control_plane.py:") and '"sourceCuts" dict' in site for site in sites)
    assert any(site.startswith("routers/control_plane.py:") and "cut executor call" in site for site in sites)


def test_the_census_catches_a_caller_that_bypasses_the_planner():
    bypass = '''
def refill(store, master):
    job = {
        "sourceKind": "dossier_source_dna",
        "sourceCuts": [{"masterSha256": master, "startMs": 0, "durationMs": 7000}],
    }
    store["jobs"]["x"] = job

def rewrite(job):
    job["sourceCuts"] = [{"startMs": 0, "durationMs": 7000}]

def handmade(master):
    return SourceCut(master, 0, 7000, True)

async def render(cut):
    await run_color_correct("in", "out", {}, clip_start_ms=cut["startMs"])
'''
    _, violations = census(bypass, "services/fake_refill.py")
    assert len(violations) == 4, violations
    assert any("sourceCuts\" dict" in v for v in violations)
    assert any("assignment" in v for v in violations)
    assert any("SourceCut built outside" in v for v in violations)
    assert any("cut executor call" in v for v in violations)


def test_the_census_accepts_a_caller_that_plans_first():
    planned = '''
def refill(recipe, served):
    cuts = plan_source_cuts(recipe, 1, served)
    return {"sourceKind": "dossier_source_dna", "sourceCuts": [c.slot_id for c in cuts]}
'''
    sites, violations = census(planned, "services/fake_refill.py")
    assert sites and violations == []
