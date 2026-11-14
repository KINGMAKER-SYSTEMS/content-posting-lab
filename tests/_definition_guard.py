"""Detect direct, unconditional test function definitions shadowed in one namespace.

This does not model conditional definitions, assignments, imports, class
redefinitions or Python's whole-program name resolution. Parsing never imports the inspected modules.
"""
import argparse
import ast
import json
from pathlib import Path


def duplicates_in_file(path):
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    duplicates = []

    def visit(body, namespace):
        seen = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if node.name in seen:
                    duplicates.append({"path": str(path), "namespace": namespace,
                                       "name": node.name, "firstLine": seen[node.name],
                                       "duplicateLine": node.lineno})
                else:
                    seen[node.name] = node.lineno
            elif isinstance(node, ast.ClassDef):
                visit(node.body, node.name if namespace == "<module>" else namespace + "." + node.name)

    visit(tree.body, "<module>")
    return duplicates


def scan_paths(paths):
    files = set()
    for value in paths:
        path = Path(value)
        if path.is_dir():
            files.update(path.rglob("test_*.py"))
        elif path.is_file():
            files.add(path)
        else:
            raise ValueError("inspection path does not exist")
    if not files:
        raise ValueError("no test files found")
    return [item for path in sorted(files) for item in duplicates_in_file(path)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    try:
        duplicates = scan_paths(args.paths)
    except (OSError, SyntaxError, UnicodeError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__}))
        return 2
    print(json.dumps({"duplicates": duplicates}, sort_keys=True))
    return 1 if duplicates else 0


if __name__ == "__main__":
    raise SystemExit(main())
