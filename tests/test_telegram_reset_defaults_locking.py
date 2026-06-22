"""Ticket: reset_default_posters bypassed the config lock (TOCTOU).

The /posters/reset-defaults endpoint did its own bare
load_config() -> mutate -> save_config(), outside the per-path lock that
services.telegram.mutate_config() holds across the whole transaction. A
concurrent setter (bot handler thread, another HTTP request) could load the
same config, the reset could save its seeded version on top, and the
concurrent setter's change would be clobbered (lost update) -- the exact race
the service-layer lock exists to close.

The fix wraps the seed in mutate_config(). These tests hammer the endpoint
against a concurrent setter from many threads and assert no update is lost in
either direction.
"""

import asyncio
import threading

import pytest

from services import json_store
from services import telegram as tg
from routers.telegram import reset_default_posters


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point services.telegram at a throwaway config and a fresh lock registry."""
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    return tmp_path


def _call_reset():
    """Run the async endpoint to completion from a worker thread."""
    return asyncio.run(reset_default_posters())


def test_reset_default_posters_seeds_under_lock(isolated_config):
    """Happy path: the endpoint re-seeds missing defaults and reports the count."""
    # Start from a config that has lost its posters (the case reset exists for):
    # write an empty config so load_config() returns it instead of auto-seeding.
    tg.save_config(tg._empty_config())
    assert tg.list_posters() == []

    result = _call_reset()
    assert result["ok"] is True
    assert result["added"] == len(tg._DEFAULT_POSTERS)
    assert result["total"] == len(tg._DEFAULT_POSTERS)

    # Re-running is idempotent: no default re-added, custom posters untouched.
    tg.set_poster("custom", {"name": "Custom", "chat_id": -1})
    result = _call_reset()
    assert result["added"] == 0
    assert tg.get_poster("custom") is not None


def test_reset_does_not_clobber_concurrent_setter(isolated_config):
    """A setter racing the reset must not lose its write, and vice versa.

    Without the lock the reset's load-modify-save and the concurrent
    set_poster/add_sound could interleave so the last writer wins, dropping the
    other's change. Under mutate_config the two transactions serialize.
    """
    n = 30
    # n add_poster threads + 1 reset thread + the main thread.
    barrier = threading.Barrier(n + 2)
    errors = []

    def add_poster(i):
        barrier.wait()
        try:
            tg.set_poster(f"x{i}", {"name": f"X{i}", "chat_id": -i})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reset():
        barrier.wait()
        try:
            _call_reset()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=add_poster, args=(i,)) for i in range(n)]
    threads.append(threading.Thread(target=reset))
    for t in threads:
        t.start()
    barrier.wait()  # release everyone at once to maximize overlap
    for t in threads:
        t.join()

    assert not errors, f"worker raised: {errors[:3]}"

    # Every concurrent custom poster survived...
    for i in range(n):
        assert tg.get_poster(f"x{i}") is not None, f"lost custom poster x{i}"
    # ...and the reset's defaults all landed too.
    for pid in tg._DEFAULT_POSTERS:
        assert tg.get_poster(pid) is not None, f"lost default poster {pid}"
