"""Tests for the page-roster store (services/roster.py).

Focus: the load-modify-save transactions (set_page / remove_page) must be
serialized by ROSTER_PATH's lock so concurrent callers — email_routing,
pipeline, roster, notion_pages all write the roster from async routers — don't
clobber each other's writes. Same TOCTOU guard content_requests.mutate_requests
and telegram.mutate_config provide. Mirrors fa451002 (TimelineStore RMW lock).
"""

import threading

import pytest

from services import roster


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the module at a throwaway JSON file on a tmp volume."""
    p = tmp_path / "page_roster.json"
    monkeypatch.setattr(roster, "ROSTER_PATH", p)
    return p


def test_set_and_get(store):
    entry = roster.set_page("int1", {"name": "Page One", "provider": "tiktok"})
    assert entry["integration_id"] == "int1"
    assert entry["name"] == "Page One"
    assert roster.get_page("int1") == entry


def test_set_page_preserves_added_at_on_update(store):
    first = roster.set_page("int1", {"name": "Page One"})
    second = roster.set_page("int1", {"project": "proj-x"})
    assert second["added_at"] == first["added_at"]
    assert second["project"] == "proj-x"
    assert second["name"] == "Page One"  # carried over


def test_set_page_persists_master_pages_generation_ontology(store):
    entry = roster.set_page("acct:miles", {
        "name": "miles.of.memories77",
        "content_niche": "POV — Night Core",
        "content_engine": "sourced_video",
        "automation_mode": "Automation",
        "vault_url": "https://shipstream.risingtidesviral.com/vault/miles.of.memories77",
        "account_status": "active",
        "archived": False,
    })

    assert roster.get_page("acct:miles") == entry
    assert entry["content_niche"] == "POV — Night Core"
    assert entry["content_engine"] == "sourced_video"
    assert entry["automation_mode"] == "Automation"
    assert entry["vault_url"] == "https://shipstream.risingtidesviral.com/vault/miles.of.memories77"
    assert entry["account_status"] == "active"
    assert entry["archived"] is False


def test_remove_page(store):
    roster.set_page("int1", {"name": "Page One"})
    assert roster.remove_page("int1") is True
    assert roster.get_page("int1") is None
    assert roster.remove_page("int1") is False


def test_concurrent_set_no_lost_writes(store):
    """Many threads set_page distinct integrations at once; every one must survive.

    Without the lock, two threads that both load the same base roster, insert
    their own page, and save would lose one insert (last-writer-wins on the whole
    pages dict). The mutate_roster lock serializes load-modify-save so all N land.
    """
    n = 50
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i):
        barrier.wait()  # release together to maximize overlap
        try:
            roster.set_page(f"int{i}", {"name": f"Page {i}"})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent set_page raised: {errors[:3]}"
    assert len(roster.list_all_pages()) == n, "a set_page was lost to a race"


def test_concurrent_remove_no_lost_writes(store):
    """Concurrent removes of distinct pages must all persist.

    Each remove loads the whole roster, deletes one entry, saves the whole dict.
    Without serialization, overlapping removes would drop sibling deletions.
    """
    for i in range(30):
        roster.set_page(f"int{i}", {"name": f"Page {i}"})

    barrier = threading.Barrier(30)
    errors: list[BaseException] = []

    def worker(i):
        barrier.wait()
        try:
            roster.remove_page(f"int{i}")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent remove_page raised: {errors[:3]}"
    assert roster.list_all_pages() == [], "a remove_page was lost to a race"
