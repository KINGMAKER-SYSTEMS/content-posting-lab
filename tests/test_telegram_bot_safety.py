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


class _SeqBot:
    """A bot stand-in for the live forward path: marker-send to find the ceiling,
    then a per-id forward loop.

    Each entry in ``fail_ids`` maps a message id to the error string its forward
    should raise. A "message to forward not found"-style string is benign (that
    id simply doesn't exist); a rate-limit / forbidden string is a *blocking*
    failure on a real message that must cap the high-water mark.
    """

    def __init__(self, ceiling: int, fail_ids: dict[int, str] | None = None):
        self._ceiling = ceiling
        self._fail_ids = fail_ids or {}
        self.forwarded_ids: list[int] = []

    async def send_message(self, *a, **k):
        return type("M", (), {"message_id": self._ceiling})()

    async def delete_message(self, *a, **k):
        return None

    async def forward_message(self, *a, message_id=None, **k):
        if message_id in self._fail_ids:
            raise RuntimeError(self._fail_ids[message_id])
        self.forwarded_ids.append(message_id)
        return type("M", (), {"message_id": message_id})()


async def test_forward_checkpoints_each_message(isolated_config, monkeypatch):
    """on_forward fires once per successful forward, in order — enabling the
    caller to persist a per-message high-water mark."""
    bot = _SeqBot(ceiling=5)  # ids 1..4 all forward
    monkeypatch.setattr(telegram_bot, "_bot", bot)

    checkpoints: list[int] = []
    result = await telegram_bot.forward_new_messages(
        from_chat_id=-100, from_topic_id=1, to_chat_id=-200, to_topic_id=2,
        after_message_id=0, on_forward=checkpoints.append,
    )

    assert bot.forwarded_ids == [1, 2, 3, 4]
    assert checkpoints == [1, 2, 3, 4]
    assert result["last_message_id"] == 4  # advances to the scan ceiling (5 - 1)


async def test_benign_missing_ids_still_advance_the_mark(isolated_config, monkeypatch):
    """Nonexistent message ids (the common case) are skipped and the high-water
    mark still advances past them — otherwise every run re-forwards the range
    (the duplicate-repost half of the bug)."""
    # Ceiling 6 => ids 1..5; id 3 is a nonexistent/service message.
    bot = _SeqBot(ceiling=6, fail_ids={3: "Bad Request: message to forward not found"})
    monkeypatch.setattr(telegram_bot, "_bot", bot)

    result = await telegram_bot.forward_new_messages(
        from_chat_id=-100, from_topic_id=1, to_chat_id=-200, to_topic_id=2,
        after_message_id=0,
    )

    assert bot.forwarded_ids == [1, 2, 4, 5]
    assert result["errors"] == 1
    # Benign skip => mark advances to the ceiling so the gap isn't re-scanned.
    assert result["last_message_id"] == 5


async def test_blocking_failure_caps_the_mark_no_orphan(isolated_config, monkeypatch):
    """A rate-limit / permission failure on a REAL message must NOT be leapfrogged.

    If the high-water mark advanced past it, that message would be orphaned —
    the content half of the "destructive repost / orphan-content" inventory bug.
    The mark must cap at the last id actually forwarded so the failed id is
    retried on the next run.
    """
    # Ceiling 6 => ids 1..5; id 3 hits a rate limit (a real, unforwarded message).
    bot = _SeqBot(ceiling=6, fail_ids={3: "Too Many Requests: retry after 30"})
    monkeypatch.setattr(telegram_bot, "_bot", bot)

    checkpoints: list[int] = []
    result = await telegram_bot.forward_new_messages(
        from_chat_id=-100, from_topic_id=1, to_chat_id=-200, to_topic_id=2,
        after_message_id=0, on_forward=checkpoints.append,
    )

    # Loop stops at the blocking failure: only 1 and 2 forwarded.
    assert bot.forwarded_ids == [1, 2]
    # Mark capped at 2 (NOT the ceiling 5) so id 3 is retried, not orphaned.
    assert result["last_message_id"] == 2
    assert checkpoints == [1, 2]
    assert 3 not in checkpoints


async def test_forward_without_callback_is_safe(isolated_config, monkeypatch):
    """The checkpoint callback is optional — omitting it still forwards cleanly."""
    bot = _SeqBot(ceiling=4)  # ids 1..3
    monkeypatch.setattr(telegram_bot, "_bot", bot)

    result = await telegram_bot.forward_new_messages(
        from_chat_id=-100, from_topic_id=1, to_chat_id=-200, to_topic_id=2,
        after_message_id=0,
    )

    assert result["forwarded"] == 3
    assert result["last_message_id"] == 3
    assert bot.forwarded_ids == [1, 2, 3]
