"""Ralph-loop handoff runner and stop-hook verifier.

This module keeps the Editor Bay v2 refinement loop mechanical:
three pass artifacts, context-wipe markers, verification evidence, and a
single stop-hook report that blocks completion when the handoff is incomplete.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REQUIRED_PASSES = 3
DEFAULT_EXPECTED_REPO_NAME = "content-posting-lab"


@dataclass
class GateReport:
    issue_id: str
    blocked: bool
    reasons: list[str]
    loop_dir: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "blocked": self.blocked,
            "reasons": self.reasons,
            "loop_dir": self.loop_dir,
            "checked_at": self.checked_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loop_dir(repo_root: Path, issue_id: str) -> Path:
    return repo_root / ".codex" / "ralph-loop" / issue_id


def _manifest_path(repo_root: Path, issue_id: str) -> Path:
    return _loop_dir(repo_root, issue_id) / "manifest.json"


def _pass_path(repo_root: Path, issue_id: str, pass_number: int) -> Path:
    return _loop_dir(repo_root, issue_id) / f"pass-{pass_number}.json"


def _wipe_marker_path(repo_root: Path, issue_id: str, pass_number: int) -> Path:
    return _loop_dir(repo_root, issue_id) / f"context-wipe-pass-{pass_number}.marker"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def init_loop(
    repo_root: Path | str,
    issue_id: str,
    *,
    branch: str,
    required_passes: int = DEFAULT_REQUIRED_PASSES,
    expected_repo_name: str = DEFAULT_EXPECTED_REPO_NAME,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    manifest = {
        "issue_id": issue_id,
        "branch": branch,
        "repo_root": str(root),
        "expected_repo_name": expected_repo_name,
        "required_passes": required_passes,
        "created_at": _now(),
        "artifact_schema": "ralph-loop/v1",
    }
    _write_json(_manifest_path(root, issue_id), manifest)
    return manifest


def record_pass(
    repo_root: Path | str,
    issue_id: str,
    *,
    pass_number: int,
    remarks: list[str],
    changes: list[str],
    verification: list[dict[str, str]],
    unresolved_risks: list[str],
    next_pass_prompt: str,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    payload = {
        "issue_id": issue_id,
        "pass_number": pass_number,
        "remarks": remarks,
        "changes": changes,
        "verification": verification,
        "unresolved_risks": unresolved_risks,
        "next_pass_prompt": next_pass_prompt,
        "recorded_at": _now(),
        "artifact_schema": "ralph-loop-pass/v1",
    }
    _write_json(_pass_path(root, issue_id, pass_number), payload)
    return payload


def wipe_context(repo_root: Path | str, issue_id: str, *, pass_number: int) -> Path:
    root = Path(repo_root).resolve()
    loop_dir = _loop_dir(root, issue_id)
    scratch = loop_dir / "scratch" / f"pass-{pass_number}"
    if scratch.exists():
        shutil.rmtree(scratch)
    marker = _wipe_marker_path(root, issue_id, pass_number)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "issue_id": issue_id,
                "pass_number": pass_number,
                "wiped_at": _now(),
                "removed": str(scratch),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return marker


def evaluate_stop_hooks(repo_root: Path | str, issue_id: str) -> GateReport:
    root = Path(repo_root).resolve()
    loop_dir = _loop_dir(root, issue_id)
    reasons: list[str] = []
    manifest_path = _manifest_path(root, issue_id)

    if not manifest_path.exists():
        reasons.append("missing loop manifest")
        return _report(issue_id, loop_dir, reasons)

    try:
        manifest = _read_json(manifest_path)
    except Exception as exc:  # pragma: no cover - defensive path
        reasons.append(f"manifest is not valid JSON: {exc}")
        return _report(issue_id, loop_dir, reasons)

    expected_repo_name = manifest.get("expected_repo_name", DEFAULT_EXPECTED_REPO_NAME)
    if root.name != expected_repo_name:
        reasons.append(f"repo boundary mismatch: expected {expected_repo_name}")

    required_passes = int(manifest.get("required_passes", DEFAULT_REQUIRED_PASSES))
    for pass_number in range(1, required_passes + 1):
        pass_path = _pass_path(root, issue_id, pass_number)
        if not pass_path.exists():
            reasons.append(f"missing pass {pass_number} artifact")
            continue

        try:
            artifact = _read_json(pass_path)
        except Exception as exc:  # pragma: no cover - defensive path
            reasons.append(f"pass {pass_number} artifact is not valid JSON: {exc}")
            continue

        if not artifact.get("remarks"):
            reasons.append(f"pass {pass_number} has no remarks")
        if not artifact.get("changes"):
            reasons.append(f"pass {pass_number} has no change summary")
        if not artifact.get("next_pass_prompt"):
            reasons.append(f"pass {pass_number} has no next-pass prompt")

        verification = artifact.get("verification") or []
        if not verification:
            reasons.append(f"pass {pass_number} has no verification evidence")
        for item in verification:
            status = str(item.get("status", "")).lower()
            if status != "passed":
                name = item.get("name") or "unnamed verification"
                reasons.append(f"pass {pass_number} verification failed: {name}")
            if not item.get("evidence"):
                name = item.get("name") or "unnamed verification"
                reasons.append(f"pass {pass_number} verification lacks evidence: {name}")

        if artifact.get("unresolved_risks"):
            reasons.append(f"pass {pass_number} has unresolved risks")

        if not _wipe_marker_path(root, issue_id, pass_number).exists():
            reasons.append(f"missing context wipe marker for pass {pass_number}")

    report = _report(issue_id, loop_dir, reasons)
    _write_json(loop_dir / "gate-report.json", report.to_dict())
    return report


def _report(issue_id: str, loop_dir: Path, reasons: list[str]) -> GateReport:
    return GateReport(
        issue_id=issue_id,
        blocked=bool(reasons),
        reasons=reasons,
        loop_dir=str(loop_dir),
        checked_at=_now(),
    )


def run_dry_run(repo_root: Path | str, issue_id: str) -> GateReport:
    root = Path(repo_root).resolve()
    init_loop(root, issue_id, branch="dry-run")
    for pass_number in range(1, DEFAULT_REQUIRED_PASSES + 1):
        record_pass(
            root,
            issue_id,
            pass_number=pass_number,
            remarks=[f"Pass {pass_number} reviewed the loop fixture."],
            changes=[f"Pass {pass_number} preserved the handoff artifact contract."],
            verification=[
                {
                    "name": "fixture stop-hook verification",
                    "status": "passed",
                    "evidence": f"dry-run pass {pass_number}",
                }
            ],
            unresolved_risks=[],
            next_pass_prompt=(
                f"Fresh pass {pass_number + 1} should re-read artifacts only."
                if pass_number < DEFAULT_REQUIRED_PASSES
                else "Final pass has no unresolved risks."
            ),
        )
        wipe_context(root, issue_id, pass_number=pass_number)
    return evaluate_stop_hooks(root, issue_id)


def render_linear_comment(repo_root: Path | str, issue_id: str) -> str:
    root = Path(repo_root).resolve()
    manifest = _read_json(_manifest_path(root, issue_id))
    report = evaluate_stop_hooks(root, issue_id)
    status = "blocked" if report.blocked else "clear"
    lines = [
        f"Ralph-loop handoff for `{issue_id}`",
        "",
        f"- Branch: `{manifest.get('branch', '')}`",
        f"- Artifact dir: `{Path(report.loop_dir)}`",
        f"- Stop-hook status: {status}",
    ]
    if report.reasons:
        lines.append("- Blocking reasons:")
        lines.extend(f"  - {reason}" for reason in report.reasons)

    required_passes = int(manifest.get("required_passes", DEFAULT_REQUIRED_PASSES))
    for pass_number in range(1, required_passes + 1):
        path = _pass_path(root, issue_id, pass_number)
        if not path.exists():
            continue
        artifact = _read_json(path)
        lines.extend(
            [
                "",
                f"Pass {pass_number}",
                f"- Remarks: {'; '.join(artifact.get('remarks') or [])}",
                f"- Changes: {'; '.join(artifact.get('changes') or [])}",
                "- Verification:",
            ]
        )
        for item in artifact.get("verification") or []:
            lines.append(
                f"  - {item.get('name', 'verification')}: "
                f"{item.get('status', 'unknown')} ({item.get('evidence', 'no evidence')})"
            )
        risks = artifact.get("unresolved_risks") or []
        lines.append(f"- Unresolved risks: {'; '.join(risks) if risks else 'none'}")
    return "\n".join(lines) + "\n"


def _verification_from_cli(values: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for value in values:
        parts = value.split("|", 2)
        if len(parts) != 3:
            raise SystemExit(
                "verification entries must use name|status|evidence format"
            )
        out.append({"name": parts[0], "status": parts[1], "evidence": parts[2]})
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ralph-loop handoff gates.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        default=".",
        help="Repository root. Defaults to current directory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser(
        "init", parents=[common], help="Initialize loop manifest."
    )
    init_cmd.add_argument("issue")
    init_cmd.add_argument("--branch", required=True)

    record = sub.add_parser(
        "record-pass", parents=[common], help="Write one pass artifact."
    )
    record.add_argument("issue")
    record.add_argument("--pass-number", type=int, required=True)
    record.add_argument("--remark", action="append", default=[])
    record.add_argument("--change", action="append", default=[])
    record.add_argument(
        "--verification",
        action="append",
        default=[],
        help="name|status|evidence",
    )
    record.add_argument("--unresolved-risk", action="append", default=[])
    record.add_argument("--next-pass-prompt", required=True)

    wipe = sub.add_parser(
        "wipe", parents=[common], help="Clear pass scratch context and mark wipe."
    )
    wipe.add_argument("issue")
    wipe.add_argument("--pass-number", type=int, required=True)

    check = sub.add_parser("check", parents=[common], help="Evaluate stop-hooks.")
    check.add_argument("issue")

    comment = sub.add_parser(
        "linear-comment", parents=[common], help="Render Linear/PR summary."
    )
    comment.add_argument("issue")

    dry = sub.add_parser(
        "dry-run", parents=[common], help="Create a clean three-pass fixture."
    )
    dry.add_argument("--issue", default="CONT-18-dry-run")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()

    if args.command == "init":
        manifest = init_loop(root, args.issue, branch=args.branch)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.command == "record-pass":
        artifact = record_pass(
            root,
            args.issue,
            pass_number=args.pass_number,
            remarks=args.remark,
            changes=args.change,
            verification=_verification_from_cli(args.verification),
            unresolved_risks=args.unresolved_risk,
            next_pass_prompt=args.next_pass_prompt,
        )
        print(json.dumps(artifact, indent=2, sort_keys=True))
        return 0
    if args.command == "wipe":
        marker = wipe_context(root, args.issue, pass_number=args.pass_number)
        print(marker)
        return 0
    if args.command == "check":
        report = evaluate_stop_hooks(root, args.issue)
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 1 if report.blocked else 0
    if args.command == "linear-comment":
        print(render_linear_comment(root, args.issue), end="")
        return 0
    if args.command == "dry-run":
        run_dry_run(root, args.issue)
        print(render_linear_comment(root, args.issue), end="")
        return 0
    raise AssertionError(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
