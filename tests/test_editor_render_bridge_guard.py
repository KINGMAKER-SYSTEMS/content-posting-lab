"""Pins the load-time guard that protects services/editor_render.py from a lost
or broken services/openshot_bridge.py.

openshot_bridge is the sanctioned OpenShot compiler module that editor_render
imports at module load. It has been UNTRACKED in a worktree and nearly lost; it
is now committed, but a worktree/checkout accident can still leave it (a) absent
or (b) present-but-truncated. Both must fail closed with the recovery message
instead of either a bare ModuleNotFoundError pointing at nothing or a cryptic
AttributeError mid-render.

These run editor_render's import in a child process against a throwaway repo
copy so the real sys.modules / package state is never mutated and ffmpeg is not
required.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EDITOR_RENDER = REPO_ROOT / "services" / "editor_render.py"


def _import_editor_render_with_bridge(tmp_path: Path, bridge_body: str | None) -> subprocess.CompletedProcess:
    """Build a minimal `services` package in tmp_path containing the real
    editor_render.py plus a stand-in openshot_bridge.py (or none), then try to
    import it in a child process. Returns the completed process."""
    pkg = tmp_path / "services"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "editor_render.py").write_text(EDITOR_RENDER.read_text())
    if bridge_body is not None:
        (pkg / "openshot_bridge.py").write_text(bridge_body)

    script = textwrap.dedent(
        """
        import importlib
        try:
            importlib.import_module("services.editor_render")
        except ModuleNotFoundError as exc:
            print("MNF::" + str(exc))
            raise SystemExit(0)
        print("OK")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )


def test_missing_bridge_file_reraises_with_recovery_message(tmp_path):
    proc = _import_editor_render_with_bridge(tmp_path, bridge_body=None)
    assert proc.returncode == 0, proc.stderr
    assert "MNF::" in proc.stdout
    assert "services/openshot_bridge.py is missing" in proc.stdout
    # The worktree-specific recovery (copy from main, then commit) must be there.
    assert "copy it from the main checkout" in proc.stdout


def test_empty_bridge_stub_fails_closed_with_recovery_message(tmp_path):
    # File present but truncated/empty: imports fine, exposes none of the symbols
    # editor_render compiles against. Must fail closed, not limp on to a cryptic
    # AttributeError at render time.
    proc = _import_editor_render_with_bridge(tmp_path, bridge_body="")
    assert proc.returncode == 0, proc.stderr
    assert "MNF::" in proc.stdout
    assert "present-but-broken stub" in proc.stdout
    assert "services/openshot_bridge.py is missing" in proc.stdout


def test_partial_bridge_stub_fails_closed_with_recovery_message(tmp_path):
    # The nastiest case the line-66 tuple check exists for: the file is present
    # and imports fine and even exposes ONE of the required symbols, but a
    # merge-clobber / partial-write dropped another. A naive `hasattr` on a
    # single attr would wave this through and then blow up with a cryptic
    # AttributeError deep in render(). The tuple check must catch it at load and
    # name the missing attr.
    body = textwrap.dedent(
        """
        def timeline_json(*a, **k):
            return "{}"
        # _resolve_asset_src lost in a merge clobber -- intentionally absent.
        """
    )
    proc = _import_editor_render_with_bridge(tmp_path, bridge_body=body)
    assert proc.returncode == 0, proc.stderr
    assert "MNF::" in proc.stdout
    assert "present-but-broken stub" in proc.stdout
    assert "_resolve_asset_src" in proc.stdout
    # The one symbol that WAS present must not be falsely reported as missing.
    assert "timeline_json" not in proc.stdout


def test_valid_bridge_stub_imports_cleanly(tmp_path):
    # A bridge exposing the required surface must import without tripping the
    # guard (no false positive that would block real renders).
    body = textwrap.dedent(
        """
        def timeline_json(*a, **k):
            return "{}"

        def _resolve_asset_src(src, asset_root=None):
            return src
        """
    )
    proc = _import_editor_render_with_bridge(tmp_path, bridge_body=body)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr
