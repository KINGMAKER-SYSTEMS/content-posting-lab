"""Tests for the Sound Assignments send path.

The Sound Assignments feature delivers each poster a *personalized, per-page*
playlist message to their dedicated Campaign Sounds topic, via a separate,
send-only SOUNDS bot.

Before this was wired through one helper, the scheduled daily batch and the
manual API endpoints diverged: the scheduler dumped a single global sound list
to every poster (main bot, ignoring playlists), while the manual endpoints sent
the personalized per-page message (sounds bot). These tests pin the contract
that both paths now go through ``telegram_bot.send_sound_assignments`` so they
can't drift apart again.
"""

import pytest

import telegram_bot
from services import telegram as tg_service


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Point services.telegram at a throwaway config."""
    monkeypatch.setattr(tg_service, "CONFIG_PATH", tmp_path / "telegram_config.json")
    return tmp_path


class _RecordingSoundsBot:
    """A SOUNDS-bot stand-in that records sends and topic creations."""

    def __init__(self, next_topic_id: int = 777):
        self.sent: list[dict] = []
        self.created_topics: list[tuple[int, str]] = []
        self._next_topic_id = next_topic_id

    async def create_forum_topic(self, chat_id, name):
        self.created_topics.append((chat_id, name))
        return type("T", (), {"message_thread_id": self._next_topic_id})()

    async def send_message(self, *, chat_id, text, message_thread_id=None, **k):
        self.sent.append(
            {"chat_id": chat_id, "text": text, "topic_id": message_thread_id}
        )
        return type("M", (), {"message_id": 1})()


def _seed_poster_with_playlist(monkeypatch):
    """Create one poster owning one page that has a one-song active playlist."""
    sound = tg_service.add_sound("https://tiktok.com/music/abc", "Hot Track")
    tg_service.set_poster(
        "seffra",
        {"name": "Seffra", "chat_id": -100123, "page_ids": ["page-1"]},
    )
    tg_service.set_page_playlist("page-1", [sound["id"]])
    return sound


async def test_send_skips_silently_when_no_songs(isolated_config, monkeypatch):
    """A poster with no active playlist songs must not trigger any send."""
    bot = _RecordingSoundsBot()
    monkeypatch.setattr(telegram_bot, "_sounds_bot", bot)
    tg_service.set_poster("nobody", {"name": "Nobody", "chat_id": -100999, "page_ids": []})

    poster = tg_service.get_poster("nobody")
    result = await telegram_bot.send_sound_assignments("nobody", poster, {})

    assert result["sent"] is False
    assert result["reason"] == "no_songs"
    assert bot.sent == []


async def test_send_uses_sounds_bot_and_autocreates_topic(isolated_config, monkeypatch):
    """The personalized per-page message goes out via the SOUNDS bot, and the
    Campaign Sounds topic is auto-created + persisted when missing."""
    bot = _RecordingSoundsBot(next_topic_id=4242)
    monkeypatch.setattr(telegram_bot, "_sounds_bot", bot)
    _seed_poster_with_playlist(monkeypatch)

    poster = tg_service.get_poster("seffra")
    assert poster.get("sounds_topic_id") is None  # no topic yet

    result = await telegram_bot.send_sound_assignments(
        "seffra", poster, {"page-1": "Page One"}
    )

    assert result["sent"] is True
    assert result["song_count"] == 1
    # Topic was created via the sounds bot and persisted to config.
    assert bot.created_topics == [(-100123, "Campaign Sounds")]
    assert tg_service.get_poster("seffra")["sounds_topic_id"] == 4242
    # Exactly one personalized send, to the new topic, naming the page.
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent["chat_id"] == -100123
    assert sent["topic_id"] == 4242
    assert "Page One" in sent["text"]
    assert "Hot Track" in sent["text"]


async def test_send_noop_when_sounds_bot_not_running(isolated_config, monkeypatch):
    """No sounds bot => silent no-op (never falls back to the main bot)."""
    monkeypatch.setattr(telegram_bot, "_sounds_bot", None)
    _seed_poster_with_playlist(monkeypatch)

    poster = tg_service.get_poster("seffra")
    result = await telegram_bot.send_sound_assignments("seffra", poster, {"page-1": "P1"})
    assert result["sent"] is False
    assert result["reason"] == "sounds_bot_not_running"


class _NoopMainBot:
    """Main bot stand-in: summary/forward calls are harmless no-ops here."""

    async def send_message(self, *a, **k):
        return type("M", (), {"message_id": 1})()


async def test_daily_batch_routes_assignments_through_sounds_bot(isolated_config, monkeypatch):
    """The scheduled daily batch must deliver per-page Sound Assignments via the
    SOUNDS bot — not a global dump on the main bot. A poster with a playlist but
    no new videos still gets their assignments."""
    sounds_bot = _RecordingSoundsBot(next_topic_id=555)
    monkeypatch.setattr(telegram_bot, "_bot", _NoopMainBot())
    monkeypatch.setattr(telegram_bot, "_sounds_bot", sounds_bot)
    # No new videos forwarded for anyone.
    monkeypatch.setattr(telegram_bot, "list_all_pages", lambda: [
        {"integration_id": "page-1", "name": "Page One"}
    ])

    tg_service.set_staging_group(chat_id=-100555, name="Staging")
    _seed_poster_with_playlist(monkeypatch)

    result = await telegram_bot.run_daily_batch()

    # The per-page assignment was sent via the sounds bot to the auto-created topic.
    assert result["sound_assignments_sent"] == 1
    assert len(sounds_bot.sent) == 1
    sent = sounds_bot.sent[0]
    assert sent["chat_id"] == -100123
    assert sent["topic_id"] == 555
    assert "Page One" in sent["text"]
    assert "Hot Track" in sent["text"]
