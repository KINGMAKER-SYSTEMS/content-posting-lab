"""Unit tests for services/abn_experts.py — the EXPERT AGENT REGISTRY.

abn_experts defines every LLM persona (researcher, scout, deepdive, loremaster,
narrator, scriptwriter, seo, titler, thumbnailer, captioner, commenter, coder,
critic) that the ABN factory invokes BY HARDCODED ROLE NAME via experts.ask(...).
A malformed config (missing field, wrong type, absurd token cap) silently
propagates into script generation, so these tests pin that:

  * every config dict is well-formed: model/max_tokens/temperature/system all
    present, correctly typed, and within sane bounds;
  * the model id is a real OpenAI chat model the rest of the lab already uses;
  * every role the factory calls by name actually exists in the registry;
  * the shared VOICE persona is wired into the public-facing experts (and the
    machine-format experts deliberately omit it);
  * ask() degrades safely (None) on unknown role or missing key, never raising;
  * ask() surfaces a logged warning when a swallowed OpenAI error occurs;
  * roster() reports exactly the registry keys.

No network, no OpenAI client — ask() is exercised only on its early-return
paths (unknown role / no API key), which never construct a client.
"""
import logging
import os

import pytest

from services import abn_experts
import services.abn_experts as experts


# Every role the factory (services/abn_factory.py) invokes by hardcoded name.
# If a rename drops one of these the pipeline silently no-ops, so pin them.
FACTORY_REQUIRED_ROLES = {
    "researcher", "scout", "deepdive", "loremaster", "narrator",
    "scriptwriter", "seo", "titler", "thumbnailer", "captioner",
    "commenter", "coder", "critic",
}

# Experts whose output is read by a human / consumed as prose carry the shared
# VOICE persona. The machine-format experts (compact lists, single SCORE line)
# deliberately omit it to avoid biasing a structured parse.
NO_VOICE_ROLES = {"captioner", "coder", "critic"}


def test_every_required_role_exists():
    missing = FACTORY_REQUIRED_ROLES - set(abn_experts.EXPERTS)
    assert not missing, f"factory calls these roles but registry lacks them: {missing}"


@pytest.mark.parametrize("role", sorted(abn_experts.EXPERTS))
def test_config_is_well_formed(role):
    cfg = abn_experts.EXPERTS[role]
    assert set(cfg) >= {"model", "max_tokens", "temperature", "system"}, \
        f"{role} missing required keys: {{'model','max_tokens','temperature','system'}} - {set(cfg)}"

    assert isinstance(cfg["model"], str) and cfg["model"], f"{role} model must be a non-empty str"

    assert isinstance(cfg["max_tokens"], int) and not isinstance(cfg["max_tokens"], bool), \
        f"{role} max_tokens must be an int"
    assert 1 <= cfg["max_tokens"] <= 4096, f"{role} max_tokens out of sane range: {cfg['max_tokens']}"

    assert isinstance(cfg["temperature"], (int, float)) and not isinstance(cfg["temperature"], bool), \
        f"{role} temperature must be numeric"
    assert 0.0 <= cfg["temperature"] <= 2.0, f"{role} temperature out of OpenAI range: {cfg['temperature']}"

    assert isinstance(cfg["system"], str) and len(cfg["system"]) > 40, \
        f"{role} system prompt must be a substantial str"


@pytest.mark.parametrize("role", sorted(abn_experts.EXPERTS))
def test_model_is_a_known_openai_chat_model(role):
    # The whole registry runs on gpt-4.1-mini today (cheap, fast, the lab's
    # standard text model). Pin it so a typo'd / hallucinated model id fails
    # loudly here instead of as a 404 mid-episode.
    assert abn_experts.EXPERTS[role]["model"] == "gpt-4.1-mini"


def test_voice_persona_wired_into_prose_experts():
    for role, cfg in abn_experts.EXPERTS.items():
        has_voice = abn_experts.VOICE in cfg["system"]
        if role in NO_VOICE_ROLES:
            assert not has_voice, f"{role} is a machine-format expert; VOICE should be omitted"
        else:
            assert has_voice, f"{role} is a prose expert; VOICE persona must be appended"


def test_voice_persona_is_grounded():
    # VOICE is the locked audience profile; guard the load-bearing phrases so a
    # well-meaning edit can't quietly drift the channel's whole persona.
    assert "AgenticBuilderNews voice" in abn_experts.VOICE
    assert "RETENTION" in abn_experts.VOICE
    assert "BROAD APPEAL" in abn_experts.VOICE


def test_roster_matches_registry_keys():
    assert abn_experts.roster() == list(abn_experts.EXPERTS.keys())
    # roster is the public discovery surface; it must cover every required role.
    assert FACTORY_REQUIRED_ROLES <= set(abn_experts.roster())


def test_ask_unknown_role_returns_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    assert abn_experts.ask("no-such-role", "hello") is None


def test_ask_without_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # A real, valid role — but no key means it must short-circuit to None and
    # never attempt to construct an OpenAI client.
    assert abn_experts.ask("scriptwriter", "write a beat") is None


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
