"""Reproduce image-only missing imports without fallback to the source checkout."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def stage_backend_files(destination):
    backend = False
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if not line.startswith(("FROM ", "COPY ")):
            continue
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0] == "FROM":
            backend = words[1].startswith("python:")
        if not backend or words[0] != "COPY" or words[1].startswith("--from="):
            continue
        assert not any(word.startswith("--") for word in words[1:])
        target = destination / words[-1]
        target.mkdir(parents=True, exist_ok=True)
        for source_name in words[1:-1]:
            source = ROOT / source_name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target / source.name)
    # The image creates these before importing app. No production volume or
    # credentials enter this isolated, temporary tree.
    for name in ("output", "caption_output", "burn_output", "projects"):
        (destination / name).mkdir()


@pytest.mark.parametrize("omit_quality_gate", [False, True])
def test_packaged_app_imports_and_detects_the_missing_quality_gate(tmp_path, omit_quality_gate):
    image = tmp_path / "image"
    image.mkdir()
    stage_backend_files(image)
    if omit_quality_gate:
        (image / "burn_quality_gate.py").unlink()
    script = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
sys.path.extend(json.loads(sys.argv[2]))
def no_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('network is forbidden in the packaging smoke test')
sys.addaudithook(no_network)
import app
import burn_quality_gate
from services import post_render
for module in (app, burn_quality_gate, post_render):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
assert any(route.path == '/api/control-plane/v1/post-renders' for route in app.app.routes)
print('packaged backend imports succeeded')
"""
    # Some development interpreters install dependencies in user site-packages.
    # Carry only those dependency directories, never the checkout or PYTHONPATH.
    dependencies = [path for path in sys.path
                    if Path(path).name in {"site-packages", "dist-packages"}]
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(image), json.dumps(dependencies)], cwd=image,
        env={"PATH": os.environ.get("PATH", ""), "PYTHON_DOTENV_DISABLED": "1",
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    if omit_quality_gate:
        assert result.returncode != 0
        assert "ModuleNotFoundError: No module named 'burn_quality_gate'" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "packaged backend imports succeeded" in result.stdout
