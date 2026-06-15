import json
import subprocess
import sys
from pathlib import Path

from tools.ralph_loop import (
    evaluate_stop_hooks,
    init_loop,
    record_pass,
    render_linear_comment,
    run_dry_run,
    wipe_context,
)


def _repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "content-posting-lab"
    root.mkdir()
    return root


def _clean_pass(root: Path, issue_id: str, pass_number: int):
    record_pass(
        root,
        issue_id,
        pass_number=pass_number,
        remarks=[f"pass {pass_number} reviewed the artifact"],
        changes=[f"pass {pass_number} changes recorded"],
        verification=[
            {
                "name": f"pass {pass_number} tests",
                "status": "passed",
                "evidence": f"pytest fixture pass {pass_number}",
            }
        ],
        unresolved_risks=[],
        next_pass_prompt=(
            f"Use pass {pass_number} remarks as input"
            if pass_number < 3
            else "Final pass complete"
        ),
    )
    wipe_context(root, issue_id, pass_number=pass_number)


def test_stop_hooks_block_until_three_verified_wiped_passes(tmp_path):
    root = _repo_root(tmp_path)
    issue_id = "CONT-18"
    init_loop(root, issue_id, branch="cont-18-editor-bay-loop-infra")

    report = evaluate_stop_hooks(root, issue_id)
    assert report.blocked is True
    assert "missing pass 1 artifact" in report.reasons

    record_pass(
        root,
        issue_id,
        pass_number=1,
        remarks=["first pass found missing docs"],
        changes=["added draft docs"],
        verification=[{"name": "unit tests", "status": "passed", "evidence": "pytest"}],
        unresolved_risks=[],
        next_pass_prompt="Re-read pass 1 remarks after context wipe",
    )
    report = evaluate_stop_hooks(root, issue_id)
    assert report.blocked is True
    assert "missing context wipe marker for pass 1" in report.reasons

    wipe_context(root, issue_id, pass_number=1)
    record_pass(
        root,
        issue_id,
        pass_number=2,
        remarks=["second pass found unresolved verification"],
        changes=["tightened stop-hook check"],
        verification=[{"name": "dry run", "status": "passed", "evidence": "fixture"}],
        unresolved_risks=["Linear comment body not generated"],
        next_pass_prompt="Resolve pass 2 risks after context wipe",
    )
    wipe_context(root, issue_id, pass_number=2)
    _clean_pass(root, issue_id, 3)

    report = evaluate_stop_hooks(root, issue_id)
    assert report.blocked is True
    assert "pass 2 has unresolved risks" in report.reasons

    _clean_pass(root, issue_id, 2)
    report = evaluate_stop_hooks(root, issue_id)
    assert report.blocked is False
    assert report.reasons == []


def test_stop_hooks_block_wrong_repo_boundary_even_with_clean_passes(tmp_path):
    root = tmp_path / "wrong-repo"
    root.mkdir()
    issue_id = "CONT-18"
    init_loop(root, issue_id, branch="cont-18-editor-bay-loop-infra")
    for pass_number in range(1, 4):
        _clean_pass(root, issue_id, pass_number)

    report = evaluate_stop_hooks(root, issue_id)

    assert report.blocked is True
    assert "repo boundary mismatch: expected content-posting-lab" in report.reasons


def test_dry_run_writes_three_passes_wipes_and_linear_summary(tmp_path):
    root = _repo_root(tmp_path)
    issue_id = "CONT-18-fixture"

    report = run_dry_run(root, issue_id)

    assert report.blocked is False
    loop_dir = root / ".codex" / "ralph-loop" / issue_id
    manifest = json.loads((loop_dir / "manifest.json").read_text())
    assert manifest["issue_id"] == issue_id
    for pass_number in range(1, 4):
        assert (loop_dir / f"pass-{pass_number}.json").exists()
        assert (loop_dir / f"context-wipe-pass-{pass_number}.marker").exists()
    assert (loop_dir / "gate-report.json").exists()

    comment = render_linear_comment(root, issue_id)
    assert issue_id in comment
    assert "Pass 1" in comment
    assert "Verification" in comment
    assert "Stop-hook status: clear" in comment


def test_dry_run_cli_can_be_invoked_from_repo(tmp_path):
    root = _repo_root(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    script = repo / "tools" / "ralph_loop.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "dry-run",
            "--repo-root",
            str(root),
            "--issue",
            "CONT-18-cli-fixture",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Stop-hook status: clear" in result.stdout


def test_context_wipe_bash_wrapper_removes_scratch_and_marks_pass(tmp_path):
    root = _repo_root(tmp_path)
    issue_id = "CONT-18-wipe"
    init_loop(root, issue_id, branch="cont-18-editor-bay-loop-infra")
    scratch = root / ".codex" / "ralph-loop" / issue_id / "scratch" / "pass-1"
    scratch.mkdir(parents=True)
    (scratch / "stale-context.md").write_text("discard this before the next pass")

    repo = Path(__file__).resolve().parents[1]
    script = repo / "tools" / "ralph_loop_context_wipe.sh"
    result = subprocess.run(
        ["bash", str(script), issue_id, "1", str(root)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not scratch.exists()
    assert (
        root
        / ".codex"
        / "ralph-loop"
        / issue_id
        / "context-wipe-pass-1.marker"
    ).exists()
