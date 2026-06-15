"""
Guards abn_memory's persistence: writes must go through the shared atomic
writer (tmp + flush + fsync + os.replace) so a kernel crash mid-write can't
truncate abn_memory.json and lose the self-refinement flywheel's history.

This is the same crash-safe write that stopped upload_jobs from being
truncated on a mid-write crash (P0). A raw write_text()+replace has no fsync
and can leave a truncated/empty file.
"""
import json

import pytest

from services import abn_memory


@pytest.fixture
def mem_path(tmp_path, monkeypatch):
    p = tmp_path / "abn_memory.json"
    monkeypatch.setattr(abn_memory, "MEM_PATH", p)
    return p


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Point the module's MEM_PATH at a temp file for the duration of a test."""
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


def test_save_round_trips(mem):
    data = {"episodes": [{"ep_id": "e1"}], "topic_counts": {"agents": 3},
            "seen_titles": [], "approved_theses": ["café"]}
    abn_memory._save(data)
    assert json.loads(mem.read_text(encoding="utf-8")) == data


def test_save_leaves_no_tmp_artifact(mem):
    """Atomic replace must not leave the .tmp scratch file behind."""
    abn_memory._save({"episodes": [], "topic_counts": {},
                      "seen_titles": [], "approved_theses": []})
    leftovers = list(mem.parent.glob("*.tmp"))
    assert leftovers == [], f"atomic write left scratch files: {leftovers}"


def test_save_delegates_to_atomic_save(mem, monkeypatch):
    """_save must route through json_store.atomic_save (fsync path), not a raw write."""
    called = {}

    def fake_atomic_save(path, data, **kwargs):
        called["path"] = path
        called["data"] = data

    monkeypatch.setattr(abn_memory, "atomic_save", fake_atomic_save)
    payload = {"episodes": [], "topic_counts": {}, "seen_titles": [], "approved_theses": []}
    abn_memory._save(payload)
    assert called["path"] == mem
    assert called["data"] == payload


def test_record_episode_persists_via_save(mem):
    """End-to-end: a recorded rendered episode lands in the seen-titles ledger on disk."""
    abn_memory.record_episode("ep1", ["A New Agent Framework Ships"], rendered=True)
    saved = json.loads(mem.read_text(encoding="utf-8"))
    assert any(s["t"] for s in saved["seen_titles"])
    assert abn_memory.is_recently_used("A New Agent Framework Ships") is True


# --- rejection ledger (record_rejection / rejection_penalty) -----------------
# The flywheel learns what to AVOID from rejected episodes. A corrupt or wrongly
# weighted ledger could make the scorer loop bad stories forever, so pin the
# accumulation, stopword filtering, persistence, and crash-safety behaviors.

def test_record_rejection_accumulates_keywords(mem):
    abn_memory.record_rejection(["Replicate pricing surprise"])
    abn_memory.record_rejection(["Replicate outage again"])
    rej = json.loads(mem.read_text(encoding="utf-8"))["rejected_keywords"]
    # "replicate" appears in both rejected episodes -> count 2
    assert rej["replicate"] == 2
    assert rej["pricing"] == 1
    assert rej["outage"] == 1


def test_record_rejection_skips_stopwords_and_short_words(mem):
    abn_memory.record_rejection(["This agent tool with code"])
    rej = json.loads(mem.read_text(encoding="utf-8")).get("rejected_keywords", {})
    # every word here is a stopword or <4 chars after the regex floor -> nothing logged
    assert rej == {}


def test_rejection_penalty_sums_overlapping_keywords(mem):
    abn_memory.record_rejection(["Replicate pricing meltdown"])
    abn_memory.record_rejection(["Replicate latency spike"])
    # "replicate" was rejected twice; "pricing" once -> 2 + 1 = 3
    assert abn_memory.rejection_penalty("Replicate pricing news") == 3
    # unrelated story shares no rejected keywords
    assert abn_memory.rejection_penalty("Lottie animation release") == 0


def test_rejection_penalty_empty_ledger_is_zero(mem):
    assert abn_memory.rejection_penalty("anything at all") == 0


def test_record_rejection_bounds_ledger_to_200_keys(mem):
    # Build a clearly-dominant keyword FIRST (high count), then flood with many
    # distinct count-1 keywords. The trim keeps the top-200 by count, so the
    # high-count keyword must survive while the ledger stays bounded.
    for _ in range(50):
        abn_memory.record_rejection(["dominant"])
    abn_memory.record_rejection([f"keyword{i:04d}" for i in range(250)])
    rej = json.loads(mem.read_text(encoding="utf-8"))["rejected_keywords"]
    assert len(rej) <= 200
    # the most-rejected keyword must survive the count-desc trim
    assert rej["dominant"] == 50


def test_record_rejection_survives_corrupt_ledger(mem):
    """A truncated/corrupt abn_memory.json must not crash the rejection logger;
    _load falls back to a clean default and the rejection still records."""
    mem.write_text("{ this is not json", encoding="utf-8")
    abn_memory.record_rejection(["Replicate pricing surprise"])
    rej = json.loads(mem.read_text(encoding="utf-8"))["rejected_keywords"]
    assert rej["replicate"] == 1


# --- scoring helpers (overexposed_topics / winning_theses) -------------------

def test_overexposed_topics_respects_threshold(mem):
    abn_memory._save({
        "episodes": [], "seen_titles": [], "approved_theses": [],
        "topic_counts": {"agents": 6, "claude": 7, "lottie": 2},
    })
    assert abn_memory.overexposed_topics() == {"agents", "claude"}
    # custom threshold narrows the set (>=7 only)
    assert abn_memory.overexposed_topics(threshold=7) == {"claude"}
    # ...and widens it (>=2 catches lottie too)
    assert abn_memory.overexposed_topics(threshold=2) == {"agents", "claude", "lottie"}


def test_winning_theses_returns_recent_tail(mem):
    abn_memory._save({
        "episodes": [], "seen_titles": [], "topic_counts": {},
        "approved_theses": ["t1", "t2", "t3", "t4", "t5", "t6"],
    })
    # default n=5 -> most recent five, in order
    assert abn_memory.winning_theses() == ["t2", "t3", "t4", "t5", "t6"]
    assert abn_memory.winning_theses(2) == ["t5", "t6"]
    # n larger than the ledger just returns everything (no padding/crash)
    assert abn_memory.winning_theses(99) == ["t1", "t2", "t3", "t4", "t5", "t6"]
