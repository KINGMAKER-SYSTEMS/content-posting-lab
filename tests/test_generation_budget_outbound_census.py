"""Census: every real outbound prediction-creation site is metered.

Replaces the old router-import census, which only proved no other *router*
imported the meter and so could not see a paid provider path added under
``services/`` or ``providers/``. This walks every backend module's syntax tree,
finds each place that actually creates a new billable provider prediction (an
HTTP POST to a Replicate ``/predictions`` endpoint, or to xAI
``/videos/generations``), and requires that submission to be metered against the
daily budget before the request.

A site is:
  * a ``.post(...)`` call whose URL literal names ``/predictions`` or
    ``videos/generations`` (Replicate / xAI submission) — a ``/cancel`` URL is a
    best-effort cancellation, not creation, and is excluded;
  * a ``Request(...)`` constructor whose URL literal names ``/predictions`` (the
    ABN raw-urllib Flux/Wan lanes).

Metering: the enclosing function must call a generation-budget reserve/debit
(``debit_generation_spend_at``, ``reserve_generation_spend_at``,
``reserve_generation_spend``, or the ABN chokepoint ``_charge_abn_submission``).
The FIRST submission of the two shared ``generate`` entry points is metered by
the CALLER (the Control Plane executor debits the ``:s0`` id and the operator-UI
execution wrapper debits), not inside the provider module, so those two functions are an
explicit, reviewed allowlist with a companion assertion that the caller meters
exist. Replicate visual-admission (``client.stream("POST", ...)`` in
``services/visual_admission.py``) is OCR/vision, not media generation, and is
out of scope — and it does not match the ``.post``/``Request`` detectors.
"""

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {"tests", "frontend", "node_modules", "__pycache__", "artifacts", "static"}

METER_FUNCS = frozenset({
    "debit_generation_spend_at",
    "reserve_generation_spend_at",
    "reserve_generation_spend",
    "_charge_abn_submission",
})

# The first submission of these two shared provider ``generate`` entry points is
# metered by the CALLER, not inside the module (replicate.generate's first
# submission is the executor's ":s0" debit / the UI execution wrapper's debit;
# HTTP retries and PA resubmission ARE metered inside the provider).
CALLER_METERED = {
    ("providers/replicate.py", "_start_prediction"),
    ("providers/grok.py", "generate"),
}


def _backend_modules():
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIPPED_DIRS and not name.startswith(".")
        )
        for name in sorted(files):
            if name.endswith(".py"):
                path = Path(directory) / name
                yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _url_is_prediction(node) -> bool:
    """True when a URL expression literally names a prediction-creation endpoint."""
    constants = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            constants.append(child.value)
    text = " ".join(constants)
    if "predictions" not in text and "videos/generations" not in text:
        return False
    return "cancel" not in text


def _creation_sites(source, filename):
    """Yield (func_name, lineno) for every outbound prediction-creation call."""
    tree = ast.parse(source, filename=filename)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            url = None
            if isinstance(func, ast.Attribute) and func.attr in ("post", "Request"):
                if node.args:
                    url = node.args[0]
            if url is not None and _url_is_prediction(url):
                fn = enclosing(node)
                if fn is not None:
                    yield fn.name, node.lineno


def _references_meter(node, names):
    """True when the function references a meter entry point by name.

    Covers both a direct call (``generation_budget.reserve_generation_spend_at(...)``,
    ``_charge_abn_submission(...)``) and the common pass-a-function-reference form
    (``asyncio.to_thread(generation_budget.debit_generation_spend_at, ...)``, where
    the meter appears as an ``Attribute`` argument, not a ``Call``).
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in names:
            return child.attr
        if isinstance(child, ast.Name) and child.id in names:
            return child.id
    return None


def test_every_outbound_prediction_creation_site_is_metered():
    sites: dict[tuple[str, str], list[int]] = {}
    for filename, source in _backend_modules():
        for func_name, lineno in _creation_sites(source, filename):
            sites.setdefault((filename, func_name), []).append(lineno)

    # The census must see the exact real paths, or it would pass by finding
    # nothing (and must fail if a new outbound creation site is added).
    expected = {
        ("providers/replicate.py", "_start_prediction"),
        ("providers/replicate.py", "remove_text"),
        ("providers/grok.py", "generate"),
        ("services/abn_factory.py", "_flux_sync"),
        ("services/abn_factory.py", "_wan_i2v_sync"),
    }
    assert set(sites) == expected, (
        f"outbound prediction-creation sites changed: {sorted(sites)}"
    )

    violations = []
    for (filename, func_name), linenos in sorted(sites.items()):
        source = (ROOT / filename).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=filename)
        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name),
            None,
        )
        if fn is None:
            violations.append(f"{filename}:{linenos[0]} {func_name} (function not found)")
            continue
        if _references_meter(fn, METER_FUNCS):
            continue
        if (filename, func_name) in CALLER_METERED:
            continue
        violations.append(f"{filename}:{linenos[0]} {func_name} (enclosing function does not meter)")
    assert violations == [], violations

    # The two caller-metered first-submission chokepoints must have their callers
    # metering: the Control Plane executor debits the ":s0" submission and the
    # operator-UI execution wrapper debits after acquiring its permit.
    cp_src = (ROOT / "routers/control_plane.py").read_text(encoding="utf-8")
    video_src = (ROOT / "routers/video.py").read_text(encoding="utf-8")
    assert "debit_generation_spend_at" in cp_src, "executor must debit the first submission"
    assert "debit_generation_spend_at" in video_src, "UI execution must debit the first submission"


def test_the_census_catches_an_unmetered_prediction_site():
    bypass = '''\nasync def generate(prompt, params, client):
    resp = await client.post("https://api.replicate.com/v1/models/x/predictions", json={})
'''
    assert list(_creation_sites(bypass, "providers/fake.py")) == [("generate", 3)]


def test_the_census_ignores_poll_and_cancel_urls():
    poll_and_cancel = '''\nasync def poll(client, pred_id):
    await client.get(f"https://api.replicate.com/v1/predictions/{pred_id}")

async def cancel(client, pred_id):
    await client.post(f"https://api.replicate.com/v1/predictions/{pred_id}/cancel")
'''
    assert list(_creation_sites(poll_and_cancel, "providers/fake.py")) == []
