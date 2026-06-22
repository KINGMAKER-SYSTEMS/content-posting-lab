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


def test_assign_pages_to_poster_is_idempotent_and_atomic(isolated_config):
    """The batched assign adds every page once, even with overlapping inputs.

    The router used to loop ``assign_page_to_poster`` per page (lock taken and
    released between each), leaving a window for a concurrent op to interleave.
    The batched helper does the whole list under one ``mutate_config`` hold.
    """
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})

    poster = tg.assign_pages_to_poster("p1", ["a", "b", "c"])
    assert poster["page_ids"] == ["a", "b", "c"]

    # Re-assigning overlapping pages must not duplicate.
    poster = tg.assign_pages_to_poster("p1", ["b", "c", "d"])
    assert poster["page_ids"] == ["a", "b", "c", "d"]


def test_concurrent_assign_batches_lose_nothing(isolated_config):
    """N threads each assign a distinct BATCH of pages to the same poster.

    This mirrors the real race: multiple assign_pages requests landing at once.
    Every page across every batch must survive — none clobbered by last-writer.
    """
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})

    n = 20
    batch_size = 5
    _barrier_runner(
        lambda i: tg.assign_pages_to_poster(
            "p1", [f"b{i}-p{j}" for j in range(batch_size)]
        ),
        n,
    )

    page_ids = set(tg.get_poster("p1")["page_ids"])
    expected = {f"b{i}-p{j}" for i in range(n) for j in range(batch_size)}
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


# --- converted setters: now go through mutate_config (was lock_for+save) -----


def test_concurrent_set_staging_topic_loses_nothing(isolated_config):
    """N threads each map a distinct integration to a staging topic.

    set_staging_topic used the unsafe lock_for + load + save pattern; it now
    runs under mutate_config. Every append-only mapping must survive.
    """
    n = 40
    _barrier_runner(
        lambda i: tg.set_staging_topic(f"page-{i}", topic_id=i, topic_name=f"t{i}"),
        n,
    )
    topics = tg.get_staging_group()["topics"]
    assert set(topics) == {f"page-{i}" for i in range(n)}
    assert topics["page-5"]["topic_id"] == 5


def test_concurrent_add_song_to_page_loses_nothing(isolated_config):
    """N threads append distinct sounds to the SAME page playlist; none dropped.

    add_song_to_page was a lock_for + load + save setter; now mutate_config.
    """
    ids = [tg.add_sound(f"http://s/{i}", f"L{i}")["id"] for i in range(40)]
    _barrier_runner(lambda i: tg.add_song_to_page("page-x", ids[i]), len(ids))
    playlist = set(tg.get_page_playlist("page-x"))
    assert playlist == set(ids), f"lost {set(ids) - playlist}"


def test_set_staging_topic_skips_write_when_not_forced(isolated_config):
    """Append-only: an existing mapping is not overwritten without force.

    Under mutate_config the save still runs on block exit, but it must persist
    the UNCHANGED config (the early return leaves config untouched).
    """
    tg.set_staging_topic("page-1", topic_id=1, topic_name="first")
    tg.set_staging_topic("page-1", topic_id=2, topic_name="second")  # no force
    topics = tg.get_staging_group()["topics"]
    assert topics["page-1"]["topic_id"] == 1
    # force overrides
    tg.set_staging_topic("page-1", topic_id=2, topic_name="second", force=True)
    assert tg.get_staging_group()["topics"]["page-1"]["topic_id"] == 2


def test_set_last_forwarded_id_persists(isolated_config):
    """set_last_forwarded_id (converted setter) writes through to disk."""
    tg.set_staging_topic("page-1", topic_id=1, topic_name="first")
    tg.set_last_forwarded_id("page-1", 123)
    assert tg.get_last_forwarded_id("page-1") == 123


def test_converted_setter_skips_save_on_exception(isolated_config, monkeypatch):
    """A converted setter that raises mid-block must not persist its partial work.

    Proves the lock_for->mutate_config swap inherited mutate_config's
    no-save-on-exception guarantee. We inject a failure after the in-block
    mutation of set_poster_topic by making _now() raise.
    """
    tg.set_poster("p1", {"name": "One", "chat_id": -1})

    real_now = tg._now

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(tg, "_now", boom)
    with pytest.raises(RuntimeError):
        tg.set_poster_topic("p1", "page-9", topic_id=9, topic_name="t9")

    # restore _now only (keep the isolated CONFIG_PATH patch intact)
    monkeypatch.setattr(tg, "_now", real_now)
    # the topic mutation happened before _now() in the block, but the whole
    # transaction must have been discarded (no save on exception)
    assert "page-9" not in tg.get_poster("p1").get("topics", {})


# --- write-failure (not just TOCTOU) regressions for the config layer -------


def test_save_failure_does_not_corrupt_config_on_disk(isolated_config, monkeypatch):
    """If the atomic write fails mid-flight, the last-good config survives.

    The locking tests above cover lost *content* updates; this covers a *write*
    failure (e.g. fsync/replace erroring on the volume). A failed save must raise
    and leave the prior on-disk config intact and loadable — never truncated.
    """
    tg.set_poster("p1", {"name": "One", "chat_id": -1})

    real_fsync = json_store.os.fsync
    fail = {"on": True}

    def maybe_boom(fd):
        if fail["on"]:
            raise OSError("simulated fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(json_store.os, "fsync", maybe_boom)
    with pytest.raises(OSError):
        tg.set_poster("p2", {"name": "Two", "chat_id": -2})

    # restore fsync (config path patch stays), reload from disk:
    # p1 still there, p2's failed write left no trace
    fail["on"] = False
    config = tg.load_config()
    assert config["posters"]["p1"]["name"] == "One"
    assert "p2" not in config["posters"]
    assert not list(isolated_config.parent.glob("*.tmp")), "tmp leaked after failed save"
