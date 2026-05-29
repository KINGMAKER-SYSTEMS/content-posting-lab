"""
Telegram Mini App authentication.

Validates the `initData` string the Telegram WebApp SDK hands the front end,
per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app,
then resolves the authenticated Telegram user to the poster they act as.

Validation (HMAC variant):
  secret_key      = HMAC_SHA256(key="WebAppData", msg=bot_token)
  data_check_str  = "\n".join(sorted "key=value" for all fields except `hash`)
  expected_hash   = hex(HMAC_SHA256(key=secret_key, msg=data_check_str))
  valid           = constant_time_eq(expected_hash, provided hash)

A dev bypass (env MINIAPP_DEV_AUTH=1 + header X-Dev-Poster-Id) exists for
local testing without a live bot.
"""

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from services.telegram import get_bot_token, get_poster, get_poster_for_user


class AuthError(Exception):
    """Raised when initData is missing, invalid, or cannot be resolved."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _dev_auth_enabled() -> bool:
    return os.getenv("MINIAPP_DEV_AUTH", "").strip().lower() in ("1", "true", "yes")


def _max_age_seconds() -> int:
    """How old `auth_date` may be before initData is rejected. 0 disables."""
    try:
        return int(os.getenv("MINIAPP_INITDATA_MAX_AGE", "86400"))
    except ValueError:
        return 86400


def parse_init_data(init_data: str, *, bot_token: str | None = None) -> dict:
    """Validate an initData string and return its parsed fields.

    Raises AuthError on any validation failure. The returned dict includes a
    parsed `user` dict when present.
    """
    if not init_data:
        raise AuthError("missing initData")

    token = bot_token if bot_token is not None else get_bot_token()
    if not token:
        raise AuthError("bot token not configured; cannot validate initData", 503)

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = pairs.pop("hash", None)
    if not provided_hash:
        raise AuthError("initData missing hash")

    data_check_string = "\n".join(
        f"{k}={pairs[k]}" for k in sorted(pairs.keys())
    )
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, provided_hash):
        raise AuthError("initData signature mismatch", 403)

    max_age = _max_age_seconds()
    if max_age > 0:
        try:
            auth_date = int(pairs.get("auth_date", "0"))
        except ValueError:
            auth_date = 0
        if auth_date and (time.time() - auth_date) > max_age:
            raise AuthError("initData expired", 403)

    out = dict(pairs)
    if "user" in pairs:
        try:
            out["user"] = json.loads(pairs["user"])
        except (json.JSONDecodeError, TypeError):
            out["user"] = None
    return out


def resolve_poster_from_request(
    init_data: str | None,
    dev_poster_id: str | None = None,
) -> dict:
    """Return the poster dict for an incoming Mini App request.

    Honors the dev bypass when enabled, otherwise validates initData and maps
    the Telegram user to a poster via telegram_user_ids / telegram_username.
    Raises AuthError if it can't.
    """
    if dev_poster_id and _dev_auth_enabled():
        poster = get_poster(dev_poster_id)
        if poster is None:
            raise AuthError(f"dev poster {dev_poster_id!r} not found", 404)
        return poster

    parsed = parse_init_data(init_data or "")
    user = parsed.get("user") or {}
    user_id = user.get("id")
    username = user.get("username")
    if user_id is None and not username:
        raise AuthError("initData has no user")

    poster = get_poster_for_user(user_id=user_id, username=username)
    if poster is None:
        raise AuthError(
            "this Telegram account is not linked to a poster — ask an admin to "
            "bind it in the Distribution → Telegram tab",
            403,
        )
    return poster
