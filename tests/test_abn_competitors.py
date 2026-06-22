"""
abn_competitors persistence: the intel file (title/hook playbooks) must be written
atomically — fsync before rename — so a crash mid-write can't leave a truncated file
that corrupts the live narrator/titler in the post-render path.
"""
import json

import pytest

import services.abn_competitors as ac
from services import abn_competitors as comp


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


def test_topic_signals_returns_titles_from_cache(intel):
    # Populate the intel cache, then the SCOUT signal must be the cached titles.
    ac.refresh(force=True)
    signals = ac.topic_signals()
    assert signals  # not empty
    assert all(isinstance(s, str) for s in signals)
    # Every competitor's stubbed headline shows up as a bare title string.
    assert f"{ac.COMPETITORS[0]} headline" in signals
    assert len(signals) == len(ac.COMPETITORS)


def test_topic_signals_respects_limit(intel):
    ac.refresh(force=True)
    # limit caps how many demand signals the scout sees.
    assert len(ac.topic_signals(limit=2)) == 2
    assert ac.topic_signals(limit=0) == []


def test_topic_signals_empty_when_no_cache(tmp_path, monkeypatch):
    # No intel file on disk → _load() falls back to static FINDINGS (no videos),
    # so the scout gets an empty signal list rather than crashing.
    missing = tmp_path / "competitor_intel.json"
    monkeypatch.setattr(ac, "INTEL_FILE", missing)
    assert ac.topic_signals() == []


def test_scrape_channel_swallows_failure(monkeypatch):
    # yt-dlp blowing up (timeout, missing binary, network) must degrade to []
    # per-channel — never propagate and wedge a refresh.
    def boom(*a, **kw):
        raise OSError("yt-dlp not found")

    monkeypatch.setattr(ac.subprocess, "run", boom)
    assert ac._scrape_channel("AnyChannel") == []


def test_scrape_channel_parses_and_skips_blank_lines(monkeypatch):
    # stdout lines become {channel,title} dicts; blank/whitespace lines are dropped.
    class _Done:
        stdout = "First Title\n\n  Second Title  \n   \n"

    monkeypatch.setattr(ac.subprocess, "run", lambda *a, **kw: _Done())
    rows = ac._scrape_channel("Chan")
    assert rows == [
        {"channel": "Chan", "title": "First Title"},
        {"channel": "Chan", "title": "Second Title"},
    ]


def test_refresh_survives_all_channels_failing(intel, monkeypatch):
    # If EVERY channel scrape fails, refresh() still returns a well-formed blob
    # (empty videos, intact findings) and persists it — no exception escapes.
    monkeypatch.setattr(ac, "_scrape_channel", lambda h, n=8: [])
    blob = ac.refresh(force=True)
    assert blob["videos"] == []
    assert blob["findings"] == ac.FINDINGS
    assert blob["channels"] == ac.COMPETITORS
    # downstream scout signal is empty but does not raise.
    assert ac.topic_signals() == []
    # and the (empty) blob still round-trips from disk.
    assert json.loads(intel.read_text()) == blob


def test_refresh_partial_channel_failure_keeps_good_videos(intel, monkeypatch):
    # One channel dies, the rest succeed → refresh keeps the survivors' titles
    # instead of throwing the whole batch away.
    monkeypatch.setattr(ac, "COMPETITORS", ["good", "bad"])

    def scrape(h, n=8):
        if h == "bad":
            return []  # _scrape_channel already converts its own failure to []
        return [{"channel": h, "title": f"{h} winner"}]

    monkeypatch.setattr(ac, "_scrape_channel", scrape)
    blob = ac.refresh(force=True)
    assert blob["videos"] == [{"channel": "good", "title": "good winner"}]
    assert "good winner" in ac.topic_signals()


def test_refresh_logs_warning_when_save_fails(intel, monkeypatch, caplog):
    # atomic_save blowing up must no longer be silently swallowed: callers get the
    # in-memory blob back, but a WARNING is emitted so a stale/missing intel file
    # (corrupt titles + cold-opens downstream) is diagnosable.
    def boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ac, "atomic_save", boom)
    with caplog.at_level("WARNING", logger=ac.log.name):
        blob = ac.refresh(force=True)
    # still returns a well-formed blob so callers degrade gracefully...
    assert blob["videos"]
    # ...and the failure is logged, not swallowed.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "save failure must emit a WARNING"
    assert any("save failed" in r.getMessage() for r in warnings)
    # exc_info is attached so the traceback is visible in the log.
    assert any(r.exc_info for r in warnings)


def test_load_routes_through_atomic_load(intel, monkeypatch):
    # _load() must read via the shared atomic_load util (specific exception handling),
    # not a bare json.loads/except that swallows every error including real bugs.
    seen = {}

    def _spy(path, *, default=None):
        seen["path"] = path
        seen["default"] = default
        return {"videos": [{"channel": "X", "title": "spied"}], "findings": ac.FINDINGS}

    monkeypatch.setattr(ac, "atomic_load", _spy)
    out = ac._load()
    assert seen["path"] == intel
    # missing/corrupt falls back to the static FINDINGS, never crashes downstream.
    assert seen["default"] == {"videos": [], "findings": ac.FINDINGS}
    assert out["videos"] == [{"channel": "X", "title": "spied"}]


def test_load_degrades_on_corrupt_intel_file(tmp_path, monkeypatch):
    # A truncated/garbage intel file must degrade to the static FINDINGS via atomic_load,
    # so the titler/narrator still get usable rules instead of an exception.
    bad = tmp_path / "competitor_intel.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(ac, "INTEL_FILE", bad)
    assert ac._load() == {"videos": [], "findings": ac.FINDINGS}
    assert ac.topic_signals() == []
    assert ac.title_playbook() == ""
    assert ac.FINDINGS["hook_rule"] in ac.hook_playbook()


def test_refresh_ignores_corrupt_cache_and_rescrapes(intel):
    # A corrupt cache must not short-circuit refresh(): atomic_load returns the {} default,
    # {}.get("ts") is falsy, so refresh falls through to a fresh scrape (force not needed).
    intel.write_text("garbage{", encoding="utf-8")
    blob = ac.refresh(force=False)
    assert blob["videos"], "corrupt cache should be ignored and channels re-scraped"
    assert json.loads(intel.read_text()) == blob


def test_refresh_writes_intel_atomically_via_shared_store(tmp_path, monkeypatch):
    """refresh() must persist competitor_intel.json through the shared atomic_save
    util (tmp + fsync + replace) so a crash mid-write can't truncate the intel cache."""
    intel = tmp_path / "competitor_intel.json"
    monkeypatch.setattr(comp, "INTEL_FILE", intel)
    monkeypatch.setattr(comp, "COMPETITORS", ["ChannelA"])
    monkeypatch.setattr(
        comp, "_scrape_channel", lambda h, n=8: [{"channel": h, "title": "A real winner"}]
    )

    calls = {}

    def _spy(path, data, **kwargs):
        calls["path"] = path
        from services.json_store import atomic_save as real
        real(path, data, **kwargs)

    monkeypatch.setattr(comp, "atomic_save", _spy)

    blob = comp.refresh(force=True)

    # routed through the shared atomic util, no stray tmp left behind...
    assert calls["path"] == intel
    assert not list(tmp_path.glob("*.tmp"))
    # ...and the file round-trips to valid, complete JSON.
    on_disk = json.loads(intel.read_text())
    assert on_disk["videos"] == [{"channel": "ChannelA", "title": "A real winner"}]
    assert on_disk == blob
