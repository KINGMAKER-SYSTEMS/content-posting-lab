"""Tests for services.abn_experts.ask error surfacing."""
import logging

import services.abn_experts as experts


def test_ask_unknown_role_returns_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert experts.ask("not-a-real-role", "hi") is None


def test_ask_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert experts.ask("critic", "hi") is None


def test_ask_logs_warning_on_api_failure(monkeypatch, caplog):
    """A swallowed OpenAI error must still surface as a logged warning."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class _BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated auth failure")

    import openai
    monkeypatch.setattr(openai, "OpenAI", _BoomClient)

    with caplog.at_level(logging.WARNING, logger="services.abn_experts"):
        result = experts.ask("critic", "hi")

    assert result is None  # contract preserved: callers still get None
    assert any("simulated auth failure" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
