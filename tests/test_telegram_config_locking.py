"""Concurrency regression tests for the telegram_config load-modify-save race.

Every setter in ``services/telegram.py`` does load_config() -> mutate -> save_config().
``atomic_save`` only atomizes the *write*, not the read-modify-write transaction,
so two concurrent callers could each load the same config, apply their own change,
and the last writer would clobber the other's update (TOCTOU lost update). This is
reachable because the setters run as sync calls from async routers
(``routers/pipeline.py`` calls ``set_poster_topic`` from async context, FastAPI runs
sync work in a threadpool, the bot runs on its own thread).

The fix serializes each transaction under a per-path reentrant lock
(``json_store.lock_for`` + ``telegram.mutate_config``). These tests hammer the
setters from many threads and assert no update is lost.
"""

import threading

import pytest

from services import json_store
from services import telegram as tg


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point services.telegram at a throwaway config and a fresh lock registry."""
    path = tmp_path / "telegram_config.json"
    monkeypatch.setattr(tg, "CONFIG_PATH", path)
    # Fresh lock table so a lock from a prior test/path can't interfere.
    monkeypatch.setattr(json_store, "_LOCKS", {})
    return path


def _barrier_runner(fn, n_threads):
    """Run ``fn(i)`` on ``n_threads`` threads released simultaneously to maximize
    the overlap window of the read-modify-write."""
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker(i):
        barrier.wait()  # all threads load the config at ~the same instant
        try:
            fn(i)
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"worker raised: {errors[:3]}"


def test_concurrent_assign_page_to_poster_loses_nothing(isolated_config):
    """N threads each append a distinct page to the SAME poster's page_ids.

    This is the canonical lost-update case: a list append under load-modify-save.
    Without the lock, last-writer-wins drops most of the appends.
    """
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})

    n = 40
    _barrier_runner(lambda i: tg.assign_page_to_poster("p1", f"page-{i}"), n)

    poster = tg.get_poster("p1")
    page_ids = set(poster["page_ids"])
    expected = {f"page-{i}" for i in range(n)}
    assert page_ids == expected, f"lost {expected - page_ids}"


def test_concurrent_set_poster_topic_loses_nothing(isolated_config):
    """N threads each map a distinct integration to a topic on the same poster.

    Mirrors routers/pipeline.py calling set_poster_topic from async context.
    Every mapping must survive.
    """
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})

    n = 40
    _barrier_runner(
        lambda i: tg.set_poster_topic("p1", f"page-{i}", topic_id=i, topic_name=f"t{i}"),
        n,
    )

    topics = tg.get_poster("p1")["topics"]
    assert set(topics) == {f"page-{i}" for i in range(n)}
    assert topics["page-7"]["topic_id"] == 7


def test_concurrent_add_inventory_loses_nothing(isolated_config):
    """N threads append inventory items to the same page list; all must land."""
    n = 40
    _barrier_runner(
        lambda i: tg.add_inventory_item("page-x", {"caption": f"c{i}"}),
        n,
    )
    items = tg.get_inventory("page-x")
    captions = {it["caption"] for it in items}
    assert captions == {f"c{i}" for i in range(n)}
    assert len(items) == n  # no item dropped, none duplicated


def test_concurrent_add_sound_loses_nothing(isolated_config):
    """N threads add sounds to the shared sounds[] list; none may be clobbered."""
    n = 40
    _barrier_runner(lambda i: tg.add_sound(f"http://s/{i}", f"L{i}"), n)
    sounds = tg.list_sounds(active_only=False)
    assert {s["label"] for s in sounds} == {f"L{i}" for i in range(n)}


def test_mutate_config_saves_on_exit(isolated_config):
    """mutate_config persists in-place mutations made inside the block."""
    with tg.mutate_config() as config:
        config["bot_username"] = "rt_bot"
    assert tg.load_config()["bot_username"] == "rt_bot"


def test_mutate_config_skips_save_on_exception(isolated_config):
    """If the block raises, the partial mutation is NOT written to disk."""
    tg.set_poster("p1", {"name": "One", "chat_id": -1})
    with pytest.raises(RuntimeError):
        with tg.mutate_config() as config:
            config["posters"]["p1"]["name"] = "MUTATED"
            raise RuntimeError("boom")
    # load fresh from disk: the mutation must not have been saved
    assert tg.load_config()["posters"]["p1"]["name"] == "One"


def test_lock_for_is_reentrant_same_path(isolated_config):
    """A nested transaction on the same path must not self-deadlock (RLock)."""
    lock = json_store.lock_for(tg.CONFIG_PATH)
    with lock:
        with lock:  # would hang forever on a plain Lock
            pass
    # same path returns the same lock object
    assert json_store.lock_for(tg.CONFIG_PATH) is lock


def test_lock_for_distinct_paths_distinct_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(json_store, "_LOCKS", {})
    a = json_store.lock_for(tmp_path / "a.json")
    b = json_store.lock_for(tmp_path / "b.json")
    assert a is not b
