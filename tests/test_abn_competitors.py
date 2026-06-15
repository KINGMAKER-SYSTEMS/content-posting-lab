"""
abn_competitors persistence: the intel file (title/hook playbooks) must be written
atomically — fsync before rename — so a crash mid-write can't leave a truncated file
that corrupts the live narrator/titler in the post-render path.
"""
import json

import pytest

import services.abn_competitors as ac


@pytest.fixture
def intel(tmp_path, monkeypatch):
    f = tmp_path / "competitor_intel.json"
    monkeypatch.setattr(ac, "INTEL_FILE", f)
    # No network: stub the scraper with a deterministic payload.
    monkeypatch.setattr(ac, "_scrape_channel", lambda h, n=8: [{"channel": h, "title": f"{h} headline"}])
    return f


def test_refresh_writes_complete_valid_json(intel):
    blob = ac.refresh(force=True)
    # File on disk parses cleanly and matches the returned blob (no truncation).
    on_disk = json.loads(intel.read_text())
    assert on_disk == blob
    assert on_disk["videos"], "videos should be populated"
    assert on_disk["findings"]["title_formula"] == ac.FINDINGS["title_formula"]


def test_refresh_leaves_no_tmp_file(intel):
    ac.refresh(force=True)
    # atomic_save renames the tmp into place; nothing partial should linger.
    leftovers = list(intel.parent.glob("*.tmp"))
    assert leftovers == [], f"stray tmp file(s) survived: {leftovers}"


def test_playbooks_read_back_from_disk(intel):
    ac.refresh(force=True)
    assert "headline" in ac.title_playbook()
    assert ac.FINDINGS["hook_rule"] in ac.hook_playbook()


def test_uses_atomic_save_for_fsync(intel, monkeypatch):
    # Guard the gate: refresh() must go through the fsync-before-rename helper,
    # not a raw write_text that can truncate on a crash.
    calls = {}

    def fake_save(path, data, **kw):
        calls["path"] = path
        calls["data"] = data

    monkeypatch.setattr(ac, "atomic_save", fake_save)
    blob = ac.refresh(force=True)
    assert calls.get("path") == intel
    assert calls.get("data") == blob
