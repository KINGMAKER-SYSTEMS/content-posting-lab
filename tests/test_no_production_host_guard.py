"""CI guard: no production hostname in Lab app code.

Every service origin comes from the environment and fails closed when unset
(see tests/test_no_production_url_defaults.py). This source scan keeps it that
way. It parses every tracked ``.py`` file outside test directories and fails
on any string value naming a known production host. That covers the default
positions (a getenv/``.get`` default, an ``or`` fallback, a module constant, a
class or dataclass default, a parameter default) and, more strictly, any
other literal, because a hardcoded production target is the same hazard
whether or not it is spelled as a fallback. Each finding names its position.

Non-Python tracked files (env templates, JSON/TOML config, frontend source)
are scanned for production URLs too. Files that describe production by
design are allowlisted below, each with its reason; an allowlist entry that
no longer names a production host fails, so the list cannot rot.

The hostname list lives here, and only here, on purpose.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Hosts that are production by suffix, matched in URLs and as bare hostnames.
PRODUCTION_HOST_SUFFIXES = (
    "risingtidesviral.com",  # ShipStream vault, Control Plane ingress, content buckets
    "risingtidesent.com",    # company domain
    ".up.railway.app",       # Campaign Hub, Content Lab and other Railway services
    ".workers.dev",          # Cloudflare Worker default hosts (Control Plane Worker)
    ".supabase.co",          # Supabase project URLs
    ".notion.site",          # published Notion workspaces
)
# Service names that mark a production host when they appear in a URL's host.
PRODUCTION_HOST_WORDS = (
    "risingtides", "shipstream", "syzygy", "campaign-hub", "control-plane", "content-lab",
)

# RFC 2606/6761 reserved names never resolve to production.
RESERVED_HOST_SUFFIXES = (".test", ".example", ".invalid", ".localhost")

# Tracked non-Python files that name production hosts by design.
ALLOWLIST = {
    "AGENTS.md": "devlog/ownership doc; states where production runs, never loaded at runtime",
    "CLAUDE.md": "agent operating doc; describes the deployed topology, never loaded at runtime",
    "events.md": "append-only devlog of production events, never loaded at runtime",
    "docs/post-render-setup.md": "deploy doc: the production ingress origins an operator sets",
    "docs/visual-admission.md": "deploy doc: where the production visual gate runs",
}

_URL_HOST = re.compile(
    r"[a-z][a-z0-9+.-]*://(?:[^/@\s'\"]*@)?(\[[^\]\s]+\]|[^/:?#\s'\"`)<>\]]+)", re.I,
)
_BARE_HOST = re.compile(r"(?<![\w@.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.I)
_TEST_DIR_NAMES = {"tests", "test", "__tests__"}
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp4", ".mov", ".mp3", ".wav",
    ".ttf", ".otf", ".woff", ".woff2", ".pdf", ".zip", ".lock",
}


def production_hosts(text: str) -> list[str]:
    """Return every production host named in ``text`` (emails excluded)."""
    found: list[str] = []
    for match in _URL_HOST.finditer(text):
        host = match.group(1).strip("[]").rstrip(".").lower()
        if host.endswith(RESERVED_HOST_SUFFIXES):
            continue
        if host.endswith(PRODUCTION_HOST_SUFFIXES) or any(
            word in host for word in PRODUCTION_HOST_WORDS
        ):
            found.append(host)
    for match in _BARE_HOST.finditer(text):
        host = match.group(1).lower()
        if host.endswith(PRODUCTION_HOST_SUFFIXES) and host not in found:
            found.append(host)
    return found


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str
    host: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.kind}] {self.host}"


_DEFAULT_CALLS = {"getenv", "get", "setdefault", "pop"}


def _label_regions(tree: ast.Module) -> dict[int, str]:
    """Map id(string constant) -> the default position that holds it."""
    labels: dict[int, str] = {}

    def mark(node: ast.AST | None, kind: str) -> None:
        if node is None:
            return
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                labels.setdefault(id(child), kind)

    # Most specific first: setdefault keeps the first label.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _DEFAULT_CALLS:
                for arg in node.args[1:]:
                    mark(arg, "getenv/get default")
                for keyword in node.keywords:
                    if keyword.arg == "default":
                        mark(keyword.value, "getenv/get default")
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values[1:]:
                if isinstance(value, (ast.Constant, ast.JoinedStr)):
                    mark(value, "or-fallback")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                mark(default, "parameter default")
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            mark(statement.value, "module constant")
        elif isinstance(statement, ast.ClassDef):
            for member in statement.body:
                if isinstance(member, (ast.Assign, ast.AnnAssign)):
                    mark(member.value, "class/dataclass default")
    return labels


def scan_python(source: str, path: str) -> list[Violation]:
    tree = ast.parse(source, filename=path)
    labels = _label_regions(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for host in production_hosts(node.value):
                violations.append(Violation(
                    path, node.lineno, labels.get(id(node), "string literal"), host,
                ))
    return sorted(violations, key=lambda v: (v.line, v.kind, v.host))


def _tracked_files() -> list[str]:
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
        ).stdout.decode()
        files = [name for name in output.split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for directory, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in {
                ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "projects",
            }]
            files.extend(
                str((Path(directory) / name).relative_to(ROOT)) for name in names
            )
    return sorted(
        name for name in files
        if not _TEST_DIR_NAMES.intersection(Path(name).parts[:-1])
        and (ROOT / name).is_file()
    )


def test_app_python_names_no_production_host():
    violations = [
        violation
        for name in _tracked_files() if name.endswith(".py")
        for violation in scan_python((ROOT / name).read_text(encoding="utf-8"), name)
    ]
    assert not violations, (
        "Production hostnames in app code. Read the origin from an env var and "
        "fail closed when it is unset (ConfigError / HTTP 503):\n"
        + "\n".join(str(v) for v in violations)
    )


def _non_python_hits() -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for name in _tracked_files():
        path = ROOT / name
        if name.endswith(".py") or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hosts = [
            match.group(1).lower() for match in _URL_HOST.finditer(text)
            if production_hosts(match.group(0))
        ]
        if hosts:
            hits[name] = sorted(set(hosts))
    return hits


def test_non_python_files_name_production_urls_only_when_allowlisted():
    hits = _non_python_hits()
    unexpected = {name: hosts for name, hosts in hits.items() if name not in ALLOWLIST}
    assert not unexpected, (
        "Production URLs outside the allowlist (templates and config must not "
        f"carry production defaults): {unexpected}"
    )
    stale = sorted(set(ALLOWLIST) - set(hits))
    assert not stale, f"Allowlist entries that no longer name a production URL: {stale}"
    assert all(reason.strip() for reason in ALLOWLIST.values())


# ── The detector itself ──────────────────────────────────────────────────

HUB_HOST = "risingtides-campaign-hub-production.up.railway.app"


@pytest.mark.parametrize(("source", "kind"), [
    (f'import os\nX = os.getenv("X", "https://{HUB_HOST}")\n', "getenv/get default"),
    (f'import os\nX = os.environ.get("X", default="https://{HUB_HOST}")\n', "getenv/get default"),
    (f'import os\ndef f():\n    return os.getenv("X") or "https://{HUB_HOST}"\n', "or-fallback"),
    (f'X = "https://{HUB_HOST}"\n', "module constant"),
    (f'ORIGINS = ("https://{HUB_HOST}",)\n', "module constant"),
    (f'from dataclasses import dataclass\n@dataclass\nclass S:\n    url: str = "https://{HUB_HOST}"\n',
     "class/dataclass default"),
    (f'def f(url="https://{HUB_HOST}"):\n    return url\n', "parameter default"),
    (f'async def f(*, url: str = "https://{HUB_HOST}/api"):\n    return url\n', "parameter default"),
    (f'def f(slug):\n    return f"https://{HUB_HOST}/api/campaign/{{slug}}"\n', "string literal"),
    ('HOST = "shipstream.risingtidesviral.com"\n', "module constant"),
    ('X = "https://imxflhdsfxkfmvsufwvr.supabase.co"\n', "module constant"),
    ('X = "https://control-plane-worker.example.workers.dev"\n', "module constant"),
])
def test_detector_flags_each_default_position(source, kind):
    violations = scan_python(source, "sample.py")
    assert len(violations) == 1, violations
    assert violations[0].kind == kind


@pytest.mark.parametrize("source", [
    'SCHEMA = "shipstream.source-manifest.v1"\n',
    'EXECUTOR = "syzygy-slideshow.formats.lyric-edits.templates"\n',
    'SENDER = "henry@risingtidesent.com"\n',
    'ALIAS = f"{name}@risingtidesviral.com"\n',
    'import os\nX = os.getenv("CAMPAIGN_HUB_URL", "")\n',
    'X = "https://hub.test"\nY = "https://shipstream.test/vault/x"\n',
    'X = "https://api.openai.com/v1/chat/completions"\n',
    'LANE = "content-bucket-control-plane"\n',
])
def test_detector_ignores_non_production_values(source):
    assert scan_python(source, "sample.py") == []


# ── Mutation proof: restoring the incident's default turns the guard red ─

ORIGINAL_HUB_DEFAULT = (
    'CAMPAIGN_HUB_URL = os.getenv(\n'
    '    "CAMPAIGN_HUB_URL",\n'
    f'    "https://{HUB_HOST}",\n'
    ').rstrip("/")\n'
)
ORIGINAL_SHIPSTREAM_ORIGIN = 'SHIPSTREAM_ORIGIN = "https://shipstream.risingtidesviral.com"\n'


@pytest.mark.parametrize(("path", "marker", "mutation", "expected"), [
    ("services/campaign_hub.py", 'CAMPAIGN_HUB_URL_ENV = "CAMPAIGN_HUB_URL"\n',
     ORIGINAL_HUB_DEFAULT, ("getenv/get default", HUB_HOST)),
    ("services/shipstream_source_manifest.py",
     'SHIPSTREAM_VAULT_ORIGIN_ENV = "SHIPSTREAM_VAULT_ORIGIN"\n',
     ORIGINAL_SHIPSTREAM_ORIGIN, ("module constant", "shipstream.risingtidesviral.com")),
])
def test_restoring_an_original_default_turns_the_guard_red(path, marker, mutation, expected):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert scan_python(source, path) == []
    assert source.count(marker) == 1
    mutated = source.replace(marker, marker + mutation, 1)
    assert [(v.kind, v.host) for v in scan_python(mutated, path)] == [expected]
