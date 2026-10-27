"""Dependency-free call-site guard for the timed renderer's MED-1 wiring.

``services/post_render_timed.py`` cannot be imported on the reduced review host
(it needs PIL and pydantic on one interpreter, which never coexist here). This
test therefore reads its SOURCE and parses it with ``ast`` — no imports, no
image stack — and asserts the three duration-derived delivery quantities really
route through ``services.timed_render_math`` at the production call sites, and
that no compiled-in ``TARGET_DURATION_MS`` constant remains.

Re-hard-coding the 20s/600 shape at those call sites (re-adding
``TARGET_DURATION_MS = 20_000`` and inlining ``/1000``, the 0.90 bitrate budget,
and ``*30//1000``) makes every assertion here go RED on the reduced host.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "services" / "post_render_timed.py"


def _tree() -> ast.Module:
    return ast.parse(SERVICE.read_text(encoding="utf-8"))


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {SERVICE}")


def _call(node: ast.AST, func_name: str) -> ast.Call:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == func_name:
            return child
    raise AssertionError(f"{func_name}(...) call not found in {SERVICE}")


def _name_args(call: ast.Call) -> list[str]:
    return [arg.id for arg in call.args if isinstance(arg, ast.Name)]


def _subscript_string_key(node: ast.Subscript) -> str | None:
    """String key of a ``timing["durationMs"]``-style subscript, or ``None``.

    Python 3.9+ parses the slice as a bare ``ast.Constant``; older Pythons wrap
    it in ``ast.Index``, so unwrap both shapes.
    """
    slice_ = node.slice
    if isinstance(slice_, ast.Index):  # pragma: no cover - py<3.9 only
        slice_ = slice_.value
    if isinstance(slice_, ast.Constant) and isinstance(slice_.value, str):
        return slice_.value
    return None


def test_no_compiled_in_target_duration_ms_name():
    names = {node.id for node in ast.walk(_tree())
             if isinstance(node, ast.Name) and node.id == "TARGET_DURATION_MS"}
    assert not names, (
        f"compiled-in TARGET_DURATION_MS remains in {SERVICE}; duration must come "
        "from the timing manifest, not a page constant"
    )


def test_duration_helpers_are_imported_from_timed_render_math():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module == "services.timed_render_math":
            imported = {alias.name for alias in node.names}
            assert {"expected_decoded_frames", "phrase_input_seconds", "retry_bitrate_bps"} <= imported, (
                f"services.timed_render_math import in {SERVICE} is missing a helper: {imported}"
            )
            return
    raise AssertionError(f"no import from services.timed_render_math in {SERVICE}")


def test_t_site_uses_phrase_input_seconds():
    call = _call(_function("_encode_timed_final"), "phrase_input_seconds")
    assert _name_args(call) == ["duration_ms"], (
        f"the -t call site in {SERVICE} must pass duration_ms to phrase_input_seconds"
    )


def test_bitrate_site_uses_retry_bitrate_bps():
    call = _call(_function("_encode_timed_final"), "retry_bitrate_bps")
    assert _name_args(call) == ["MAX_FINAL_BYTES", "duration_ms"], (
        f"the retry bitrate call site in {SERVICE} must pass MAX_FINAL_BYTES, duration_ms "
        "to retry_bitrate_bps"
    )


def test_frames_site_uses_expected_decoded_frames():
    call = _call(_function("render_timed_post"), "expected_decoded_frames")
    assert _name_args(call) == ["target_duration_ms"], (
        f"the decoded-frames call site in {SERVICE} must pass target_duration_ms "
        "to expected_decoded_frames"
    )


def test_target_duration_ms_is_read_from_timing_manifest():
    """The duration SOURCE is the per-page timing manifest, not a re-hard-coded constant.

    This is the guard for ``post_render_timed.py``'s ``target_duration_ms =
    timing["durationMs"]`` line itself: replacing it with a literal (e.g.
    ``target_duration_ms = 20_000``) leaves every call-site assertion above
    green, so the source must be pinned here too.

    It checks EVERY assignment to ``target_duration_ms`` inside
    ``render_timed_post`` (a full AST walk, not a first-match scan): exactly one
    must exist, and it must be the ``timing["durationMs"]`` subscript. An
    additive shadow re-hard-code — a second ``target_duration_ms = 20_000``
    later in the function — therefore fails the "exactly one" assertion, and a
    replacement literal fails the subscript assertion.
    """
    fn = _function("render_timed_post")
    assignments = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "target_duration_ms" for t in node.targets)
    ]
    assert assignments, (
        f"target_duration_ms assignment not found in render_timed_post in {SERVICE}"
    )
    assert len(assignments) == 1, (
        f"render_timed_post in {SERVICE} must have exactly one target_duration_ms "
        f"assignment, found {len(assignments)}: "
        + "; ".join(ast.dump(a.value) for a in assignments)
    )
    value = assignments[0].value
    assert isinstance(value, ast.Subscript), (
        f"target_duration_ms in {SERVICE} must be read from the timing manifest, "
        f"not re-hard-coded; got {ast.dump(value)}"
    )
    assert isinstance(value.value, ast.Name) and value.value.id == "timing", (
        f"target_duration_ms in {SERVICE} must read the `timing` manifest, "
        f"got {ast.dump(value)}"
    )
    assert _subscript_string_key(value) == "durationMs", (
        f"target_duration_ms in {SERVICE} must read timing[\"durationMs\"], "
        f"got {ast.dump(value)}"
    )


def _int_constants(node: ast.AST) -> list[int]:
    """Every integer literal under ``node`` (underscores are already stripped)."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, int)]


def _abs_duration_checks() -> list[ast.Call]:
    """Every ``abs(<probe>.duration_ms - <operand>)`` duration check in the module."""
    sites = []
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "abs" and len(node.args) == 1):
            continue
        arg = node.args[0]
        if (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Sub)
                and isinstance(arg.left, ast.Attribute) and arg.left.attr == "duration_ms"):
            sites.append(node)
    return sites


def test_no_compiled_in_20s_duration_literal():
    # MED-1: the reviewer's mutant re-hard-coded the three duration compares back
    # to ``20_000`` and every runnable test stayed green. Duration is page data,
    # so no ``20_000`` integer may appear anywhere in the renderer.
    assert 20_000 not in _int_constants(_tree()), (
        f"a compiled-in 20_000 duration literal remains in {SERVICE}; duration "
        "must come from the timing manifest, not a page constant"
    )


def test_duration_checks_compare_against_data_not_literal():
    # MED-1: pin the three probe-duration checks (encode acceptance, source
    # admission, final admission) to compare against ``duration_ms`` /
    # ``target_duration_ms``, never a literal such as 20_000.
    sites = _abs_duration_checks()
    assert len(sites) == 3, (
        f"expected exactly 3 probe-duration checks in {SERVICE}, found {len(sites)}: "
        + "; ".join(ast.dump(site) for site in sites)
    )
    for site in sites:
        right = site.args[0].right
        assert isinstance(right, ast.Name) and right.id in {"duration_ms", "target_duration_ms"}, (
            f"a duration check in {SERVICE} compares against a non-data operand; "
            f"expected duration_ms/target_duration_ms, got {ast.dump(right)}"
        )


def test_phrase_cap_is_bounded_by_timing_constants_not_literal_seven():
    # MED-1: the reviewer's mutant also reverted ``_validate_phrases`` to
    # ``<= 7``. With MAX_TIMING_PHRASES == 7 that is behaviorally identical, so
    # only a source-shape guard can catch it: the cap must route through the
    # lyric_timing bounds constants, and no literal ``7`` may be hard-coded.
    fn = _function("_validate_phrases")
    names = {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}
    assert {"MIN_TIMING_PHRASES", "MAX_TIMING_PHRASES"} <= names, (
        f"_validate_phrases in {SERVICE} must bound the phrase count with "
        "MIN_TIMING_PHRASES/MAX_TIMING_PHRASES, not a literal"
    )
    assert 7 not in _int_constants(fn), (
        f"_validate_phrases in {SERVICE} must not hard-code the phrase cap as 7"
    )
