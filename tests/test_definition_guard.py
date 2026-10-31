"""Local definition coverage; no application or optional backend imports."""
import json
from pathlib import Path
import subprocess
import sys

from tests import _definition_guard as guard


def write_source(tmp_path, source):
    path = tmp_path / "test_example.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_duplicate_module_definitions_are_reported(tmp_path):
    path = write_source(tmp_path, "def test_hidden():\n    assert 1\ndef test_hidden():\n    assert 2\n")
    assert guard.duplicates_in_file(path) == [{
        "path": str(path), "namespace": "<module>", "name": "test_hidden",
        "firstLine": 1, "duplicateLine": 3,
    }]


def test_duplicate_async_class_definitions_are_reported(tmp_path):
    path = write_source(tmp_path, "class TestOne:\n    async def test_hidden(self): pass\n    def test_hidden(self): pass\n")
    assert [(d["namespace"], d["name"], d["firstLine"], d["duplicateLine"])
            for d in guard.duplicates_in_file(path)] == [("TestOne", "test_hidden", 2, 3)]


def test_distinct_namespaces_and_conditional_definitions_are_out_of_scope(tmp_path):
    path = write_source(tmp_path, "def test_same(): pass\nclass TestOne:\n    def test_same(self): pass\nclass TestTwo:\n    def test_same(self): pass\nif True:\n    def test_same(): pass\ndef helper():\n    def test_same(): pass\n")
    assert guard.duplicates_in_file(path) == []


def test_cli_reports_duplicate_exit_one(tmp_path):
    path = write_source(tmp_path, "def test_a(): pass\ndef test_a(): pass\n")
    result = subprocess.run([sys.executable, str(Path(guard.__file__)), str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert json.loads(result.stdout)["duplicates"][0]["name"] == "test_a"


def test_cli_clean_exit_zero(tmp_path):
    path = write_source(tmp_path, "def test_a(): pass\n")
    result = subprocess.run([sys.executable, str(Path(guard.__file__)), str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"duplicates": []}


def test_cli_parse_error_is_not_clean(tmp_path):
    path = write_source(tmp_path, "def test_a(:\n")
    result = subprocess.run([sys.executable, str(Path(guard.__file__)), str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error": "SyntaxError"}


def test_repository_has_no_unconditional_duplicate_test_function_definitions():
    assert guard.scan_paths([Path(__file__).parent]) == []
