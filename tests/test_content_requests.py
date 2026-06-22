"""Tests for the content-request store (services/content_requests.py).

Focus: the load-modify-save transactions (add_request / update_request) must be
serialized by REQUESTS_PATH's lock so concurrent callers don't clobber each
other's writes — the same TOCTOU guard telegram.mutate_config provides.
"""

import threading

import pytest

from services import content_requests as cr


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the module at a throwaway JSON file on a tmp volume."""
    p = tmp_path / "content_requests.json"
    monkeypatch.setattr(cr, "REQUESTS_PATH", p)
    return p


def test_add_and_get(store):
    entry = cr.add_request("poster1", "need 3 clips", quantity=3)
    assert entry["status"] == "open"
    assert cr.get_request(entry["id"]) == entry


def test_update_request(store):
    entry = cr.add_request("poster1", "x")
    updated = cr.update_request(entry["id"], status="fulfilled", agent_note="done")
    assert updated["status"] == "fulfilled"
    assert updated["fulfilled_at"] is not None
    assert updated["agent_note"] == "done"
    # persisted, not just returned
    assert cr.get_request(entry["id"])["status"] == "fulfilled"


def test_update_invalid_status_rejected(store):
    entry = cr.add_request("poster1", "x")
    with pytest.raises(ValueError):
        cr.update_request(entry["id"], status="bogus")


def test_concurrent_add_no_lost_writes(store):
    """Many threads add_request at once; every append must survive.

    Without the lock, two threads that both load the same base, append their own
    entry, and save would lose one append (last-writer-wins on the whole list).
    The mutate_requests lock serializes the load-modify-save so all N land.
    """
    n = 50
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i):
        barrier.wait()  # release together to maximize overlap
        try:
            cr.add_request(f"poster{i}", f"req {i}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent add_request raised: {errors[:3]}"
    assert len(cr.list_requests()) == n, "an append was lost to a race"


def test_concurrent_update_no_lost_writes(store):
    """Concurrent updates to distinct requests must all persist.

    Each update loads the whole list, mutates one entry, saves the whole list.
    Without serialization, overlapping updates would drop sibling changes.
    """
    ids = [cr.add_request(f"poster{i}", f"req {i}")["id"] for i in range(30)]
    barrier = threading.Barrier(len(ids))
    errors: list[BaseException] = []

    def worker(rid):
        barrier.wait()
        try:
            cr.update_request(rid, status="fulfilled")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent update_request raised: {errors[:3]}"
    assert all(r["status"] == "fulfilled" for r in cr.list_requests()), (
        "an update was lost to a race"
    )
