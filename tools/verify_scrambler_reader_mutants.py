"""Replay compiling unused-footage reader mutants against behavioral tests.

Run remotely through suite-slot. Baseline and mutant JUnit reports must contain
actual selected test cases. A mutant counts only assertion failures: syntax,
import, fixture, runtime and zero-selection failures never count as red proof.
All mutation needles are validated before execution; application files are never
modified. Keep each evidence directory as an immutable source-hash/log/diff set.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TEST_FILES = (
    "test_scrambler_originality_contract_independent.py",
    "test_scrambler_originality_boundaries.py",
    "test_scrambler_posted_asset_evidence.py",
    "test_scrambler_reader_review_regressions.py",
    "test_scrambler_reader_mutant_contract.py",
)
RUN_TIMEOUT_S = 90


class MutationDefinitionError(ValueError):
    """A missing/ambiguous needle or invalid mutant invalidates the proof."""


@dataclass(frozen=True)
class Mutation:
    name: str
    needle: str
    replacement: str
    selector: str
    occurrences: int = 1

    def apply(self, source: str) -> str:
        count = source.count(self.needle)
        if count != self.occurrences:
            raise MutationDefinitionError(
                f"{self.name}: expected {self.occurrences} mutation needles, found {count}")
        mutant = source.replace(self.needle, self.replacement)
        if mutant == source:
            raise MutationDefinitionError(f"{self.name}: mutation changes nothing")
        try:
            compile(mutant, self.name + ".py", "exec")
        except SyntaxError as error:
            raise MutationDefinitionError(f"{self.name}: mutant does not compile") from error
        return mutant


def mutation_cases() -> tuple[Mutation, ...]:
    return (
        Mutation("prior-show-bypass", "if self.shows.get((*origin_key, second)):",
                 "if False:", "reader_contract_prior_show"),
        Mutation("age-expiry-30d", '    kind = row.get("origin_kind")',
                 '    if now_epoch - posted >= 30 * 86400:\n        return None\n    kind = row.get("origin_kind")',
                 "reader_contract_prior_show"),
        Mutation("age-expiry-90d", '    kind = row.get("origin_kind")',
                 '    if now_epoch - posted >= 90 * 86400:\n        return None\n    kind = row.get("origin_kind")',
                 "reader_contract_prior_show"),
        Mutation("retired-alias", "if (mapping.origin_kind, mapping.origin_id) in self.retired_origins:",
                 "if False:", "retired_origin_alias"),
        Mutation("case-sensitive-origin", "_OriginWindow(origin_kind=kind, origin_id=origin_id,",
                 "_OriginWindow(origin_kind=kind, origin_id=origin_id.lower(),", "case_sensitive_origin_identity"),
        Mutation("wildcard-prior", "if self.wildcard_shows.get(origin_key):",
                 "if False:", "reader_contract_wildcard"),
        Mutation("partial-origin-second", "last = int(math.ceil(end_s)) - 1",
                 "last = int(math.floor(max(start_s, end_s - 0.001)))", "partial_millisecond"),
        Mutation("budget-per-second", "entries += span\n        if entries > USAGE_MAX_INDEX_ENTRIES:",
                 "entries += span\n        if False:", "index_entry_budget_counts"),
        Mutation("budget-wildcard", "# Wildcard show: one stored entry, no per-second expansion.\n            entries += 1\n            if entries > USAGE_MAX_INDEX_ENTRIES:",
                 "# Wildcard show: one stored entry, no per-second expansion.\n            entries += 1\n            if False:",
                 "reader_contract_wildcard_entry_budget_is_enforced_without_asset_metadata"),
        Mutation("upstream-offset", "origin_start = int(offset_ms) + start_ms",
                 "origin_start = start_ms", "canonical_origin_window_applies_upstream_offset"),
        Mutation("mapped-bounds", "if start_ms < Decimal(str(mapping.window[0])) * 1000 or end_ms > Decimal(str(mapping.window[1])) * 1000:",
                 "if False:", "reader_contract_mapped_bounds"),
        Mutation("unknown-origin", 'return False, f"origin_mapping_unknown:{master[:12]}"',
                 'return True, f"origin_mapping_unknown:{master[:12]}"', "reader_contract_unknown_origin"),
        Mutation("unknown-origin-window", 'return False, f"origin_window_unknown:{mapping.origin_kind}:{mapping.origin_id}"',
                 'return True, f"origin_window_unknown:{mapping.origin_kind}:{mapping.origin_id}"',
                 "reader_contract_unknown_window"),
        Mutation("sha-hex-validity", 'if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):',
                 "if len(text) != 64:", "nonhex_retirement"),
        Mutation("page-filter-bypass", "if self.shows.get((*origin_key, second)):",
                 "if any(show.page == page_id for show in self.shows.get((*origin_key, second), ())):",
                 "reader_contract_prior_show"),
        Mutation("decimal-milliseconds", "offset_ms = Decimal(str(mapping.window[0])) * 1000",
                 "offset_ms = mapping.window[0] * 1000.0", "decimal_millisecond_mapping"),
        Mutation("finite-query-clock", 'if _finite_number(now) is None:\n            return False, "clock_invalid"',
                 'if False:\n            return False, "clock_invalid"', "reader_contract_invalid_query_clock"),
        Mutation("finite-loader-clock", 'if _finite_number(now) is None:\n        raise ScramblerOriginalityUnavailable("usage_clock_invalid")',
                 'if False:\n        raise ScramblerOriginalityUnavailable("usage_clock_invalid")',
                 "reader_contract_invalid_loader_clock and nan"),
        Mutation("normalized-master-collision", "if master in seen_masters:",
                 "if False:", "conflicting_normalized"),
        Mutation("posted-full-coverage", "covered = record.first_second <= first and record.last_second >= last",
                 "covered = True", "partial_coverage_refuses"),
        Mutation("posted-asset-binding", "self.posted_assets.get((asset, origin_kind, origin_id), ())",
                 "next(iter(self.posted_assets.values()), ())", "wrong_or_unknown_asset or origin_identity_matches_case_sensitively"),
        Mutation("posted-latest", "record.posted_epoch >= best.posted_epoch",
                 "record.posted_epoch <= best.posted_epoch", "latest_of_multiple_covering"),
        Mutation("budget-posted-metadata", "# query, so it never releases a hold and never bricks the feed.\n            entries += 1\n            if entries > USAGE_MAX_INDEX_ENTRIES:",
                 "# query, so it never releases a hold and never bricks the feed.\n            entries += 1\n            if False:",
                 "reader_contract_posted_metadata_entry_budget_is_checked_before_allocation"),
    )


def prepare_mutants(source: str, cases: tuple[Mutation, ...]) -> list[tuple[Mutation, str]]:
    if not cases or len({case.name for case in cases}) != len(cases):
        raise MutationDefinitionError("mutation inventory must be nonempty with unique names")
    prepared = [(case, case.apply(source)) for case in cases]
    seen_bodies: dict[str, str] = {}
    for case, body in prepared:
        if body in seen_bodies:
            raise MutationDefinitionError(
                f"{case.name}: duplicate mutated body also produced by {seen_bodies[body]}")
        seen_bodies[body] = case.name
    return prepared


def junit_stats(path: Path) -> dict:
    """Count real testcase elements; never infer selection from exit status."""
    try:
        root = ET.parse(path).getroot()
        cases = list(root.iter("testcase"))
        if not cases:
            return {"valid": False, "reason": "zero_selected_tests"}
        for suite in root.iter("testsuite"):
            if int(suite.attrib["tests"]) != len(list(suite.iter("testcase"))):
                return {"valid": False, "reason": "inconsistent_junit_count"}
        failures = [failure for case in cases for failure in case.findall("failure")]
        messages = [failure.get("message", "").strip() for failure in failures]
        return {"valid": True, "tests": len(cases), "failures": len(failures),
                "errors": sum(len(case.findall("error")) for case in cases),
                "skipped": sum(len(case.findall("skipped")) for case in cases),
                "assertion_failures": sum(bool(re.match(
                    r"^(?:AssertionError\b|assert\b|Failed: DID NOT RAISE\b|Failed: behavioral assertion:)", message))
                    for message in messages)}
    except (OSError, ET.ParseError, KeyError, ValueError):
        return {"valid": False, "reason": "missing_or_invalid_junit"}


def baseline_passed(exit_code: int, stats: dict) -> bool:
    return bool(exit_code == 0 and stats.get("valid") and stats["tests"] > 0
                and stats["failures"] == stats["errors"] == stats["skipped"] == 0)


def behaviorally_red(exit_code: int, stats: dict) -> bool:
    return bool(exit_code == 1 and stats.get("valid") and stats["tests"] > 0
                and stats["errors"] == stats["skipped"] == 0
                and stats["failures"] > 0 and stats["assertion_failures"] == stats["failures"])


def write_bootstrap(path: Path) -> None:
    path.write_text('''import importlib.util, sys, pytest
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
import services
spec = importlib.util.spec_from_file_location("services.scrambler_originality", sys.argv[2])
module = importlib.util.module_from_spec(spec)
# Preserve runtime resource resolution while executing the copied mutant body.
module.__file__ = str(root / "services/scrambler_originality.py")
sys.modules[spec.name] = module; services.scrambler_originality = module
spec.loader.exec_module(module)
raise SystemExit(pytest.main(sys.argv[3:]))
''')


def run_case(out: Path, name: str, body: str, selector: str | None = None) -> dict:
    source_path = out / (name + ".py")
    source_path.write_text(body)
    report = out / (name + ".xml")
    command = [sys.executable, str(out / "bootstrap.py"), str(ROOT), str(source_path),
               *[str(ROOT / "tests" / name) for name in TEST_FILES],
               "-q", "--tb=short", "--junitxml=" + str(report)]
    if selector:
        command += ["-k", selector]
    try:
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=RUN_TIMEOUT_S)
        output, code = result.stdout, result.returncode
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        code = 124
    (out / (name + ".log")).write_text(output)
    return {"case": name, "exit": code, "junit": junit_stats(report)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    raw = args.output or os.environ.get("SCRAMBLER_MUTANT_OUT")
    out = Path(raw) if raw else Path(tempfile.mkdtemp(prefix="scrambler-reader-mutants-"))
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        parser.error("evidence directory must be empty; preserve earlier results")
    print("Evidence directory:", out, flush=True)
    source = (ROOT / "services/scrambler_originality.py").read_text()
    (out / "source.sha256").write_text(hashlib.sha256(source.encode()).hexdigest() + "\n")
    mutations = mutation_cases()
    try:
        bodies = prepare_mutants(source, mutations)
    except MutationDefinitionError as error:
        (out / "results.json").write_text(json.dumps({"preflight_error": str(error)}, indent=2) + "\n")
        print(error, file=sys.stderr)
        return 2
    write_bootstrap(out / "bootstrap.py")
    rows = []
    baseline = run_case(out, "base", source)
    baseline["passed"] = baseline_passed(baseline["exit"], baseline["junit"])
    rows.append(baseline)
    if baseline["passed"]:
        for case, body in bodies:
            (out / (case.name + ".diff")).write_text("".join(difflib.unified_diff(
                source.splitlines(True), body.splitlines(True),
                fromfile="services/scrambler_originality.py", tofile=case.name + ".py")))
            row = run_case(out, case.name, body, case.selector)
            row["behaviorally_red"] = behaviorally_red(row["exit"], row["junit"])
            rows.append(row)
            print(json.dumps(row), flush=True)
    (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    if not baseline["passed"]:
        print("Baseline failed; no mutant claims", file=sys.stderr)
        return 1
    return 0 if all(row.get("behaviorally_red") for row in rows[1:]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
