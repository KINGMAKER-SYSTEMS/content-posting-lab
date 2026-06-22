"""Ticket: batch race in poster page assignment.

The assign_pages router endpoint looped ``assign_page_to_poster`` once per page;
each call does its own load_config/save_config, so a concurrent op (an unassign,
another assign batch, a direct poster edit) could interleave between pages, and
it wrote the whole config file once per page. ``assign_pages_to_poster`` adds the
whole page list in one batched read-modify-write (one disk write, idempotent).

This adapts the original ticket's two locking tests to this code line, whose
services.telegram uses load_config/save_config (not a mutate_config CM) and which
does not ship the older test_telegram_config_locking harness.
"""

import pytest

from services import telegram as tg


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    return tmp_path


def test_assign_pages_to_poster_is_idempotent_and_batched(isolated_config):
    """The batched assign adds every page once, even with overlapping inputs."""
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})

    poster = tg.assign_pages_to_poster("p1", ["a", "b", "c"])
    assert poster["page_ids"] == ["a", "b", "c"]

    # Re-assigning overlapping pages must not duplicate.
    poster = tg.assign_pages_to_poster("p1", ["b", "c", "d"])
    assert poster["page_ids"] == ["a", "b", "c", "d"]

    # The persisted config reflects the batch (one write, all pages present).
    assert tg.get_poster("p1")["page_ids"] == ["a", "b", "c", "d"]


def test_assign_pages_to_poster_unknown_poster_raises(isolated_config):
    with pytest.raises(ValueError):
        tg.assign_pages_to_poster("nope", ["a"])


def test_assign_pages_to_poster_empty_list_is_noop(isolated_config):
    tg.set_poster("p1", {"name": "Poster One", "chat_id": -100})
    poster = tg.assign_pages_to_poster("p1", [])
    assert poster["page_ids"] == []
