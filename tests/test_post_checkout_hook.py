"""Tests for the post-checkout git hook that auto-stages
services/openshot_bridge.py.

The hook lives in the git *common* dir (.git/hooks/post-checkout), shared by all
worktrees, so that a `git worktree add` / `git checkout` never leaves the
sanctioned OpenShot compiler module untracked and at risk of being lost (the
recurring lesson recorded in services/editor_render.py and prod-cycle.js).

These tests are self-contained: they copy the live hook into a throwaway git
repo and exercise its three branches (untracked -> staged, tracked -> no-op,
absent -> no-op) without touching the real repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BRIDGE = "services/openshot_bridge.py"


def _hook_path() -> Path:
    """Locate the post-checkout hook to test.

    Prefer the installed copy in this repo's git common dir; fall back to the
    tracked source (scripts/post-checkout.hook) so the test still runs in a
    fresh clone where install-git-hooks.sh has not been run yet.
    """
    here = Path(__file__).resolve().parent
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=here,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    installed = (here / common / "hooks" / "post-checkout").resolve()
    if installed.exists():
        return installed
    return (here.parent / "scripts" / "post-checkout.hook").resolve()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _staged_files(repo: Path) -> set[str]:
    out = _git(repo, "diff", "--cached", "--name-only").stdout
    return {line for line in out.splitlines() if line}


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """A fresh git repo with the live post-checkout hook installed."""
    hook = _hook_path()
    assert hook.exists(), f"no post-checkout hook found (installed or tracked) at {hook}"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "post-checkout"
    shutil.copy(hook, dest)
    dest.chmod(0o755)
    (repo / "services").mkdir()
    return repo


def _run_hook(repo: Path) -> subprocess.CompletedProcess:
    # post-checkout args: <prev-head> <new-head> <branch-flag>
    return subprocess.run(
        [str(repo / ".git" / "hooks" / "post-checkout"), "0", "0", "1"],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_untracked_bridge_is_staged(sandbox: Path) -> None:
    (sandbox / BRIDGE).write_text("# bridge\n")
    assert BRIDGE not in _staged_files(sandbox)

    proc = _run_hook(sandbox)
    assert proc.returncode == 0, proc.stderr

    assert BRIDGE in _staged_files(sandbox)


def test_tracked_bridge_is_noop(sandbox: Path) -> None:
    (sandbox / BRIDGE).write_text("# bridge\n")
    _git(sandbox, "add", BRIDGE)
    _git(sandbox, "commit", "-q", "-m", "add bridge")
    assert not _staged_files(sandbox)

    proc = _run_hook(sandbox)
    assert proc.returncode == 0, proc.stderr

    # Already tracked + unmodified -> nothing to stage.
    assert not _staged_files(sandbox)


def test_missing_bridge_is_noop(sandbox: Path) -> None:
    assert not (sandbox / BRIDGE).exists()

    proc = _run_hook(sandbox)
    assert proc.returncode == 0, proc.stderr

    assert not _staged_files(sandbox)
