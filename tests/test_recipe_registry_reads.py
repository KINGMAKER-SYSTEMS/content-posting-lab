"""Repeated capability polls must not reopen the entire recipe registry."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from routers import control_plane_recipes as recipes


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path))
    recipes._recipe_cache.clear()
    return tmp_path


def put(root, name, page="acct:a", **fields):
    record = {"status": "registered", "pageId": page, "nested": {"value": 1}, **fields}
    path = root / f"{name}.json"
    path.write_text(json.dumps(record))
    return path


def test_concurrent_polls_read_each_unchanged_file_once(registry, monkeypatch):
    for i in range(40):
        put(registry, str(i), page=f"acct:{i}")
    original = Path.open
    reads = []
    lock = Lock()

    def counted(path, *args, **kwargs):
        if path.parent == registry:
            with lock:
                reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(recipes.list_registered_recipes, [f"acct:{i % 40}" for i in range(120)]))
    assert all(len(rows) == 1 for rows in results)
    assert len(reads) == 40


def test_add_edit_remove_and_corruption_are_visible_immediately(registry):
    a = put(registry, "a")
    assert len(recipes.list_registered_recipes("acct:a")) == 1
    b = put(registry, "b")
    assert len(recipes.list_registered_recipes("acct:a")) == 2
    put(registry, "a", page="acct:b")
    assert len(recipes.list_registered_recipes("acct:a")) == 1
    assert len(recipes.list_registered_recipes("acct:b")) == 1
    b.unlink()
    assert recipes.list_registered_recipes("acct:a") == []
    a.write_text("{")
    assert recipes.list_registered_recipes("acct:b") == []
    put(registry, "a", page="acct:b")
    assert len(recipes.list_registered_recipes("acct:b")) == 1


def test_replacement_with_same_size_and_mtime_is_not_stale(registry):
    a = put(registry, "a")
    original_stat = a.stat()
    assert len(recipes.list_registered_recipes("acct:a")) == 1
    replacement = put(registry, "replacement", page="acct:b")
    assert replacement.stat().st_size == original_stat.st_size
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(a)
    assert recipes.list_registered_recipes("acct:a") == []
    assert len(recipes.list_registered_recipes("acct:b")) == 1


def test_returned_rows_cannot_mutate_later_reads(registry, monkeypatch):
    put(registry, "a", recipeId="recipe", engine="ai_video", recipeVersion="v1")
    monkeypatch.setattr(recipes, "publication_matches_master_pages", lambda *args: True)
    direct = recipes.list_registered_recipes("acct:a")
    direct[0]["nested"]["value"] = 99
    listed = recipes.list_registered_recipe_bindings("acct:a", {}, "hash")
    assert listed[0][1]["nested"]["value"] == 1
    listed[0][1]["nested"]["value"] = 88
    alias = recipes.load_registered_recipe_binding("acct:alias", "recipe", "ai_video", "v1", {}, "hash")
    assert alias[1]["nested"]["value"] == 1
    alias[1]["nested"]["value"] = 77
    assert recipes.list_registered_recipes("acct:a")[0]["nested"]["value"] == 1


def test_new_ambiguous_alias_is_not_hidden_by_cache(registry, monkeypatch):
    put(registry, "a", recipeId="recipe", engine="ai_video", recipeVersion="v1")
    monkeypatch.setattr(recipes, "publication_matches_master_pages", lambda *args: True)
    args = ("acct:alias", "recipe", "ai_video", "v1", {}, "hash")
    assert recipes.load_registered_recipe_binding(*args) is not None
    put(registry, "b", page="acct:b", recipeId="recipe", engine="ai_video", recipeVersion="v1")
    assert recipes.load_registered_recipe_binding(*args) is None
    assert recipes.list_registered_recipe_bindings("acct:alias", {}, "hash") == []


def test_changed_during_read_is_not_cached(registry, monkeypatch):
    path = put(registry, "a")
    original = json.load

    def changed(handle):
        record = original(handle)
        if handle.name == str(path):
            put(registry, "a", page="acct:b")
        return record

    monkeypatch.setattr(recipes.json, "load", changed)
    assert recipes.list_registered_recipes("acct:a") == []
    monkeypatch.setattr(recipes.json, "load", original)
    assert len(recipes.list_registered_recipes("acct:b")) == 1
