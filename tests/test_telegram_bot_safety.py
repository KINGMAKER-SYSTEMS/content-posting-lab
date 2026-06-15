"""Safety regression tests for the Telegram bot's inventory scanner.

Guards the fix for the "destructive delete-and-repost behavior in inventory
tracking" flagged in CLAUDE.md. The root cause was `_scan_topic_fallback` in
`telegram_bot.py`: when the safe Pyrogram MTProto scanner was unavailable it
brute-forced a topic by *forwarding every message back into the same staging
group* to read its media, then deleting the copy. A crash / rate-limit /
permission failure between the forward and the delete orphaned content in the
group and desynced inventory.

These tests pin the contract that the fallback is now inert: it never forwards,
never deletes, never sends, and never mutates inventory. If anyone reintroduces
the forward-and-delete probe, `_BoomBot` makes the test fail loudly.
"""

import pytest

import telegram_bot
from services import telegram as tg_service


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point services.telegram at a throwaway config so we never touch the real one."""
    monkeypatch.setattr(tg_service, "CONFIG_PATH", tmp_path / "telegram_config.json")
    return tmp_path


class _BoomBot:
    """A bot stand-in that explodes on any destructive (forward/delete/send) call.

    Used so that if the fallback ever tries to mutate the group again, the test
    fails with a clear message instead of silently passing.
    """

    async def send_message(self, *a, **k):  # pragma: no cover - must not be called
        raise AssertionError(
            "fallback scan must NOT send messages into the group (destructive probe)"
        )

    async def forward_message(self, *a, **k):  # pragma: no cover - must not be called
        raise AssertionError(
            "fallback scan must NOT forward messages — this is the orphan-content bug"
        )

    async def delete_message(self, *a, **k):  # pragma: no cover - must not be called
        raise AssertionError(
            "fallback scan must NOT delete messages — this is the orphan-content bug"
        )


async def test_scan_fallback_is_non_destructive(isolated_config, monkeypatch):
    """The brute-force fallback must not forward, delete, send, or write inventory."""
    # Force a live bot object that would blow up on any destructive call, and
    # ensure pyrogram is treated as unavailable so the fallback path is taken.
    monkeypatch.setattr(telegram_bot, "_bot", _BoomBot())
    monkeypatch.setattr(telegram_bot, "_pyro", None)

    result = await telegram_bot.scan_topic_inventory(
        chat_id=-1001234567890,
        topic_id=5,
        integration_id="page-abc",
        next_topic_id=500,
    )

    # No content was touched and the scan reports itself as unavailable.
    assert result["found"] == 0
    assert result["total_scanned"] == 0
    assert "unavailable" in result.get("error", "").lower()

    # And nothing was written to inventory as a side effect.
    assert tg_service.get_inventory("page-abc") == []


async def test_scan_fallback_safe_without_bot(isolated_config, monkeypatch):
    """Even with no bot connected, the fallback returns cleanly (no raise)."""
    monkeypatch.setattr(telegram_bot, "_bot", None)
    monkeypatch.setattr(telegram_bot, "_pyro", None)

    result = await telegram_bot._scan_topic_fallback(
        chat_id=-100,
        topic_id=1,
        integration_id="page-xyz",
    )
    assert result["found"] == 0
    assert result["total_scanned"] == 0
    assert tg_service.get_inventory("page-xyz") == []
