"""Tests for the deduplicated abn_memory import helpers in services/abn_factory.py.

The 11 copy-pasted `try: import services.abn_memory ... except: <default>` blocks were
collapsed into `_safe_mem_import()` (module_or_None, is_available) and the
`_mem_episodes()` convenience used for every rotation modulo. These pin the
graceful-degradation contract: when the optional ledger is missing OR raises, callers
get the safe default — never an exception.
"""
import sys
import types

from services import abn_factory


def test_safe_mem_import_returns_module_when_available():
    mem, ok = abn_factory._safe_mem_import()
    # abn_memory ships in the repo, so it imports cleanly.
    assert ok is True
    assert mem is not None
    assert hasattr(mem, "stats")


def test_safe_mem_import_degrades_when_module_missing(monkeypatch):
    """If the ledger module can't be imported, callers get (None, False) — not an exception."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def boom(name, *a, **k):
        if name == "services.abn_memory":
            raise ImportError("simulated missing ledger")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", boom)
    mem, ok = abn_factory._safe_mem_import()
    assert mem is None
    assert ok is False


def test_mem_episodes_returns_int():
    n = abn_factory._mem_episodes()
    assert isinstance(n, int)
    assert n >= 0


def test_mem_episodes_zero_when_import_missing(monkeypatch):
    monkeypatch.setattr(abn_factory, "_safe_mem_import", lambda: (None, False))
    assert abn_factory._mem_episodes() == 0


def test_mem_episodes_zero_when_stats_raises(monkeypatch):
    """A live module whose stats() throws still degrades to 0, not a crash."""
    fake = types.SimpleNamespace(stats=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(abn_factory, "_safe_mem_import", lambda: (fake, True))
    assert abn_factory._mem_episodes() == 0


def test_mem_episodes_reads_episode_count(monkeypatch):
    fake = types.SimpleNamespace(stats=lambda: {"episodes": 42})
    monkeypatch.setattr(abn_factory, "_safe_mem_import", lambda: (fake, True))
    assert abn_factory._mem_episodes() == 42


def test_mem_recent_false_when_ledger_missing(monkeypatch):
    monkeypatch.setattr(abn_factory, "_safe_mem_import", lambda: (None, False))
    assert abn_factory.mem_recent("anything", 100) is False


def test_is_recently_used_safe_false_when_ledger_missing(monkeypatch):
    monkeypatch.setattr(abn_factory, "_safe_mem_import", lambda: (None, False))
    assert abn_factory._is_recently_used_safe("anything") is False
