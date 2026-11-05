"""Operator kill switch and no-burst-at-boot for the generated-media retention loop."""

import routers.control_plane as cp


def _count_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "_generated_volume_pressure", lambda: "normal")
    monkeypatch.setattr(cp, "run_generated_media_retention_once", lambda **kw: calls.append(kw))
    return calls


def test_off_pauses_every_pass(monkeypatch):
    calls = _count_runs(monkeypatch)
    for value in ("off", "OFF", " Off "):
        monkeypatch.setenv("CONTENT_LAB_MEDIA_RETENTION", value)
        assert cp._media_retention_tick(float("-inf"), 10_000.0) == float("-inf")
    assert calls == []


def test_unset_or_other_values_run_normally(monkeypatch):
    calls = _count_runs(monkeypatch)
    for value in (None, "", "on", "of", "false"):
        if value is None:
            monkeypatch.delenv("CONTENT_LAB_MEDIA_RETENTION", raising=False)
        else:
            monkeypatch.setenv("CONTENT_LAB_MEDIA_RETENTION", value)
        assert cp._media_retention_tick(float("-inf"), 10_000.0) == 10_000.0
    assert len(calls) == 5


def test_first_pass_waits_one_interval_after_boot(monkeypatch):
    calls = _count_runs(monkeypatch)
    monkeypatch.delenv("CONTENT_LAB_MEDIA_RETENTION", raising=False)
    boot = 5_000.0
    interval = cp._MEDIA_RETENTION_INTERVAL_SECONDS["normal"]
    assert cp._media_retention_tick(boot, boot + 1) == boot
    assert cp._media_retention_tick(boot, boot + interval - 1) == boot
    assert calls == []
    assert cp._media_retention_tick(boot, boot + interval) == boot + interval
    assert len(calls) == 1


def test_loop_starts_its_clock_at_boot_not_minus_infinity():
    import inspect
    src = inspect.getsource(cp._media_retention_loop)
    assert "last_run = time.monotonic()" in src
    assert 'float("-inf")' not in src
