"""The archive index has ONE reader: services.job_store_compaction.read_archive_index.

Rules (the same ones compact_job_store enforces): a missing key means nothing
has been archived yet; a dict is the index; anything else, null included, is
unreadable and refuses. Every other reader used to clean a bad value into {}
on its own, which read unknown history as "nothing used" (PR #389 reviews).
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routers import control_plane as cp
from services import job_store_compaction as compaction
from services.json_store import atomic_save

ROOT = Path(__file__).resolve().parents[1]
KEY = compaction.ARCHIVE_INDEX_KEY
BAD = [None, [], "", 0, False, ["x"], "index", 1]
ALLOWED = {("services/job_store_compaction.py", "read_archive_index"),
           ("services/job_store_compaction.py", "carry_archive_index")}


def _is_key(node):
    return (
        (isinstance(node, ast.Name) and node.id == "ARCHIVE_INDEX_KEY")
        or (isinstance(node, ast.Attribute) and node.attr == "ARCHIVE_INDEX_KEY")
        or (isinstance(node, ast.Constant) and node.value == KEY)
    )


def direct_reads(source, path="<snippet>"):
    """(path, function, line) for every read of the archive index value."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Subscript) and isinstance(child.ctx, ast.Load) and _is_key(child.slice):
                found.append((path, name, child.lineno))
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in {"get", "pop", "setdefault"}
                    and child.args and _is_key(child.args[0])):
                found.append((path, name, child.lineno))
            visit(child, name)

    visit(ast.parse(source), None)
    return found


def test_the_guard_sees_a_direct_read():
    # Positive control: without this the scan below could pass by seeing nothing.
    snippet = (
        "def f(store):\n"
        "    a = store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}\n"
        "    b = store['archiveIndex']\n"
        "    c = view[ARCHIVE_INDEX_KEY]\n"
        "    store[ARCHIVE_INDEX_KEY] = {}\n"
        "    return ARCHIVE_INDEX_KEY in store\n"
    )
    assert [line for _, _, line in direct_reads(snippet)] == [2, 3, 4]


def test_no_code_reads_the_archive_index_except_the_one_reader():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".")) or "/." in rel or "node_modules" in rel or "site-packages" in rel:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "ARCHIVE_INDEX_KEY" not in source and KEY not in source:
            continue
        offenders += [hit for hit in direct_reads(source, rel) if hit[:2] not in ALLOWED]
    assert offenders == [], f"read the archive index through compaction.read_archive_index: {offenders}"


def test_reader_rules():
    assert compaction.read_archive_index({"jobs": {}}) == {}
    index = {"version": 1, "slideshow": []}
    assert compaction.read_archive_index({KEY: index}) is index
    for value in BAD:
        with pytest.raises(compaction.ArchiveIndexUnreadable):
            compaction.read_archive_index({KEY: value})


@pytest.mark.parametrize("value", BAD)
def test_compaction_and_reader_agree(value):
    with pytest.raises(compaction.ArchiveIndexUnreadable):
        compaction.compact_job_store({"jobs": {}, "byIdempotency": {}, KEY: value},
                                     compaction.datetime.now(compaction.timezone.utc))


def _snapshot(value):
    data = {"jobs": {}, "byIdempotency": {}}
    if value is not Ellipsis:
        data[KEY] = value
    return cp._JobsSnapshot((0, 0, 0, 0), 1, data, {}, {})


@pytest.mark.parametrize("value", BAD)
def test_capability_views_pass_the_index_on_untouched(value):
    snapshot = _snapshot(value)
    page_view, dna_view = cp._capabilities_job_views(snapshot, "acct:p")
    truck_view = cp._generation_related_jobs_view(snapshot, page_view, "acct:p")
    for view in (page_view, dna_view, truck_view):
        assert view[KEY] is value
    page_view, dna_view = cp._capabilities_job_views(_snapshot(...), "acct:p")
    assert KEY not in page_view and KEY not in dna_view


@pytest.mark.parametrize("value", BAD)
def test_every_reservation_helper_refuses_unknown_history(value):
    store = {"jobs": {}, KEY: value}
    slide = SimpleNamespace(library_id="lib")
    source = SimpleNamespace(masters=[], source_library_id="lib", source_library_hash="h")
    for call in (
        lambda: cp._slideshow_unavailable_signatures(store, slide, "acct:p"),
        lambda: cp._generic_clean_video_reserved_media(store, slide, "acct:p"),
        lambda: cp._source_dna_unavailable_slots(store, source, "rv1"),
    ):
        with pytest.raises(compaction.ArchiveIndexUnreadable):
            call()


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    cp._capabilities_cache.clear()
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "registry"))
    return path


@pytest.mark.parametrize("value", BAD)
def test_capabilities_refuse_with_503_on_unknown_history(job_path, value):
    atomic_save(job_path, {"jobs": {}, "byIdempotency": {}, KEY: value})
    with pytest.raises(HTTPException) as excinfo:
        cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)
    assert excinfo.value.status_code == 503


def test_capabilities_answer_when_nothing_is_archived(job_path):
    atomic_save(job_path, {"jobs": {}, "byIdempotency": {}})
    cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)
