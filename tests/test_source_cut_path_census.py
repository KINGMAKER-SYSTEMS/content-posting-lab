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
site's enclosing function calls plan_source_cuts. The sole exception is the
source runner atomically narrowing a completed job to validated cuts that made
manifested outputs. That runner is ``_run_dossier_source``, or the Scrambler's
``_run_dossier_source_inner`` when its only call sites are direct calls inside
``_run_dossier_source`` (the page-lock wrapper); there its pinned guard may
also pass ``scrambler=scram_record`` and nothing else. An executor site's enclosing function re-derives each cut
with source_cut_is_planned (the planner's vocabulary check) before rendering it.
"""

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {"tests", "frontend", "node_modules", "__pycache__", "artifacts", "static"}
PLANNER_MODULE = "services/control_plane_sources.py"
RUNNER_MODULE = "routers/control_plane.py"
# The Scrambler (#281) split the source runner: the page-lock wrapper keeps the
# name _run_dossier_source and the cut loop moved into this one function.
SCRAMBLER_RUNNER = "_run_dossier_source_inner"
SCRAMBLER_WRAPPER = "_run_dossier_source"


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

    def refers_to_scrambler_runner(node):
        return ((isinstance(node, ast.Name) and node.id == SCRAMBLER_RUNNER)
                or (isinstance(node, ast.Attribute) and node.attr == SCRAMBLER_RUNNER)
                or (isinstance(node, ast.alias) and SCRAMBLER_RUNNER in (node.name, node.asname))
                or _string(node, SCRAMBLER_RUNNER))

    def scrambler_runner_misuses():
        # The inner runner may be named only by direct calls inside the
        # module-level wrapper of the runner module: no second caller, no
        # alias, no callback, no import elsewhere.
        top = {node.name: node for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        wrapper = top.get(SCRAMBLER_WRAPPER) if filename == RUNNER_MODULE else None
        bad = []
        for node in ast.walk(tree):
            if not refers_to_scrambler_runner(node):
                continue
            parent = parents.get(node)
            functions = enclosing_functions(node)
            if not (wrapper is not None and isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and isinstance(parent, ast.Call) and parent.func is node
                    and functions and functions[0] is wrapper):
                bad.append(f"{filename}:{getattr(node, 'lineno', getattr(parent, 'lineno', 0))} "
                           f"{SCRAMBLER_RUNNER} referenced outside {SCRAMBLER_WRAPPER}")
        return bad

    def scrambler_runner_is_wrapped(fn):
        if filename != RUNNER_MODULE or fn.name != SCRAMBLER_RUNNER or parents.get(fn) is not tree:
            return False
        defined = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and node.name in {SCRAMBLER_RUNNER, SCRAMBLER_WRAPPER}]
        wrappers = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == SCRAMBLER_WRAPPER]
        if len(defined) != 2 or len(wrappers) != 1:
            return False
        calls = [node for node in ast.walk(wrappers[0]) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == SCRAMBLER_RUNNER]
        return bool(calls) and not scrambler_runner_misuses()

    def delivered_subset_completion(node, functions):
        # One exception: a source runner may atomically replace the planned
        # list with only cuts that actually made a completed manifest. Keep
        # this structural check narrow; every other rewrite still needs the
        # planner. Runtime tests separately verify active and archived claims.
        if filename != "routers/control_plane.py" or not functions:
            return False
        fn = functions[0]
        scrambler_runner = scrambler_runner_is_wrapped(fn)
        if fn.name not in {"_run_dossier_source", "_run_owned_dossier_source"} and not scrambler_runner:
            return False
        threaded_update = (isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
                           and isinstance(node.func.value, ast.Name) and node.func.value.id == "asyncio"
                           and len(node.args) == 2 and isinstance(node.args[0], ast.Name)
                           and node.args[0].id in {"_update_job", "_update_source_job"}
                           and isinstance(node.args[1], ast.Name) and node.args[1].id == "job_id")
        drained_update = (isinstance(node.func, ast.Name) and node.func.id == "_settle_source_update"
                          and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                          and node.args[0].id == "job_id")
        if not (threaded_update or drained_update):
            return False
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if not (_string(keywords.get("status"), "completed")
                and isinstance(keywords.get("clips"), ast.Name) and keywords["clips"].id == "manifests"
                and isinstance(keywords.get("sourceCuts"), ast.Name) and keywords["sourceCuts"].id == "delivered_cuts"):
            return False
        names = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "delivered_cuts"]
        if len(names) != 3:  # empty initialization, one append, completed update
            return False
        initializers = [n for n in ast.walk(fn) if isinstance(n, ast.AnnAssign)
                        and isinstance(n.target, ast.Name) and n.target.id == "delivered_cuts"
                        and isinstance(n.value, ast.List) and not n.value.elts]
        loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)
                 and isinstance(n.target, ast.Tuple)
                 and [getattr(e, "id", None) for e in n.target.elts] == ["index", "source_cut"]
                 and isinstance(n.iter, ast.Call) and isinstance(n.iter.func, ast.Name)
                 and n.iter.func.id == "enumerate" and len(n.iter.args) == 1
                 and isinstance(n.iter.args[0], ast.Name) and n.iter.args[0].id == "source_cuts"]
        if len(initializers) != 1 or len(loops) != 1:
            return False
        loop = loops[0]
        def append_of(stmt, target, argument):
            return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == "append"
                    and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == target
                    and len(stmt.value.args) == 1 and isinstance(stmt.value.args[0], ast.Name)
                    and stmt.value.args[0].id == argument and not stmt.value.keywords)
        append_indexes = [i for i, stmt in enumerate(loop.body) if append_of(stmt, "delivered_cuts", "source_cut")]
        if len(append_indexes) != 1 or append_indexes[0] == 0:
            return False
        index = append_indexes[0]
        if not append_of(loop.body[index - 1], "manifests", "manifest"):
            return False
        # Pin the entire guard expression, not merely a decorative planner
        # call hidden inside a different boolean expression.
        expected = ast.parse(
            "master is None or start_ms != source_cut.get('libraryStartMs') or "
            "not source_cut_is_planned(recipe, master, start_ms, duration_ms, source_cut.get('slotId'))",
            mode="eval",
        ).body
        guards = [ast.dump(expected, include_attributes=False)]
        if scrambler_runner:
            # The Scrambler runner's one allowed difference: the same pinned
            # guard plus exactly ``scrambler=scram_record``, where scram_record
            # is only ever this cut's own stored record (or None).
            guards.append(ast.dump(ast.parse(
                "master is None or start_ms != source_cut.get('libraryStartMs') or "
                "not source_cut_is_planned(recipe, master, start_ms, duration_ms, "
                "source_cut.get('slotId'), scrambler=scram_record)",
                mode="eval",
            ).body, include_attributes=False))
            own_record = ast.dump(ast.parse("source_cut.get('scrambler')", mode="eval").body,
                                  include_attributes=False)
            stores = [n for n in ast.walk(fn) if isinstance(n, ast.Name)
                      and n.id == "scram_record" and not isinstance(n.ctx, ast.Load)]
            assigns = [n for n in loop.body if isinstance(n, ast.Assign)
                       and [getattr(t, "id", None) for t in n.targets] == ["scram_record"]]
            if (len(assigns) != 1 or ast.dump(assigns[0].value, include_attributes=False) != own_record
                    or any(not (isinstance(parents.get(n), ast.Assign)
                                and parents[n].targets == [n]
                                and (parents[n] is assigns[0] or _string(parents[n].value, None)))
                           for n in stores)):
                return False
        return any(isinstance(stmt, ast.If)
                   and ast.dump(stmt.test, include_attributes=False) in guards
                   and len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Raise)
                   for stmt in loop.body[:index])

    def check(node, kind, required):
        where = f"{filename}:{node.lineno} {kind}"
        sites.append(where)
        functions = enclosing_functions(node)
        if kind == "sourceCuts= keyword" and delivered_subset_completion(node, functions):
            return
        if not functions or not _calls_named(functions[0], required):
            violations.append(f"{where} (enclosing function does not call {required})")

    violations.extend(scrambler_runner_misuses())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called == "SourceCut":
                where = f"{filename}:{node.lineno} SourceCut()"
                sites.append(where)
                names = [function.name for function in enclosing_functions(node)]
                # The planner is plan_source_cuts plus its two implementations
                # (_plan_source_cuts_base / _plan_source_cuts_scrambled); a
                # SourceCut may be built nowhere else in any module.
                if filename != PLANNER_MODULE or not any(
                    "plan_source_cuts" in name for name in names
                ):
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


def test_completed_source_subset_is_the_only_safe_cut_rewrite():
    source = (ROOT / "routers/control_plane.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    runner = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name in {"_run_dossier_source", "_run_owned_dossier_source", SCRAMBLER_RUNNER}
                  and _calls_named(node, "source_cut_is_planned"))
    segment = ast.get_source_segment(source, runner)
    if runner.name == SCRAMBLER_RUNNER:
        # The inner runner is accepted only together with its sole caller.
        wrapper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == SCRAMBLER_WRAPPER)
        segment = ast.get_source_segment(source, wrapper) + "\n\n\n" + segment
    source = segment
    filename = "routers/control_plane.py"
    _, violations = census(source, filename)
    assert violations == []
    # The runtime-claim follow-up drains this same conditional update before
    # releasing its claim; it must pass the identical subset proof.
    threaded_call = "await asyncio.to_thread(_update_job,\n        job_id,"
    drained_call = "await _settle_source_update(\n        job_id,"
    if drained_call in source:
        drained = source
        alternate = source.replace(drained_call, threaded_call, 1)
    else:
        drained = source.replace(threaded_call, drained_call, 1)
        alternate = drained
    assert alternate != source
    assert census(alternate, filename)[1] == []
    assert census(drained, filename)[1] == []
    wrong_job = drained.replace("await _settle_source_update(\n        job_id,",
                                "await _settle_source_update(\n        other_job,", 1)
    assert wrong_job != drained
    assert any("sourceCuts= keyword" in item for item in census(wrong_job, filename)[1])
    mutations = [
        ('status="completed",\n        progress=100,\n        clips=manifests,\n        sourceCuts=delivered_cuts,',
         'status="running",\n        progress=100,\n        clips=manifests,\n        sourceCuts=delivered_cuts,'),
        (f'async def {runner.name}(', 'async def unrelated_writer('),
        ('sourceCuts=delivered_cuts,', 'sourceCuts=source_cuts,'),
        ('or not source_cut_is_planned(', 'or False and not source_cut_is_planned('),
        ('delivered_cuts.append(source_cut)', 'delivered_cuts.append({"slotId": "unplanned"})'),
        ('delivered_cuts.append(source_cut)', 'delivered_cuts.append(source_cut)\n            delivered_cuts.append({"slotId": "extra"})'),
    ]
    for old, new in mutations:
        assert old in source
        _, violations = census(source.replace(old, new, 1), filename)
        assert any('sourceCuts= keyword' in item for item in violations), (old, violations)


def _scrambler_runner_source():
    return (ROOT / RUNNER_MODULE).read_text(encoding="utf-8")


def _without_scrambler_guard(source):
    """Delete the inner runner's pinned planner guard (the whole if/raise)."""
    tree = ast.parse(source)
    runner = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == SCRAMBLER_RUNNER)
    guard = next(node for node in ast.walk(runner) if isinstance(node, ast.If)
                 and _calls_named(node.test, "source_cut_is_planned"))
    lines = source.splitlines(keepends=True)
    return "".join(lines[:guard.lineno - 1] + lines[guard.end_lineno:])


def test_scrambler_runner_exception_is_exactly_the_wrapped_runner():
    """#281's runner shape passes; each way of stretching it fails."""
    source = _scrambler_runner_source()
    tree = ast.parse(source)
    assert any(isinstance(node, ast.AsyncFunctionDef) and node.name == SCRAMBLER_RUNNER
               for node in tree.body), "the census must be checking the real runner"
    assert census(source, RUNNER_MODULE)[1] == []

    def red(mutated, filename=RUNNER_MODULE):
        assert mutated != source
        _, violations = census(mutated, filename)
        assert violations, "mutant passed the census"
        return violations

    # m1: no planner guard before the sourceCuts write.
    assert any("sourceCuts= keyword" in v for v in red(_without_scrambler_guard(source)))
    # m2: a second caller, in the same module or in another one.
    second = source + "\n\nasync def _second_caller(job_id):\n    await _run_dossier_source_inner(job_id)\n"
    violations = red(second)
    assert any("referenced outside" in v for v in violations)
    assert any("sourceCuts= keyword" in v for v in violations)
    assert any("referenced outside" in v for v in red(
        "from routers.control_plane import _run_dossier_source_inner\n\n"
        "async def go(job_id):\n    await _run_dossier_source_inner(job_id)\n",
        "services/other_runner.py"))
    callback = source.replace("return await _run_dossier_source_inner(job_id)",
                              "runner = _run_dossier_source_inner\n        return await runner(job_id)", 1)
    assert any("referenced outside" in v for v in red(callback))
    # m3: the same body under any other name.
    renamed = source.replace(SCRAMBLER_RUNNER, "_run_scrambler_cut_loop")
    assert any("sourceCuts= keyword" in v for v in red(renamed))
    # The guard may add scrambler=scram_record and nothing else.
    guard_call = "scrambler=scram_record,\n"
    assert source.count(guard_call) == 1
    for extra in ("scrambler=scram_record, strict=False,\n", "scrambler=None,\n",
                  "scrambler=source_cut,\n", "scram_record,\n"):
        assert any("sourceCuts= keyword" in v for v in red(source.replace(guard_call, extra, 1)))
    # scram_record must be this cut's own stored record.
    own = 'scram_record = source_cut.get("scrambler")'
    assert source.count(own) == 1
    assert any("sourceCuts= keyword" in v for v in red(
        source.replace(own, 'scram_record = job.get("scrambler")', 1)))
    # The scrambler= form is not accepted for the wrapper's own name.
    plain = source.replace("async def _run_dossier_source(job_id", "async def _unused_wrapper(job_id", 1)
    plain = plain.replace(SCRAMBLER_RUNNER, SCRAMBLER_WRAPPER)
    assert any("sourceCuts= keyword" in v for v in red(plain))
