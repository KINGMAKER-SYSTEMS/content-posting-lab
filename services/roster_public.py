"""What the /api/roster HTTP surface may say about a page, and who may change it.

The roster cache (services/roster.py) holds account credentials for every
page: the Notion signup email and password, the forwarding address, and the
Cloudflare Email Routing alias/rule/destination. Other parts of the Lab read
them from disk. The /api/roster routes, however, answer an operator UI that
sends no credential at all and are reachable from the public internet
whenever APP_API_KEY is unset (as in production). So:

* Every row leaves through ``public_page`` -- an allowlist. A field that is
  not named in ``PUBLIC_PAGE_FIELDS`` never crosses, including fields added to
  the roster cache later.
* The whole response body then passes ``scrub_credentials``, which removes any
  credential-shaped key at any depth. It is the backstop for a route that
  forgets ``public_page``, not a replacement for it.
* ``require_roster_auth`` guards routes no unauthenticated UI calls. It accepts
  the Control Plane bearer (CONTROL_PLANE_TOKEN) or the app key (APP_API_KEY)
  and fails closed (503) when neither is configured.
"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any

from fastapi import Header, HTTPException

# Safe roster row fields. Mirrors the ontology the Control Plane snapshot
# already carries (routers/control_plane.py ROSTER_SNAPSHOT_FIELDS) plus the
# UI bookkeeping the Distribution/Pipeline screens render.
PUBLIC_PAGE_FIELDS: tuple[str, ...] = (
    "integration_id",
    "name",
    "provider",
    "picture",
    "project",
    "drive_folder_url",
    "drive_folder_id",
    "source",
    "tiktok_url",
    "poster_name",
    "group",
    "group_label",
    "account_type",
    "notion_page_id",
    "status",
    "pipeline",
    "page_type",
    "content_niche",
    "content_engine",
    "automation_mode",
    "vault_url",
    "account_status",
    "archived",
    "sounds_reference",
    "go_live_date",
    "r2_prefix",
    "r2_bucket",
    "added_at",
    "updated_at",
    # Enrichment added by GET /api/roster/project/{name}.
    "has_staging_topic",
    "staging_topic_name",
)

# Keys that carry, or point at, an account credential. Matched
# case-insensitively as a substring so `signupEmail`, `fwd_destination`,
# `email_rule_id`, `api_key`, `session_cookie` etc. are all caught.
_CREDENTIAL_KEY_RE = re.compile(
    r"passw"
    r"|secret"
    r"|token"
    r"|credential"
    r"|cookie"
    r"|session"
    r"|api_?key"
    r"|signup_?email"
    r"|fwd"
    r"|forward(ing)?_?(address|destination|to)"
    r"|email_?(alias|rule)"
    r"|alias"
    r"|(^|_)otp($|_)"
    r"|2fa|mfa|totp",
    re.IGNORECASE,
)

# Counters on the dedup/telegram surfaces that merely contain the word
# "forward" (inventory_forwarded) are counts, not addresses.
_CREDENTIAL_KEY_ALLOW = frozenset({"inventory_forwarded"})


def is_credential_key(key: object) -> bool:
    """True when a response key names a credential and must not be serialised."""
    if not isinstance(key, str):
        return False
    if key in _CREDENTIAL_KEY_ALLOW:
        return False
    return bool(_CREDENTIAL_KEY_RE.search(key))


def public_page(page: dict[str, Any]) -> dict[str, Any]:
    """Reduce one roster row to the fields the HTTP surface may carry."""
    allowed = set(PUBLIC_PAGE_FIELDS)
    return {
        key: value
        for key, value in page.items()
        if key in allowed and not is_credential_key(key)
    }


def public_pages(pages: Any) -> list[dict[str, Any]]:
    return [public_page(page) for page in (pages or []) if isinstance(page, dict)]


def scrub_credentials(value: Any) -> Any:
    """Return ``value`` with every credential-shaped dict key removed, at any depth."""
    if isinstance(value, dict):
        return {
            key: scrub_credentials(item)
            for key, item in value.items()
            if not is_credential_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [scrub_credentials(item) for item in value]
    return value


def _configured_tokens() -> list[bytes]:
    tokens = []
    for name in ("CONTROL_PLANE_TOKEN", "APP_API_KEY"):
        token = (os.getenv(name) or "").strip()
        if token:
            tokens.append(token.encode("utf-8"))
    return tokens


def require_roster_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: the caller must present a configured machine credential."""
    tokens = _configured_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="Roster authentication is not configured")
    supplied: list[bytes] = []
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        supplied.append(authorization.removeprefix("Bearer ").strip().encode("utf-8"))
    if isinstance(x_api_key, str) and x_api_key.strip():
        supplied.append(x_api_key.strip().encode("utf-8"))
    for candidate in supplied:
        for token in tokens:
            if hmac.compare_digest(candidate, token):
                return None
    raise HTTPException(status_code=401, detail="Invalid or missing roster credential")
