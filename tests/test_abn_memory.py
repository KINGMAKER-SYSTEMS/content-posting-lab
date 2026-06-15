"""
Guards abn_memory's persistence: writes must go through the shared atomic
writer (tmp + flush + fsync + os.replace) so a kernel crash mid-write can't
truncate abn_memory.json and lose the self-refinement flywheel's history.
"""
import json

import pytest

from services import abn_memory


@pytest.fixture
def mem_path(tmp_path, monkeypatch):
    p = tmp_path / "abn_memory.json"
    monkeypatch.setattr(abn_memory, "MEM_PATH", p)
    return p


def test_save_uses_atomic_writer(mem_path, monkeypatch):
    """_save must route through json_store.atomic_save (the fsync'd path)."""
    called = {}

    real = abn_memory.atomic_save

    def spy(path, data):
        called["path"] = path
        called["data"] = data
        return real(path, data)

    monkeypatch.setattr(abn_memory, "atomic_save", spy)
    abn_memory._save({"episodes": [], "topic_counts": {}, "seen_titles": [], "approved_theses": []})

    assert called["path"] == mem_path
    # no orphaned tmp file, and the result is valid readable JSON
    assert not (mem_path.parent / "abn_memory.json.tmp").exists()
    assert json.loads(mem_path.read_text())["episodes"] == []


def test_record_episode_round_trips(mem_path):
    abn_memory.record_episode("ep1", ["Anthropic ships Claude agents SDK"], thesis="agents win",
                              approved=True, rendered=True)
    on_disk = json.loads(mem_path.read_text())
    assert on_disk["episodes"][0]["ep_id"] == "ep1"
    assert on_disk["seen_titles"], "rendered episode should record a seen title"
    # freshness ledger reads it back
    assert abn_memory.is_recently_used("Anthropic ships Claude agents SDK") is True
    assert abn_memory.stats()["episodes"] == 1


def test_load_falls_back_on_corrupt_file(mem_path):
    mem_path.write_text("{ this is not json")
    m = abn_memory._load()
    assert m == {"episodes": [], "topic_counts": {}, "seen_titles": [], "approved_theses": []}


def test_load_missing_file_returns_default(mem_path):
    assert not mem_path.exists()
    assert abn_memory._load()["episodes"] == []
