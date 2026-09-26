"""Guard: retired account-password literals must never ship again.

The Pipeline intake modal once printed a shared default TikTok account
password as literal text, which Vite compiled into the public JS bundle, and
the backend fell back to a guessable default when its env var was unset. This
test fails if either value reappears in the backend sources, anywhere else in
the repository's text sources or, when it has been built, in ``frontend/dist``
(the bundle the Lab serves).

Only SHA-256 digests of the retired values are stored here, never the values.
Candidate substrings are hashed and compared against the digest, so the test
can be run and its failures reported without disclosing the password.

Run against another built bundle with:
    python tests/test_no_shipped_default_password.py path/to/dist
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# SHA-256 hex digest and length of each retired literal. Add new entries here
# whenever a credential is found in source; never add the credential itself.
FORBIDDEN_LITERAL_DIGESTS: dict[str, int] = {
    # The shared default TikTok account password once printed by the Pipeline
    # intake modal (and, until 2026-06-19, hard-coded in routers/pipeline.py).
    "2c04122561f52246bb9d4baa62cd53f23886d9d2540542e9c8c69a34d1adc2a7": 15,
    # The guessable fallback routers/pipeline.py used when DEFAULT_INTAKE_PASSWORD
    # was unset; intakes now refuse with a typed 503 instead.
    "057ba03d6c44104863dc7361fe4578965d1887360f90a0895882e58a6248fc86": 8,
}

TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".map", ".html",
    ".htm", ".css", ".json", ".md", ".toml", ".txt", ".yml", ".yaml", ".sh",
    ".cfg", ".ini", ".env", ".example", ".svg",
}

# Runtime output and dependency trees; frontend/dist is deliberately scanned.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
    "output", "caption_output", "burn_output", "burn_uploads", "video-output",
    "projects", "playwright-report", "test-results",
}

MAX_FILE_BYTES = 16 * 1024 * 1024

# Split on whitespace, quotes, markup and code punctuation. Characters that can
# occur inside a password (letters, digits, $ ! @ # % & * _ - . + ^ ~ ?) stay
# inside the token; every fixed-length window of each token is then hashed, so
# trailing sentence punctuation cannot hide a match.
_TOKEN_SPLIT = re.compile(r"[\s\"'`<>{}()\[\],;:=|\\/]+")


def _digests_by_length(digests: dict[str, int]) -> dict[int, set[str]]:
    by_len: dict[int, set[str]] = {}
    for digest, length in digests.items():
        by_len.setdefault(length, set()).add(digest)
    return by_len


def text_contains_forbidden(text: str, digests: dict[str, int] = FORBIDDEN_LITERAL_DIGESTS) -> int:
    """Return how many forbidden-literal occurrences ``text`` contains."""
    by_len = _digests_by_length(digests)
    hits = 0
    for token in _TOKEN_SPLIT.split(text):
        for length, wanted in by_len.items():
            if len(token) < length:
                continue
            for start in range(len(token) - length + 1):
                window = token[start:start + length].encode("utf-8")
                if hashlib.sha256(window).hexdigest() in wanted:
                    hits += 1
    return hits


def scan_tree(root: Path, digests: dict[str, int] = FORBIDDEN_LITERAL_DIGESTS) -> dict[str, int]:
    """Map each offending file (relative to ``root``) to its occurrence count."""
    found: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            count = text_contains_forbidden(text, digests)
            if count:
                found[str(path.relative_to(root))] = count
    return found


def _dummy_digest(value: str) -> dict[str, int]:
    return {hashlib.sha256(value.encode("utf-8")).hexdigest(): len(value)}


def test_matcher_finds_literal_in_jsx_and_bundle_shapes():
    dummy = "Dummy-Secret99!"
    digests = _dummy_digest(dummy)
    assert text_contains_forbidden(f'<span className="x">{dummy}</span>', digests) == 1
    assert text_contains_forbidden(f'children:"{dummy}"}}),', digests) == 1
    assert text_contains_forbidden(f"use password {dummy}. Then", digests) == 1
    assert text_contains_forbidden("use the standard intake password", digests) == 0


def test_backend_sources_do_not_contain_retired_password():
    backend = [REPO_ROOT / name for name in ("routers", "services", "providers")]
    backend += sorted(REPO_ROOT.glob("*.py"))
    found: dict[str, int] = {}
    for target in backend:
        if target.is_dir():
            for path, count in scan_tree(target).items():
                found[f"{target.name}/{path}"] = count
        elif target.is_file():
            count = text_contains_forbidden(target.read_text(encoding="utf-8", errors="ignore"))
            if count:
                found[target.name] = count
    assert backend and not found, (
        "retired password literal found in backend source (value withheld): "
        + ", ".join(f"{path}={count}" for path, count in sorted(found.items()))
    )


def test_repository_sources_do_not_contain_retired_password():
    found = scan_tree(REPO_ROOT)
    assert not found, (
        "retired default account password found (value withheld): "
        + ", ".join(f"{path}={count}" for path, count in sorted(found.items()))
    )


def test_built_frontend_bundle_does_not_contain_retired_password():
    dist = REPO_ROOT / "frontend" / "dist"
    if not dist.is_dir():
        import pytest  # imported here so the CLI mode needs only the stdlib

        pytest.skip("frontend/dist not built; run `npm run build` in frontend/ to check the bundle")
    found = scan_tree(dist)
    assert not found, (
        "retired default account password found in built bundle (value withheld): "
        + ", ".join(f"{path}={count}" for path, count in sorted(found.items()))
    )


if __name__ == "__main__":
    targets = [Path(arg) for arg in sys.argv[1:]] or [REPO_ROOT]
    total = 0
    for target in targets:
        for path, count in sorted(scan_tree(target).items()):
            print(f"{target}/{path}: {count}")
            total += count
    print(f"forbidden-literal occurrences: {total}")
    sys.exit(1 if total else 0)
