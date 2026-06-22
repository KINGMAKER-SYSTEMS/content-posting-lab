"""Auto-publish health metric tests for services/abn_factory.py.

The auto-publish loop imports services/abn_youtube.py and, when it is absent (broken deploy or
unimplemented feature), used to surface the breakage exactly ONCE on the ephemeral event BUS —
production-blind, because a dashboard that polls later sees nothing. `_probe_auto_publish` now
also writes a PERSISTENT health record to STATE["auto_publish"] (the dict returned by
GET /factory/state) so the broken state is visible on every poll, and clears it on recovery.
"""
import builtins
import time

import pytest

from services import abn_factory


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset the auto-publish bits of STATE around each test (it is module-global)."""
    saved = {k: abn_factory.STATE.get(k) for k in ("auto_publish", "_ytmod_warned")}
    abn_factory.STATE.pop("auto_publish", None)
    abn_factory.STATE.pop("_ytmod_warned", None)
    yield
    for k, v in saved.items():
        if v is None:
            abn_factory.STATE.pop(k, None)
        else:
            abn_factory.STATE[k] = v


def _force_module_missing(monkeypatch):
    """Make `import services.abn_youtube` raise ModuleNotFoundError regardless of disk state."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "services.abn_youtube":
            raise ModuleNotFoundError("No module named 'services.abn_youtube'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_missing_module_records_persistent_broken_metric(monkeypatch):
    _force_module_missing(monkeypatch)

    mod = abn_factory._probe_auto_publish(3)

    assert mod is None
    health = abn_factory.STATE["auto_publish"]
    assert health["ok"] is False
    assert health["pending_episodes"] == 3
    assert "abn_youtube" in health["reason"]
    # Persistent fields a dashboard can render: when it broke + when last checked.
    assert isinstance(health["since"], float)
    assert isinstance(health["checked"], float)


def test_broken_metric_survives_across_cycles_and_keeps_since(monkeypatch):
    _force_module_missing(monkeypatch)

    abn_factory._probe_auto_publish(1)
    first_since = abn_factory.STATE["auto_publish"]["since"]
    assert abn_factory.STATE["_ytmod_warned"] is True

    time.sleep(0.01)
    abn_factory._probe_auto_publish(5)  # next cycle, more episodes piled up
    health = abn_factory.STATE["auto_publish"]

    # Still broken and visible — NOT silently swallowed after the one-time BUS warning.
    assert health["ok"] is False
    assert health["pending_episodes"] == 5          # current backlog reflected
    assert health["since"] == first_since           # outage start is stable
    assert health["checked"] > first_since          # liveness ticks each poll


def test_one_time_bus_warning_only_fires_once_while_broken(monkeypatch):
    _force_module_missing(monkeypatch)
    events = []
    monkeypatch.setattr(abn_factory.BUS, "emit",
                        lambda *a, **k: events.append((a, k)))

    abn_factory._probe_auto_publish(2)
    abn_factory._probe_auto_publish(2)
    abn_factory._probe_auto_publish(2)

    unavailable = [e for e in events if e[0][:2] == ("publisher", "unavailable")]
    assert len(unavailable) == 1  # spam-guarded; metric carries the persistent signal


def test_recovery_clears_metric_and_rearms_warning(monkeypatch):
    # Start broken.
    _force_module_missing(monkeypatch)
    abn_factory._probe_auto_publish(4)
    assert abn_factory.STATE["auto_publish"]["ok"] is False
    assert abn_factory.STATE["_ytmod_warned"] is True

    # Module appears (deploy fixed): real import now succeeds.
    monkeypatch.undo()
    mod = abn_factory._probe_auto_publish(4)

    assert mod is not None
    assert abn_factory.STATE["auto_publish"]["ok"] is True
    # Warning re-armed so the NEXT outage alerts again.
    assert abn_factory.STATE["_ytmod_warned"] is False


def test_healthy_path_returns_module_when_present():
    # In this worktree services/abn_youtube.py exists, so the live import must succeed.
    mod = abn_factory._probe_auto_publish(0)
    assert mod is not None
    assert hasattr(mod, "is_configured")
    assert abn_factory.STATE["auto_publish"]["ok"] is True
