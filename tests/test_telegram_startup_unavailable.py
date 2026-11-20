"""An unavailable Telegram configuration never stops the whole service starting.

Codex P1 on #403: with TELEGRAM_BOT_TOKEN unset, the lifespan's token lookup
read a malformed or unreadable Telegram config, raised JsonStoreUnavailable
before yielding, and took every route down, health included. Startup must
keep the file's bytes, skip the bot, and leave Telegram routes answering 503.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import telegram_bot
from services import telegram as tg


@pytest.mark.parametrize("contents", [b'{"unfinished":', b"[]", b"\xff"])
def test_unavailable_telegram_config_keeps_the_service_up(tmp_path, monkeypatch, contents):
    path = tmp_path / "telegram_config.json"
    path.write_bytes(contents)
    monkeypatch.setattr(tg, "CONFIG_PATH", path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(app_module, "_APP_API_KEY", None)
    started = []

    async def start_bot(token):
        started.append(token)

    monkeypatch.setattr(telegram_bot, "start_bot", start_bot)

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/telegram/status").status_code == 503

    assert started == []
    assert path.read_bytes() == contents
