"""Offline regressions for malformed credentials, freshness and revocation."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from services import miniapp_auth as auth, telegram as tg

TOKEN = "test-token"


def signed(fields):
    fields = dict(fields)
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.parametrize("stamp", [None, "0", "-1", "bad", "1031"])
def test_invalid_or_future_auth_date_rejected(monkeypatch, stamp):
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "60")
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    fields = {} if stamp is None else {"auth_date": stamp}
    with pytest.raises(auth.AuthError):
        auth.parse_init_data(signed(fields), bot_token=TOKEN)


@pytest.mark.parametrize("stamp", ["940", "1000", "1030"])
def test_freshness_and_clock_skew_boundaries(monkeypatch, stamp):
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "60")
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    assert auth.parse_init_data(signed({"auth_date": stamp}), bot_token=TOKEN)


def test_explicit_freshness_disable_remains_supported(monkeypatch):
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "0")
    assert auth.parse_init_data(signed({"query_id": "x"}), bot_token=TOKEN)


@pytest.mark.parametrize("value", ["é", "f" * 63, "g" * 64])
def test_malformed_hash_is_auth_error(value):
    with pytest.raises(auth.AuthError):
        auth.parse_init_data(urlencode({"hash": value}), bot_token=TOKEN)


@pytest.mark.parametrize("user", [[], "user", 1, {"id": "abc"},
                                      {"id": True}, {"id": -1},
                                      {"id": 1, "username": []}])
def test_malformed_user_never_reaches_lookup(monkeypatch, user):
    monkeypatch.setattr(auth, "get_bot_token", lambda: TOKEN)
    monkeypatch.setenv("MINIAPP_INITDATA_MAX_AGE", "0")
    monkeypatch.setattr(auth, "get_poster_for_user",
                        lambda **kw: pytest.fail("invalid user reached lookup"))
    with pytest.raises(auth.AuthError):
        auth.resolve_poster_from_request(signed({"user": json.dumps(user)}))


def test_unbind_revokes_username_alias_without_removing_other_users(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram.json")
    tg.set_poster("p", {"name": "Poster"})
    tg.bind_user_to_poster("p", 555, "alice")
    tg.bind_user_to_poster("p", 777)
    tg.unbind_user_from_poster("p", 555)
    assert tg.get_poster_for_user(555, "alice") is None
    assert tg.get_poster_for_user(777)["poster_id"] == "p"
