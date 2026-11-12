"""The mutation harness must reject proofs caused by its own broken plumbing."""
import xml.etree.ElementTree as ET

import pytest
from tools import verify_scrambler_reader_mutants as tool


def _report(tmp_path, *, message=None, error=None, skip=False, count=1):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests=str(count))
    for index in range(count):
        case = ET.SubElement(suite, "testcase", name=f"real-case-{index}")
        if message is not None:
            ET.SubElement(case, "failure", message=message)
        if error is not None:
            ET.SubElement(case, "error", message=error)
        if skip:
            ET.SubElement(case, "skipped", message="not exercised")
    path = tmp_path / "proof.xml"
    ET.ElementTree(root).write(path)
    return path


def test_inventory_mutates_current_source_and_preserves_compileability():
    source = (tool.ROOT / "services/scrambler_originality.py").read_text()
    cases = tool.mutation_cases()
    prepared = tool.prepare_mutants(source, cases)
    assert len(prepared) == len(cases) > 0
    required = {"prior-show-bypass", "age-expiry-30d", "age-expiry-90d",
                "wildcard-prior", "unknown-origin", "unknown-origin-window",
                "mapped-bounds", "finite-query-clock", "finite-loader-clock"}
    assert required <= {case.name for case in cases}
    for case, mutant in prepared:
        assert mutant != source
        compile(mutant, case.name, "exec")


@pytest.mark.parametrize("source", ["missing", "x = 1\nx = 1\n"])
def test_missing_or_ambiguous_needles_refuse_before_any_execution(source):
    with pytest.raises(tool.MutationDefinitionError, match="mutation needles"):
        tool.Mutation("bad", "x = 1", "x = 2", "selected").apply(source)


def test_noncompiling_mutation_is_not_a_red_proof():
    with pytest.raises(tool.MutationDefinitionError, match="does not compile"):
        tool.Mutation("bad", "x = 1", "if:", "selected").apply("x = 1")


def test_empty_or_duplicate_inventory_refuses():
    with pytest.raises(tool.MutationDefinitionError):
        tool.prepare_mutants("x = 1", ())
    item = tool.Mutation("same", "x = 1", "x = 2", "selected")
    with pytest.raises(tool.MutationDefinitionError):
        tool.prepare_mutants("x = 1", (item, item))


def test_successful_exit_with_no_selected_tests_does_not_pass_baseline(tmp_path):
    stats = tool.junit_stats(_report(tmp_path, count=0))
    assert not tool.baseline_passed(0, stats)
    assert not tool.behaviorally_red(1, stats)


def test_actual_selected_clean_cases_pass_baseline_but_never_red_proof(tmp_path):
    stats = tool.junit_stats(_report(tmp_path, count=2))
    assert stats["tests"] == 2
    assert tool.baseline_passed(0, stats)
    assert not tool.behaviorally_red(0, stats)
    assert not tool.behaviorally_red(1, stats)


@pytest.mark.parametrize("message", ["assert not True", "AssertionError: origin reused",
                                     "Failed: DID NOT RAISE <class 'RuntimeError'>",
                                     "Failed: behavioral assertion: valid canonical milliseconds were refused"])
def test_behavioral_assertion_failures_count_only_with_exit_one(tmp_path, message):
    stats = tool.junit_stats(_report(tmp_path, message=message))
    assert tool.behaviorally_red(1, stats)
    assert not tool.behaviorally_red(0, stats)
    assert not tool.behaviorally_red(2, stats)
    assert not tool.baseline_passed(0, stats)


@pytest.mark.parametrize("message", ["ImportError: missing module", "NameError: removed constant",
                                     "TypeError: broken mutation", "SyntaxError: bad body",
                                     "Failed: Timeout (>1.0s) from pytest-timeout.",
                                     "Failed: external subprocess did not finish"])
def test_runtime_or_import_failure_never_masquerades_as_behavioral_red(tmp_path, message):
    stats = tool.junit_stats(_report(tmp_path, message=message))
    assert not tool.behaviorally_red(1, stats)


def test_fixture_errors_or_skips_invalidate_otherwise_behavioral_red(tmp_path):
    assert not tool.behaviorally_red(1, tool.junit_stats(
        _report(tmp_path, message="assert not True", error="fixture error")))
    assert not tool.behaviorally_red(1, tool.junit_stats(
        _report(tmp_path, message="assert not True", skip=True)))


def test_missing_invalid_or_inconsistent_junit_cannot_certify_a_proof(tmp_path):
    path = tmp_path / "missing.xml"
    assert not tool.behaviorally_red(1, tool.junit_stats(path))
    path.write_text("not xml")
    assert not tool.behaviorally_red(1, tool.junit_stats(path))
    path.write_text('<testsuite tests="2"><testcase name="one"/></testsuite>')
    assert not tool.baseline_passed(0, tool.junit_stats(path))


def test_a_mixed_assertion_and_runtime_failure_does_not_certify_a_mutant(tmp_path):
    path = _report(tmp_path, message="assert not True", count=2)
    tree = ET.parse(path)
    tree.findall(".//testcase")[1].find("failure").set("message", "OverflowError: invalid clock")
    tree.write(path)
    stats = tool.junit_stats(path)
    assert stats["failures"] == 2 and stats["assertion_failures"] == 1
    assert not tool.behaviorally_red(1, stats)


def test_existing_evidence_directory_is_never_reused(tmp_path):
    marker = tmp_path / "older-result.xml"
    marker.write_text("retained proof")
    with pytest.raises(SystemExit) as error:
        tool.main(["--output", str(tmp_path)])
    assert error.value.code == 2
    assert marker.read_text() == "retained proof"


def test_main_missing_guard_refuses_before_running_any_tests(tmp_path, monkeypatch):
    def forbidden_execution(*args, **kwargs):
        raise AssertionError("no test process may run after missing mutation preflight")

    monkeypatch.setattr(tool, "run_case", forbidden_execution)
    monkeypatch.setattr(tool, "mutation_cases", lambda: (
        tool.Mutation("missing", "NOT-A-REAL-CURRENT-GUARD", "replacement", "selected"),))
    assert tool.main(["--output", str(tmp_path / "new-proof")]) == 2
